"""One backend for the command line, GUI, and ngm:// desktop handler.

Game content and patching are handled by Nexon's unmodified NGM. We install
its official bootstrap modules, configure Wine, and verify the resulting client.
"""
from __future__ import annotations

import contextlib
import csv
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tarfile
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

GAME_CODE = "106603"
GAME = "106603@1585"
WEBSITE = "https://maplestoryworlds.nexon.com/en/play"
GAME_INFO = f"https://ngmapi.nexon.com/game-info/{GAME_CODE}"
PLATFORM = "https://platform.nexon.com/NGM/"
WEBVIEW_URL = "https://go.microsoft.com/fwlink/?linkid=2124701"
DXVK_VERSION = "3.1"
DXVK_URL = f"https://github.com/doitsujin/dxvk/releases/download/v{DXVK_VERSION}/dxvk-{DXVK_VERSION}.tar.gz"
DXVK_SHA256 = "30f9cc326874be344285582275446968cfa4c069db31ce56df312d6644179154"
DXVK_DLLS = ("d3d11.dll", "dxgi.dll")
GAME_REGISTRY = r"HKCU\Software\Nexon\MOD"
REQUIRED_PE = ("msw.exe", "UnityPlayer.dll", "GameAssembly.dll")
REQUIRED_DATA = ("msw_Data/globalgamemanagers", "msw_Data/boot.config",
                 "msw_Data/il2cpp_data/Metadata/global-metadata.dat")
MANIFEST = f"obd-manifest-{GAME_CODE}"


class HelperError(Exception):
    """An actionable failure, suitable for display in the GUI."""


class OperationInProgress(HelperError):
    pass


class Cancelled(HelperError):
    pass


def validate_uri(uri: str) -> str:
    """Inspect a copy, but pass the original string untouched to NGM.

    The official JS deliberately inserts a space after ngm://launch/ and uses
    encodeURIComponent (not form encoding). In particular, '+' must stay '+'.
    """
    if len(uri) > 32700 or not uri.lower().startswith("ngm://launch/"):
        raise HelperError("Expected a MapleStory Worlds ngm://launch/ link.")
    decoded = unquote(uri[len("ngm://launch/"):], errors="strict")
    if any(ord(c) < 32 or ord(c) == 127 for c in uri + decoded):
        raise HelperError("The launch link contains control characters.")
    if re.search(r"%(?![0-9a-fA-F]{2})", uri):
        raise HelperError("The launch link contains invalid percent encoding.")
    games = re.findall(r"(?:^|\s)-game:(?:'([^']+)'|([^\s]+))", decoded)
    if len(games) != 1 or (games[0][0] or games[0][1]).split("@", 1)[0] != GAME_CODE:
        raise HelperError("This handler accepts MapleStory Worlds links only (game 106603).")
    return uri


def make_uri(mode: str) -> str:
    if mode not in {"install", "restore", "launch"}:
        raise ValueError(mode)
    arguments = (f"-mode:{mode} -game:'{GAME}' -passarg:'' "
                 f"-position:'GameWeb|{WEBSITE}' -architectureplatform:'x64' "
                 f"-timestamp:{int(time.time() * 1000)}")
    return "ngm://launch/ " + quote(arguments, safe="~()*!.'-")


def redact(line: str) -> str:
    if re.search(r"ngm://|Command Line|(?:-|/)(?:token|npp|insign_token|ticket|sid):",
                 line, re.IGNORECASE):
        return "[Launch arguments omitted]"
    return line.rstrip()


def pe_machine(path: Path) -> int:
    try:
        with path.open("rb") as f:
            if f.read(2) != b"MZ":
                return 0
            f.seek(0x3C)
            offset = struct.unpack("<I", f.read(4))[0]
            f.seek(offset)
            if f.read(4) != b"PE\0\0":
                return 0
            return struct.unpack("<H", f.read(2))[0]
    except (OSError, struct.error):
        return 0


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
    temp.replace(path)


def official_url(url: str) -> str:
    """Upgrade Nexon's legacy HTTP metadata URLs; reject unexpected hosts."""
    if url == DXVK_URL:
        return url  # One pinned upstream release, verified against its SHA-256.
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise HelperError("Invalid download URL in publisher metadata.")
    if not any(host == domain or host.endswith("." + domain)
               for domain in ("nexon.com", "nexoncdn.co.kr", "microsoft.com")):
        raise HelperError(f"Unexpected download host: {host}")
    return parsed._replace(scheme="https").geturl()


class Helper:
    def __init__(self, data_dir: Path | str | None = None, wine: str | None = None,
                 emit=None, cancel: threading.Event | None = None):
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        self.home = Path(data_dir or os.environ.get("MSW_HOME", data_home / "msw-helper-wine")).expanduser().resolve()
        self.prefix = self.home / "prefix"
        self.cache = self.home / "cache"
        self.logs = self.home / "logs"
        self.wine = wine or os.environ.get("MSW_WINE", "wine")
        self.emit = emit or (lambda text: print(text, flush=True))
        self.cancel = cancel or threading.Event()
        self.ngm_dir = self.prefix / "drive_c/ProgramData/Nexon/NGM"
        self.game_dir = self.prefix / "drive_c/Nexon/MapleStory Worlds"

    def env(self) -> dict:
        env = os.environ.copy()
        env.update(WINEPREFIX=str(self.prefix), WINEDEBUG="-all")
        # Let the 64-bit host Wine choose its supported WoW64 layout; do not
        # inherit an unrelated application's WINEARCH=win32 setting.
        env.pop("WINEARCH", None)
        # Preserve the user's driver/audio settings. Never clear DISPLAY.
        overrides = env.get("WINEDLLOVERRIDES", "")
        env["WINEDLLOVERRIDES"] = overrides + (";" if overrides else "") + "winemenubuilder.exe=d"
        env.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu")
        env.setdefault("DXVK_LOG_PATH", str(self.logs))
        return env

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled("Operation cancelled. Run Install or Update / repair to resume.")

    @contextlib.contextmanager
    def locked(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with (self.home / "operation.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OperationInProgress("Another install, update, or launch is in progress.") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def download(self, url: str, target: Path, *, crc: int | None = None,
                 compressed=False, executable=False, sha256: str | None = None) -> Path:
        self.check_cancel()
        target.parent.mkdir(parents=True, exist_ok=True)

        def digest(path):
            with path.open("rb") as f:
                return hashlib.file_digest(f, "sha256").hexdigest()

        def valid(path):
            if not path.is_file() or not path.stat().st_size:
                return False
            if executable and not pe_machine(path):
                return False
            if sha256 is not None and digest(path) != sha256:
                return False
            return True

        # NGM's published `crc` is an opaque revision, not a whole-file zlib
        # CRC32 (verified against the official signed binaries). Use it to
        # invalidate caches; SHA-256 detects changes to our cached downloads.
        stamp = target.with_name(target.name + ".download.json")
        if sha256 is not None and crc is None and valid(target):
            return target
        if crc is not None and valid(target) and stamp.is_file():
            try:
                cached = json.loads(stamp.read_text())
                if cached.get("revision") == crc and cached.get("sha256") == digest(target):
                    return target
            except (OSError, ValueError):
                pass
        temp = target.with_name(target.name + ".part")
        url = official_url(url)
        self.emit(f"Downloading {target.name}…")
        try:
            request = Request(url, headers={"User-Agent": "msw-helper-wine/0.1"})
            with urlopen(request, timeout=45) as response, temp.open("wb") as out:
                if not response.geturl().startswith("https://"):
                    raise HelperError("Refusing an insecure download redirect.")
                total = int(response.headers.get("Content-Length", "0"))
                received, last = 0, time.monotonic()
                while chunk := response.read(1024 * 1024):
                    self.check_cancel()
                    out.write(chunk)
                    received += len(chunk)
                    if time.monotonic() - last > 2:
                        suffix = f" / {total // 1048576} MiB" if total else " MiB"
                        self.emit(f"{target.name}: {received // 1048576}{suffix}")
                        last = time.monotonic()
                if total and received != total:
                    raise HelperError(f"Incomplete download: {target.name}. Please retry.")
            if compressed:
                unpacked = temp.with_suffix(".unpacked")
                try:
                    with gzip.open(temp, "rb") as source, unpacked.open("wb") as out:
                        shutil.copyfileobj(source, out)
                    unpacked.replace(temp)
                finally:
                    unpacked.unlink(missing_ok=True)
            if not valid(temp):
                raise HelperError(f"Download or executable validation failed: {target.name}")
            temp.replace(target)
            if crc is not None:
                atomic_json(stamp, {"revision": crc, "sha256": digest(target), "url": url})
            return target
        finally:
            temp.unlink(missing_ok=True)

    def metadata(self, url: str) -> dict:
        self.check_cancel()
        with urlopen(Request(official_url(url), headers={"User-Agent": "msw-helper-wine/0.1"}), timeout=45) as r:
            if not r.geturl().startswith("https://"):
                raise HelperError("Refusing an insecure metadata redirect.")
            data = json.loads(r.read(2 * 1024 * 1024))
        if not isinstance(data, dict):
            raise HelperError("Unexpected response from Nexon's metadata service.")
        return data

    def run(self, args: list[str], *, timeout=120, label="Wine", allowed=(0,)) -> str:
        """Drain pipes even when a launcher spawns a long-lived child.

        Wait for the process we started, not pipe EOF or wineserver --wait.
        Those can wait forever for the game or Edge's background services.
        """
        self.check_cancel()
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = self.logs / "helper.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        output = []
        env = self.env()
        if args and args[0] == "wineboot":
            # NGM and Worlds are native applications; optional Mono/Gecko first
            # run dialogs must not block installation. Only suppress for wineboot.
            env["WINEDLLOVERRIDES"] += ";mscoree,mshtml="
        try:
            process = subprocess.Popen([self.wine, *map(str, args)], env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            os.close(fd)
            raise
        def drain():
            with os.fdopen(fd, "a", encoding="utf-8") as log:
                for raw in iter(process.stdout.readline, b""):
                    line = redact(raw.decode("utf-8", errors="replace"))
                    if line:
                        output.append(line)
                        if len(output) > 300:
                            del output[:100]
                        log.write(line + "\n")
                        log.flush()
                process.stdout.close()
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        start = time.monotonic()
        try:
            while process.poll() is None:
                self.check_cancel()
                if timeout and time.monotonic() - start > timeout:
                    raise HelperError(f"{label} timed out. See {log_path}.")
                self.cancel.wait(0.2)
        except (HelperError, KeyboardInterrupt):
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
        reader.join(timeout=1)
        if process.returncode not in allowed:
            raise HelperError(f"{label} exited with code {process.returncode}. See {log_path}.")
        return "\n".join(output)

    def reg(self, key, name, value):
        self.run(["reg", "add", key, "/v", name, "/t", "REG_SZ", "/d", value, "/f"])

    def require_wine(self):
        if not shutil.which(self.wine):
            raise HelperError("Wine is missing. Install a 64-bit Wine build or pass --wine /path/to/wine.")
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise HelperError("A graphical session is required. Run this from your Linux desktop.")

    def setup_prefix(self):
        self.require_wine()
        from .maintenance import mark_installation
        mark_installation(self)
        self.prefix.mkdir(parents=True, exist_ok=True)
        if not (self.prefix / "system.reg").exists():
            self.emit("Creating a dedicated 64-bit Wine prefix…")
            self.run(["wineboot", "-i"], timeout=180, label="Wine prefix initialization")
        registry = self.prefix / "system.reg"
        if registry.exists() and "#arch=win32" in registry.read_text(errors="replace"):
            raise HelperError("Worlds needs a 64-bit prefix. Choose a new --data-dir.")
        self.reg(r"HKCU\Software\Wine", "Version", "win10")
        self.reg(r"HKCU\Software\Wine\AppDefaults\msedgewebview2.exe", "Version", "win7")
        # XWayland works on both X11 and Wayland desktops, including NGM windows.
        if os.environ.get("DISPLAY"):
            self.reg(r"HKCU\Software\Wine\Drivers", "Graphics", "x11")

    def webview_runtime(self) -> Path | None:
        for base in (self.prefix / "drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application",
                     self.prefix / "drive_c/Program Files/Microsoft/EdgeWebView/Application"):
            for candidate in sorted(base.glob("*/msedgewebview2.exe"), reverse=True):
                if pe_machine(candidate) == 0x8664:
                    return candidate
        return None

    def dxvk_ready(self) -> bool:
        try:
            state = json.loads((self.home / "dxvk.json").read_text())
            if not isinstance(state, dict) or state.get("version") != DXVK_VERSION:
                return False
            files = state.get("files")
            if not isinstance(files, dict):
                return False
            for name in DXVK_DLLS:
                path = self.prefix / "drive_c/windows/system32" / name
                if pe_machine(path) != 0x8664:
                    return False
                with path.open("rb") as f:
                    if hashlib.file_digest(f, "sha256").hexdigest() != files.get(name):
                        return False
            return True
        except (OSError, ValueError):
            return False

    def setup_dxvk(self):
        """Worlds and Vuplex must share D3D11 textures across processes.

        WineD3D returns E_NOTIMPL from OpenSharedResource, which Vuplex 4.3.3
        dereferences and crashes. DXVK 2.x uses Proton's older sharing protocol;
        use DXVK 3.1 with upstream Wine's shared-resource implementation.
        """
        if not self.dxvk_ready():
            self.emit(f"Installing DXVK {DXVK_VERSION} for Worlds' embedded browser…")
            archive = self.download(DXVK_URL, self.cache / f"dxvk-{DXVK_VERSION}.tar.gz",
                                    sha256=DXVK_SHA256)
            staged = self.cache / f"dxvk-{DXVK_VERSION}"
            staged.mkdir(parents=True, exist_ok=True)
            hashes = {}
            try:
                with tarfile.open(archive, "r:gz") as package:
                    for name in DXVK_DLLS:
                        self.check_cancel()
                        # Read only exact regular members, never extract paths or links.
                        member = package.getmember(f"dxvk-{DXVK_VERSION}/x64/{name}")
                        if not member.isfile() or not 0 < member.size < 100 * 1024**2:
                            raise HelperError("Invalid DLL in the DXVK archive.")
                        target = staged / name
                        with package.extractfile(member) as source, target.open("wb") as out:
                            shutil.copyfileobj(source, out)
                        if pe_machine(target) != 0x8664:
                            raise HelperError("DXVK archive does not contain valid 64-bit DLLs.")
                        with target.open("rb") as f:
                            hashes[name] = hashlib.file_digest(f, "sha256").hexdigest()
            except (tarfile.TarError, KeyError) as exc:
                raise HelperError("DXVK archive is incomplete or invalid.") from exc
            # Validate both DLLs before changing the prefix. Atomic replacement
            # also avoids following Wine DLL symlinks back into the host system.
            for name in DXVK_DLLS:
                target = self.prefix / "drive_c/windows/system32" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(".dxvk-tmp")
                try:
                    shutil.copy2(staged / name, temp)
                    temp.replace(target)
                finally:
                    temp.unlink(missing_ok=True)
            atomic_json(self.home / "dxvk.json", {"version": DXVK_VERSION,
                        "archive_sha256": DXVK_SHA256, "files": hashes})
        for name in DXVK_DLLS:
            # Undo the original prefix-wide DXVK override. Only the game and
            # its browser need shared textures; Nexon's other processes keep
            # Wine's builtin graphics implementation.
            self.reg(r"HKCU\Software\Wine\DllOverrides", name[:-4], "builtin")
            for app in ("msw.exe", "Vuplex WebView.vuplex"):
                self.reg(r"HKCU\Software\Wine\AppDefaults" + "\\" + app + r"\DllOverrides",
                         name[:-4], "native,builtin")
        # The bundled Chromium 100 also hit a separate cef_shutdown crash in
        # Win10 mode. Scope compatibility to the browser; Worlds stays on Win10.
        self.reg(r"HKCU\Software\Wine\AppDefaults\Vuplex WebView.vuplex", "Version", "win7")

    def setup_webview(self):
        if self.webview_runtime():
            return
        installer = self.cache / "WebView2.exe"
        if not pe_machine(installer):
            self.download(WEBVIEW_URL, installer, executable=True)
        self.emit("Installing Microsoft WebView2 for Nexon's launcher…")
        # Recent Omaha bootstrappers hang under Wine after unpacking the offline
        # payload. Let the publisher's bootstrapper unpack it, then run the
        # publisher's inner installer directly (no patched Microsoft binaries).
        self.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        bootstrap_log = self.logs / "webview-bootstrap.log"
        with os.fdopen(os.open(bootstrap_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "wb") as log:
            bootstrap = subprocess.Popen([self.wine, str(installer), "/silent", "/install"],
                                         env=self.env(), stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)
        try:
            start = time.monotonic()
            while time.monotonic() - start < 240:
                self.check_cancel()
                if self.webview_runtime():
                    break
                inner = self.stage_webview_payload()
                if inner:
                    self.run([str(inner), "--msedgewebview", "--system-level",
                              "--do-not-launch-chrome", "--verbose-logging"],
                             timeout=240, label="WebView2 installation", allowed=(0, 19))
                    break
                if bootstrap.poll() not in (None, 0):
                    raise HelperError(f"WebView2 bootstrap failed ({bootstrap.returncode}). See {bootstrap_log}.")
                self.cancel.wait(1)
            if not self.webview_runtime():
                raise HelperError("WebView2 did not install. Check Wine and retry Install; downloaded files are cached.")
        finally:
            # Stop only the bootstrapper we created, never the prefix's wineserver.
            if bootstrap.poll() is None:
                bootstrap.terminate()
                try:
                    bootstrap.wait(5)
                except subprocess.TimeoutExpired:
                    bootstrap.kill()
                    bootstrap.wait()

    def stage_webview_payload(self) -> Path | None:
        """Copy before launching: Omaha moves/deletes its temporary payloads.

        Only use a completely unpacked executable matching Microsoft's bundled
        OfflineManifest SHA-256 and size. No waiting for Edge's updater service.
        """
        base = self.prefix / "drive_c/Program Files (x86)/Microsoft"
        manifests = [*base.glob("Temp/*/OfflineManifest.gup"),
                     *base.glob("EdgeUpdate/Offline/*/OfflineManifest.gup")]
        for manifest in manifests:
            try:
                tree = ET.parse(manifest)
                package = tree.find(".//app[@appid='{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}']/updatecheck/manifest/packages/package")
                if package is None:
                    continue
                name, sha, size = (package.attrib[k] for k in ("name", "hash_sha256", "size"))
                if not re.fullmatch(r"MicrosoftEdge_X64_[\d.]+\.exe", name) or not re.fullmatch(r"[a-fA-F0-9]{64}", sha):
                    continue
                target = self.cache / name
                if target.is_file() and target.stat().st_size == int(size):
                    with target.open("rb") as f:
                        if hashlib.file_digest(f, "sha256").hexdigest() == sha.lower():
                            return target
                for source in manifest.parent.rglob(name + "*"):
                    if not source.is_file() or source.stat().st_size != int(size):
                        continue
                    temp = target.with_suffix(".part")
                    try:
                        shutil.copyfile(source, temp)
                        with temp.open("rb") as f:
                            valid = hashlib.file_digest(f, "sha256").hexdigest() == sha.lower()
                        if valid and pe_machine(temp) in {0x14C, 0x8664}:
                            temp.replace(target)
                            return target
                    finally:
                        temp.unlink(missing_ok=True)
            except (OSError, ValueError, KeyError, ET.ParseError):
                continue  # The bootstrapper may still be writing or moving files.
        return None

    def setup_ngm(self):
        self.emit("Checking Nexon Game Manager components…")
        metadata = self.metadata(PLATFORM + "Json/NGMUpdateInfo.json")
        for group in ("NGM64_update_list", "NGM_Service_64"):
            entries = metadata.get(group)
            if not isinstance(entries, list) or not entries:
                raise HelperError(f"Nexon metadata is missing {group}.")
            for entry in entries:
                name = entry["name"].replace("\\", "/")
                base = self.ngm_dir
                if name.startswith("$NexonRoot$/"):
                    base = self.ngm_dir.parent
                    name = name[len("$NexonRoot$/"):]
                if name.startswith("/") or any(p in {"", ".", ".."} for p in name.split("/")) or ":" in name:
                    raise HelperError("Invalid path in Nexon component metadata.")
                self.download(entry["url"], base / name, crc=int(entry["crc"]),
                              compressed=entry["url"].endswith(".gz"), executable=True)
        resources = self.metadata(PLATFORM + "Json/NGMResourceInfo.json")
        resource = resources["NGMResource_info"]["en-US"]
        self.download(resource["url"], self.ngm_dir / "NGMResource.dll",
                      crc=int(resource["crc"]), compressed=True, executable=True)
        atomic_json(self.cache / "ngm-resources.json", resources)
        if pe_machine(self.ngm_dir / "NGM64.exe") != 0x8664:
            raise HelperError("The 64-bit Nexon Game Manager is missing.")
        self.reg(r"HKCU\Software\Classes\ngm", "URL Protocol", "")
        command = r'"C:\ProgramData\Nexon\NGM\NGM64.exe" "%1"'
        self.run(["reg", "add", r"HKCU\Software\Classes\ngm\shell\open\command",
                  "/ve", "/t", "REG_SZ", "/d", command, "/f"])

    def processes(self) -> set[str]:
        output = self.run(["tasklist", "/fo", "csv"], timeout=20, label="Wine process check")
        return {row[0].lower() for row in csv.reader(io.StringIO(output))
                if len(row) >= 2 and row[1].strip().isdigit()}

    def ensure_idle(self):
        if not (self.prefix / "system.reg").exists():
            return
        from .session import ACTIVE_APPS, BACKGROUND_APPS, shutdown_prefix
        running = self.processes()
        busy = running & ACTIVE_APPS
        if busy:
            raise HelperError("Close Worlds and Nexon's launcher before changing the installation. "
                              "Still running: " + ", ".join(sorted(busy)) +
                              ". If the windows are already closed, use Stop Worlds or ./msw stop.")
        if running & BACKGROUND_APPS:
            self.emit("Cleaning up background processes left after Worlds closed…")
            shutdown_prefix(self)

    def verify(self) -> dict:
        missing = []
        for name in REQUIRED_PE:
            if pe_machine(self.game_dir / name) != 0x8664:
                missing.append(name + " (64-bit Windows binary)")
        for name in (*REQUIRED_DATA, MANIFEST):
            p = self.game_dir / name
            if not p.is_file() or p.stat().st_size == 0:
                missing.append(name)
        return {"installed": not missing, "game_dir": str(self.game_dir),
                "executable": str(self.game_dir / "msw.exe"), "missing": missing,
                "webview2": bool(self.webview_runtime()),
                "dxvk": self.dxvk_ready(),
                "ngm": pe_machine(self.ngm_dir / "NGM64.exe") == 0x8664}

    def register_game(self, *, complete=True):
        if complete and not self.verify()["installed"]:
            raise HelperError("Worlds' client files are incomplete. Run Install first.")
        root = r"C:\Nexon\MapleStory Worlds"
        self.reg(GAME_REGISTRY, "RootPath", root)
        self.reg(GAME_REGISTRY, "Executable", root + r"\msw.exe")

    def ngm(self, uri: str):
        validate_uri(uri)
        if not self.dxvk_ready():
            self.ensure_idle()  # A graphics migration needs a fresh game process.
        self.setup_dxvk()
        self.run([str(self.ngm_dir / "NGM64.exe"), uri], timeout=3600,
                 label="Nexon installation / update")
        # The NGM process can hand off work to another NGM or launcher process.
        # Ignore Wine services and the game itself; never kill the newly launched game.
        deadline = time.monotonic() + 3600
        while (running := self.processes()) & {"ngm.exe", "ngm64.exe", "nexonlauncher64.exe"}:
            self.check_cancel()
            if time.monotonic() > deadline:
                raise HelperError("Nexon's launcher is still running. Close it and check the log.")
            self.cancel.wait(2)
        if "msw.exe" in running:
            from .session import start_watcher
            start_watcher(self)

    def install(self):
        with self.locked():
            self.require_wine()
            from .maintenance import mark_installation
            mark_installation(self)
            self.ensure_idle()
            self.setup_prefix()
            info = self.metadata(GAME_INFO)
            if info.get("executable_file_name") != "msw.exe" or info.get("game_code") != int(GAME_CODE):
                raise HelperError("Nexon's Worlds metadata changed; refusing to install a different game.")
            self.cache.mkdir(parents=True, exist_ok=True)
            atomic_json(self.cache / "game-info.json", info)
            if shutil.disk_usage(self.home).free < 6 * 1024**3:
                raise HelperError("At least 6 GiB of free space is required for Wine, WebView2, and Worlds.")
            self.setup_webview()
            self.setup_ngm()
            if (self.game_dir / MANIFEST).exists():
                self.register_game(complete=False)
                self.restore()
            else:
                self.emit("Nexon is downloading and verifying MapleStory Worlds. This may take several minutes…")
                self.ngm(make_uri("install"))
            self.finish("install")

    def finish(self, operation):
        report = self.verify()
        if not report["installed"]:
            raise HelperError("Nexon exited without a complete Worlds client. Missing: " + ", ".join(report["missing"]))
        self.register_game()
        atomic_json(self.home / "state.json", {"last_successful_operation": operation,
                    "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "game_dir": str(self.game_dir),
                    "manifest_sha256": hashlib.sha256((self.game_dir / MANIFEST).read_bytes()).hexdigest()})
        self.emit(f"Worlds client verified: {self.game_dir / 'msw.exe'}")

    def update(self):
        with self.locked():
            self.require_wine()
            if not self.game_dir.is_dir():
                raise HelperError("Worlds is not installed yet. Run Install first.")
            from .maintenance import mark_installation
            mark_installation(self)
            self.ensure_idle()
            # Missing/corrupt client files are exactly what restore must repair.
            self.register_game(complete=False)
            self.setup_webview()
            self.setup_ngm()
            self.restore()
            self.finish("update")

    def restore(self):
        self.emit("Nexon is checking and repairing Worlds. The client opens when this finishes…")
        manifest = self.game_dir / MANIFEST
        before = manifest.stat().st_mtime_ns if manifest.exists() else None
        self.ngm(make_uri("restore"))
        after = manifest.stat().st_mtime_ns if manifest.exists() else None
        if before == after and "msw.exe" not in self.processes():
            raise HelperError("Nexon exited, but a completed update could not be confirmed. Retry Update / repair.")

    def play(self):
        """Launch the desktop client through Nexon's normal patch/launch flow."""
        self.open_uri(make_uri("launch"))

    def uninstall(self):
        from .maintenance import uninstall
        uninstall(self)

    def stop(self):
        from .session import stop
        stop(self)

    def winecfg(self):
        with self.locked():
            self.require_wine()
            from .maintenance import mark_installation
            mark_installation(self)
            self.ensure_idle()
            if not (self.prefix / "system.reg").exists():
                self.setup_prefix()
            self.run(["winecfg"], timeout=None, label="Wine settings")

    def open_uri(self, uri):
        validate_uri(uri)
        with self.locked():
            self.require_wine()
            if not self.verify()["installed"]:
                raise HelperError("Install MapleStory Worlds from the helper first, then retry Play Worlds.")
            # Do not replace or persist a browser's short-lived login token.
            self.register_game()
            self.emit("Starting MapleStory Worlds…")
            self.ngm(uri)

    def doctor(self):
        report = self.verify()
        report.update(data_dir=str(self.home), prefix=str(self.prefix),
                      wine=shutil.which(self.wine), display=os.environ.get("DISPLAY"),
                      wayland_display=os.environ.get("WAYLAND_DISPLAY"),
                      log=str(self.logs / "helper.log"))
        return report
