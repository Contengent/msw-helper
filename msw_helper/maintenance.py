"""Remove only this helper's dedicated installation and tracked integration."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from .core import HelperError, atomic_json

MARKER = "installation.json"
# Never recursively delete the user-selected data directory itself. It may also
# contain unrelated files. Wine's drive links are unlinked, not traversed.
DIRECTORIES = ("prefix", "cache", "logs", "helper")
FILES = ("state.json", "dxvk.json", "desktop-state.json", MARKER)


def check_location(helper):
    protected = {Path("/"), Path.home(), Path.home() / ".local", Path.home() / ".local/share",
                 Path.home() / ".config", Path(__file__).resolve().parent.parent}
    if helper.home in protected:
        raise HelperError("Use a dedicated Worlds data directory, not your home or source directory.")
    if helper.prefix.is_symlink():
        raise HelperError("Refusing to remove or manage a Wine prefix that is a symlink.")


def mark_installation(helper):
    check_location(helper)
    marker = helper.home / MARKER
    if marker.exists():
        validate_ownership(helper)
    else:
        # Registration must not turn arbitrary pre-existing prefix/cache/log
        # directories into files that a later uninstall is allowed to delete.
        existing = [helper.home / name for name in (*DIRECTORIES, *FILES)
                    if (helper.home / name).exists() or (helper.home / name).is_symlink()]
        if existing:
            try:
                validate_ownership(helper)  # Upgrade a recorded legacy install.
            except HelperError as exc:
                raise HelperError(f"Cannot claim existing files in {helper.home}: no valid Worlds ownership record. "
                                  "Choose an empty --data-dir; the existing files have been left in place.") from exc
        atomic_json(marker, {"application": "msw-helper-wine", "version": 1, "data_dir": str(helper.home)})


def validate_ownership(helper):
    check_location(helper)
    try:
        marker = helper.home / MARKER
        if marker.exists():
            state = json.loads(marker.read_text())
            if state == {"application": "msw-helper-wine", "version": 1, "data_dir": str(helper.home)}:
                return
        else:
            # Successful installs made before ownership tracking already record
            # their exact game path. Accept those for in-place migration.
            state = json.loads((helper.home / "state.json").read_text())
            if (isinstance(state, dict) and state.get("game_dir") == str(helper.game_dir)
                    and (helper.prefix / "system.reg").is_file()):
                return
    except (OSError, ValueError, TypeError):
        pass
    raise HelperError(f"No valid Worlds installation ownership record in {helper.home}. Refusing removal.")


def uninstall_plan(helper):
    paths = [helper.home / name for name in (*DIRECTORIES, *FILES)
             if (helper.home / name).exists() or (helper.home / name).is_symlink()]
    if not paths:
        return {"data_dir": str(helper.home), "paths": [], "desktop_files": []}
    validate_ownership(helper)
    from .desktop import read_state, legacy_state
    state = read_state(helper) or legacy_state(helper)
    desktop = []
    if state:
        for name, digest in state["files"].items():
            path = Path(state["data_home"]) / name
            if path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                desktop.append(str(path))
        if (not any(path.endswith(".desktop") for path in desktop)
                and any((Path(state["data_home"]) / name).exists()
                        for name in state["files"] if name.endswith(".desktop"))):
            desktop = []
    return {"data_dir": str(helper.home), "paths": [str(path) for path in paths], "desktop_files": desktop}


def stop_prefix(helper):
    if not (helper.prefix / "system.reg").exists():
        return
    helper.require_wine()
    helper.ensure_idle()
    wine = Path(shutil.which(helper.wine)).resolve()
    server = wine.with_name("wineserver")
    if not server.is_file():
        raise HelperError(f"Cannot find the matching wineserver beside {wine}. Close the prefix before uninstalling.")
    helper.emit("Stopping background Wine services in the Worlds prefix…")
    subprocess.run([str(server), "-k"], env=helper.env(), check=True, timeout=15)
    subprocess.run([str(server), "-w"], env=helper.env(), check=True, timeout=30)


def uninstall(helper):
    with helper.locked():
        plan = uninstall_plan(helper)
        if not plan["paths"]:
            helper.emit("Worlds is already uninstalled.")
            return
        helper.check_cancel()
        # Upgrade legacy ownership before removing any prefix files so a failed
        # removal remains resumable even after system.reg has been deleted.
        mark_installation(helper)
        stop_prefix(helper)
        from .desktop import unregister
        unregister(helper)
        # Ownership records go last, allowing an interrupted removal to resume.
        for name in (*DIRECTORIES, *FILES):
            path = helper.home / name
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                helper.emit(f"Removing {path}…")
                shutil.rmtree(path)
        helper.emit("Worlds uninstalled. Unrelated files and the operation lock were preserved.")
