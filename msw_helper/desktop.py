"""Install tracked desktop entries and restore the previous protocol association."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .core import HelperError, atomic_json

APP_ID = "msw-helper-wine.desktop"
MANAGER_ID = "msw-helper-wine-manager.desktop"
PROTOCOL_ID = "msw-helper-wine-ngm.desktop"
GUI_CLASS = "Msw-helper-wine"
MIME_TYPE = "x-scheme-handler/ngm"
RESOURCES = Path(__file__).resolve().parent / "resources"
DESKTOP_IDS = (APP_ID, MANAGER_ID, PROTOCOL_ID)
ICON_NAME = "msw-helper-wine.png"
ICON_PATH = "icons/hicolor/256x256/apps/" + ICON_NAME
LEGACY_ICON_PATH = "icons/hicolor/scalable/apps/msw-helper-wine.svg"
OWNED_FILES = {*(f"applications/{name}" for name in DESKTOP_IDS), ICON_PATH, LEGACY_ICON_PATH}


def exec_argument(value):
    # Desktop Entry has two escaping layers: the string value and Exec quoting.
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise HelperError("Desktop paths cannot contain control characters.")
    value = value.replace("%", "%%")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + value.replace("\\", "\\\\") + '"'


def string_value(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise HelperError("Desktop paths cannot contain control characters.")
    return value.replace("\\", "\\\\").replace(" ", r"\s")


def desktop_files(helper):
    destination = helper.home / "helper"
    wine = shutil.which(helper.wine) or helper.wine
    command = " ".join(exec_argument(arg) for arg in
                       (sys.executable, destination / "msw", "--data-dir", helper.home, "--wine", wine))
    files = {}
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).resolve()
    for name in DESKTOP_IDS:
        template = (RESOURCES / name).read_text(encoding="utf-8")
        files[f"applications/{name}"] = template.replace("@COMMAND@", command).replace(
            "@DATA_DIR@", string_value(helper.home)).replace(
            "@ICON@", string_value(data_home / ICON_PATH)).encode("utf-8")
    files[ICON_PATH] = (RESOURCES / ICON_NAME).read_bytes()
    return files


def mime_env(state):
    return dict(os.environ, XDG_DATA_HOME=state["data_home"], XDG_CONFIG_HOME=state["config_home"])


def default_handler(state):
    return subprocess.run(["xdg-mime", "query", "default", MIME_TYPE], env=mime_env(state),
                          capture_output=True, text=True, check=True).stdout.strip()


def refresh(data_home):
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(Path(data_home) / "applications")], check=True)
    # Entries use the installed icon's absolute path. They do not depend on
    # rebuilding a user's shared icon cache (which can contain invalid assets).


def read_state(helper):
    path = helper.home / "desktop-state.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
        if (state["version"] != 1 or not isinstance(state["files"], dict)
                or not set(state["files"]).issubset(OWNED_FILES)
                or any(not Path(state[key]).is_absolute() for key in ("data_home", "config_home"))):
            raise ValueError("Unexpected desktop manifest")
        return state
    except (ValueError, KeyError, TypeError) as exc:
        raise HelperError(f"Cannot read desktop ownership manifest: {path}") from exc


def legacy_state(helper):
    """Recognize entries from 0.1.x by their exact installed-helper command."""
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).resolve()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).resolve()
    command = " ".join(exec_argument(arg) for arg in
                       (sys.executable, helper.home / "helper/msw", "--data-dir", helper.home,
                        "--wine", shutil.which(helper.wine) or helper.wine))
    files = {}
    for name, suffix in ((APP_ID, "gui"), (PROTOCOL_ID, "open %u")):
        path = data_home / "applications" / name
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            if f"Exec={command} {suffix}" in content.decode("utf-8", errors="replace").splitlines():
                files[f"applications/{name}"] = hashlib.sha256(content).hexdigest()
    return {"version": 1, "data_home": str(data_home), "config_home": str(config_home),
            "files": files, "previous_ngm": ""} if files else None


def register(helper):
    with helper.locked():
        return _register(helper)


def _register(helper):
    if not shutil.which("xdg-mime"):
        raise HelperError("xdg-mime is missing. Install xdg-utils to register website links.")
    from .maintenance import mark_installation
    mark_installation(helper)
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).resolve()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).resolve()
    state = read_state(helper)
    if state and (state["data_home"] != str(data_home) or state["config_home"] != str(config_home)):
        unregister(helper)
        state = None
    if state is None:
        state = {"version": 1, "data_home": str(data_home), "config_home": str(config_home), "files": {}}
        previous = default_handler(state)
        state["previous_ngm"] = previous if previous != PROTOCOL_ID else ""
    source = Path(__file__).resolve().parent
    destination = helper.home / "helper"
    destination.mkdir(parents=True, exist_ok=True)
    # The installed copy survives moving/removing the source checkout.
    if source != destination / "msw_helper":
        shutil.copytree(source, destination / "msw_helper", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # copytree merges updates, so explicitly retire the former bundled artwork.
    (destination / "msw_helper/resources/msw-helper-wine.svg").unlink(missing_ok=True)
    launcher = destination / "msw"
    launcher.write_text("#!/usr/bin/env python3\nfrom msw_helper.cli import main\nraise SystemExit(main())\n")
    launcher.chmod(0o755)
    files = desktop_files(helper)
    obsolete = {name: digest for name, digest in state["files"].items() if name not in files}
    state["files"] = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    # Persist ownership before mutations so an interrupted install is removable.
    atomic_json(helper.home / "desktop-state.json", dict(state, files={**obsolete, **state["files"]}))
    for name, content in files.items():
        target = data_home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".msw-tmp")
        temp.write_bytes(content)
        temp.replace(target)
    for name, digest in obsolete.items():
        target = data_home / name
        if (target.is_file() and not target.is_symlink()
                and hashlib.sha256(target.read_bytes()).hexdigest() == digest):
            target.unlink()
    atomic_json(helper.home / "desktop-state.json", state)
    refresh(data_home)
    result = subprocess.run(["xdg-mime", "default", PROTOCOL_ID, MIME_TYPE], env=mime_env(state),
                            capture_output=True, text=True)
    if result.returncode:
        raise HelperError("Could not register the ngm:// handler: " + result.stderr.strip())
    if default_handler(state) != PROTOCOL_ID:
        raise HelperError("The desktop did not select the Worlds handler. Set it in your default applications.")
    helper.emit("Worlds game, helper menu entries, icon, and ngm:// website links registered.")
    return data_home / "applications" / APP_ID


def remove_mime_references(path):
    """Remove only our handler tokens; retain comments and other associations."""
    if not path.is_file():
        return
    original = path.read_text(encoding="utf-8")
    output = []
    section = ""
    for line in original.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
        if (section in {"[Default Applications]", "[Added Associations]", "[Removed Associations]"}
                and "=" in line and line.split("=", 1)[0].strip() == MIME_TYPE):
            key, value = line.split("=", 1)
            values = [item.strip() for item in value.strip().split(";") if item.strip()]
            if PROTOCOL_ID in values:
                values = [item for item in values if item != PROTOCOL_ID]
                if values:
                    output.append(key + "=" + ";".join(values) + ";\n")
                continue
        output.append(line)
    updated = "".join(output)
    if updated != original:
        # mimeapps.list is often a user-managed symlink; preserve its target.
        target = path.resolve()
        temp = target.with_suffix(target.suffix + ".msw-tmp")
        temp.write_text(updated, encoding="utf-8")
        temp.chmod(target.stat().st_mode & 0o777)
        temp.replace(target)


def unregister(helper):
    """Called under the helper lock. Never remove another prefix's registration."""
    state = read_state(helper) or legacy_state(helper)
    if not state:
        return
    data_home = Path(state["data_home"])
    owned = {}
    marker = "X-MSW-DataDir=" + string_value(helper.home)
    for name, digest in state["files"].items():
        path = data_home / name
        if path.is_file() and not path.is_symlink():
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() == digest:
                owned[name] = path
            elif name.endswith(".desktop") and marker in content.decode("utf-8", errors="replace").splitlines():
                helper.emit(f"Preserving modified desktop file: {path}")
    # A later install can take over the shared desktop IDs and icon.
    # The icon alone is never proof that this registration is still ours.
    if (not any(name.endswith(".desktop") for name in owned)
            and any((data_home / name).exists() for name in state["files"] if name.endswith(".desktop"))):
        return
    if (f"applications/{PROTOCOL_ID}" in owned
            or not (data_home / "applications" / PROTOCOL_ID).exists()):
        if not shutil.which("xdg-mime"):
            raise HelperError("Install xdg-utils before removing the website handler.")
        current = default_handler(state)
        for directory in (Path(state["config_home"]), data_home / "applications"):
            paths = {directory / "mimeapps.list", *directory.glob("*-mimeapps.list")}
            for path in sorted(paths):
                remove_mime_references(path)
        previous = state.get("previous_ngm", "")
        roots = [data_home, *(Path(p) for p in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"))]
        if (current == PROTOCOL_ID and re.fullmatch(r"[\w.-]+\.desktop", previous)
                and previous != PROTOCOL_ID
                and any((root / "applications" / previous).is_file() for root in roots)):
            subprocess.run(["xdg-mime", "default", previous, MIME_TYPE], env=mime_env(state), check=True)
    for path in owned.values():
        path.unlink()
    refresh(data_home)
