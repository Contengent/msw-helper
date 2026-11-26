"""Finish a Worlds session without touching another application's Wine prefix."""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .core import Helper, HelperError, OperationInProgress

ACTIVE_APPS = {"msw.exe", "ngm.exe", "ngm64.exe", "nexonlauncher64.exe", "winecfg.exe", "regedit.exe"}
BACKGROUND_APPS = {"vuplex webview.vuplex", "msedgewebview2.exe", "microsoftedgeupdate.exe"}
QUIT_MESSAGE = b"Input System module state changed to: ShutdownInProgress."


class QuitLog:
    """Observe new Unity quit messages without logging any game/account data."""

    def __init__(self, helper):
        self.users = helper.prefix / "drive_c/users"
        self.files = {}
        self.poll(initial=True)

    def poll(self, *, initial=False):
        found = False
        for path in self.users.glob("*/AppData/LocalLow/nexon/MapleStory Worlds/Player.log"):
            try:
                with path.open("rb") as log:
                    info = os.fstat(log.fileno())
                    identity = (info.st_dev, info.st_ino)
                    old_identity, offset, partial = self.files.get(path, (None, 0, b""))
                    if initial:
                        # An earlier session's quit must never stop a new game.
                        self.files[path] = (identity, info.st_size, b"")
                        continue
                    if old_identity != identity or info.st_size < offset:
                        offset, partial = 0, b""
                    log.seek(offset)
                    data = log.read(65536)
                    lines = (partial + data).split(b"\n")
                    found |= any(line.rstrip(b"\r") == QUIT_MESSAGE for line in lines[:-1])
                    self.files[path] = (identity, log.tell(), lines[-1][-256:])
            except (FileNotFoundError, PermissionError):
                continue
        return found

    def wait(self, timeout):
        if not self.files:
            time.sleep(timeout)
            return self.poll()
        # Only read the small log tail frequently; /proc scans remain infrequent.
        deadline = time.monotonic() + timeout
        while True:
            if self.poll():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))


def terminate_processes(helper, names):
    """End selected processes in the owned prefix using stable kernel handles."""
    from .maintenance import validate_ownership
    validate_ownership(helper)
    for pid, name in native_processes(helper.prefix).items():
        if name not in names or pid == os.getpid():
            continue
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        try:
            # A recycled PID between inspection and pidfd_open must not target
            # an unrelated process. Once opened, the handle cannot change owner.
            if native_processes(helper.prefix).get(pid) == name:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            os.close(fd)


def finish_quit(helper):
    """Allow normal teardown, then finish even if the game or CEF stalls."""
    helper.emit("Worlds confirmed Quit. Waiting briefly for it to finish…")
    deadline = time.monotonic() + 5
    while "msw.exe" in native_processes(helper.prefix).values():
        if time.monotonic() >= deadline:
            helper.emit("Finishing the remaining Worlds shutdown…")
            break
        time.sleep(0.1)
    shutdown_prefix(helper)


def native_processes(prefix, proc_root=Path("/proc")):
    """Read only this user's processes in this exact prefix; never expose args.

    Chromium changes its Linux process name to CrBrowserMain/CrGpuMain, while
    argv[0] still names Vuplex. A native check avoids starting Wine merely to
    discover that it already stopped.
    """
    expected = ("WINEPREFIX=" + str(prefix)).encode()
    result = {}
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            if expected not in (process / "environ").read_bytes().split(b"\0"):
                continue
            image = (process / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            image = image.replace("\\", "/").rsplit("/", 1)[-1].lower()
            name = (process / "comm").read_text().strip().lower()
            result[int(process.name)] = image if image.endswith((".exe", ".vuplex")) else name
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # Same-user services such as ssh-agent can deliberately prohibit
            # reading /proc/PID/environ. They must not disable game cleanup.
            # The watcher also checks Wine's process list before stopping it.
            continue
    return result


# Wine's own native processes, and CEF's crash handler (its comm is truncated).
WINE_NATIVE = {"wineserver", "wine", "wine64", "wine-preloader", "wine64-preloader", "winedbg", "crashpad_handle"}


def is_wine_process(name):
    return name.endswith((".exe", ".vuplex", ".aes")) or name in WINE_NATIVE


def prefix_processes(prefix, proc_root=Path("/proc")):
    """Processes that actually run in the prefix: Windows images and Wine itself.

    A native app the game opened — Firefox via winebrowser/xdg-open, for one —
    inherits WINEPREFIX but is not part of the session. Counting it would make
    cleanup retry forever and hold the operation lock, blocking Play and updates.
    """
    return {pid: name for pid, name in native_processes(prefix, proc_root).items() if is_wine_process(name)}


def shutdown_prefix(helper):
    from .maintenance import validate_ownership
    validate_ownership(helper)
    wine = shutil.which(helper.wine)
    if not wine:
        raise HelperError("Wine is missing; cannot stop the Worlds prefix.")
    candidates = (Path(wine).absolute().with_name("wineserver"), Path(wine).resolve().with_name("wineserver"))
    server = next((path for path in candidates if path.is_file()), None)
    if server is None:
        raise HelperError(f"Cannot find the matching wineserver beside {wine}.")
    helper.emit("Stopping Wine background services for Worlds…")
    failure = None
    try:
        subprocess.run([str(server), "-k"], env=helper.env(), check=True, timeout=5)
        subprocess.run([str(server), "-w"], env=helper.env(), check=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        failure = exc
        helper.emit("Wine shutdown did not finish; checking for stuck Worlds processes…")
    remaining = prefix_processes(helper.prefix)
    if remaining:
        # Crash handlers and browser children can outlive Wine's server. Limit
        # the fallback to known Wine images and their native crash handler.
        terminate_processes(helper, set(remaining.values()))
        deadline = time.monotonic() + 2
        while (remaining := prefix_processes(helper.prefix)) and time.monotonic() < deadline:
            time.sleep(0.05)
        if remaining:
            raise HelperError("Worlds shutdown is incomplete. Still running: " +
                              ", ".join(sorted(set(remaining.values()))))
    elif failure:
        # A failed command and an empty /proc view are not proof of success
        # (for example, when procfs hides the real session).
        raise failure
    helper.emit("Worlds session fully stopped.")


def stop(helper):
    with helper.locked():
        if not (helper.prefix / "system.reg").exists():
            helper.emit("Worlds is not running.")
            return
        from .maintenance import validate_ownership
        validate_ownership(helper)
        helper.require_wine()
        try:
            running = set(native_processes(helper.prefix).values()) & ACTIVE_APPS
            if running:
                helper.emit("Closing Worlds…")
                # Post WM_CLOSE once for all apps, then give them a shared
                # deadline. Do not repeatedly start tasklist while Wine exits.
                args = ["taskkill"]
                for name in sorted(running):
                    args.extend(["/im", name])
                helper.run(args, timeout=3, label="Closing Worlds", allowed=(0, 1, 128))
                deadline = time.monotonic() + 8
                while set(native_processes(helper.prefix).values()) & ACTIVE_APPS:
                    if time.monotonic() >= deadline:
                        helper.emit("Stopping the remaining stuck Worlds processes…")
                        break
                    time.sleep(0.2)
        except (HelperError, OSError, subprocess.SubprocessError) as exc:
            # An explicit stop still needs to finish when Wine's close command
            # fails or hangs. Ownership was checked before attempting it.
            helper.emit(f"Worlds did not close normally; stopping its Wine session: {exc}")
        shutdown_prefix(helper)
        helper.emit("Worlds and its background processes are stopped.")


def watch_session(helper, interval=2):
    server_pid = None
    game_pids = None
    quit_log = QuitLog(helper)
    quitting = False
    while (helper.prefix / "system.reg").exists():
        native = prefix_processes(helper.prefix)
        if not native:
            return
        servers = {pid for pid, name in native.items() if name == "wineserver"}
        if servers and server_pid is not None and server_pid not in servers:
            return  # Never follow a new session.
        if server_pid is None:
            if not servers:
                return
            server_pid = next(iter(servers))
        current_game = {pid for pid, name in native.items() if name == "msw.exe"}
        if game_pids is not None and current_game and current_game != game_pids:
            quit_log = QuitLog(helper)
            quitting = False
        game_pids = current_game
        quitting |= quit_log.poll()
        active = set(native.values()) & ACTIVE_APPS
        if not servers or not active or (quitting and active <= {"msw.exe"}):
            try:
                with helper.locked():
                    if not (helper.prefix / "system.reg").exists():
                        return
                    native = prefix_processes(helper.prefix)
                    current_servers = {pid for pid, name in native.items() if name == "wineserver"}
                    if current_servers and server_pid not in current_servers:
                        return
                    if not current_servers:
                        # The observed server exited, but native browser/crash
                        # handlers can survive it. Never start Wine to list them.
                        if native:
                            shutdown_prefix(helper)
                        return
                    active = set(native.values()) & ACTIVE_APPS
                    if quitting and active <= {"msw.exe"} and {
                            pid for pid, name in native.items() if name == "msw.exe"} <= game_pids:
                        finish_quit(helper)
                        return
                    if active:
                        continue
                    # Recheck through Wine under the operation lock. A renamed
                    # Unix process or a simultaneous new launch must not be killed.
                    if not helper.processes() & ACTIVE_APPS:
                        helper.emit("Worlds closed. Cleaning up its background processes…")
                        shutdown_prefix(helper)
                        return
            except OperationInProgress:
                pass
            except (HelperError, OSError, subprocess.SubprocessError) as exc:
                helper.emit(f"Session cleanup will retry: {exc}")
        quitting |= quit_log.wait(interval)


def start_watcher(helper):
    # A separate process keeps cleanup working after the CLI or helper GUI exits.
    # It receives paths only, never a website URL or account ticket.
    env = os.environ.copy()
    env.pop("WINEPREFIX", None)  # The watcher itself is outside Wine's session.
    source = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = source + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    helper.logs.mkdir(parents=True, exist_ok=True)
    path = helper.logs / "session.log"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a") as log:
        subprocess.Popen([sys.executable, "-m", "msw_helper.session", "--data-dir", str(helper.home),
                          "--wine", helper.wine], env=env, stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--wine", required=True)
    args = parser.parse_args()
    helper = Helper(args.data_dir, args.wine)
    if not (helper.prefix / "system.reg").exists():
        return
    try:
        with (helper.home / "session.lock").open("a") as lock:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        return
                    time.sleep(0.2)
            watch_session(helper)
    except (HelperError, OSError, subprocess.SubprocessError) as exc:
        print(f"Session cleanup: {exc}", flush=True)


if __name__ == "__main__":
    main()
