"""Per-user desktop entries with correctly quoted arguments and a stable copy."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .core import HelperError

APP_ID = "msw-helper-wine.desktop"
PROTOCOL_ID = "msw-helper-wine-ngm.desktop"


def exec_argument(value):
    # Desktop Entry has two escaping layers: the string value and Exec quoting.
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise HelperError("Desktop paths cannot contain control characters.")
    value = value.replace("%", "%%")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + value.replace("\\", "\\\\") + '"'


def register(helper):
    if not shutil.which("xdg-mime"):
        raise HelperError("xdg-mime is missing. Install xdg-utils to register website links.")
    source = Path(__file__).resolve().parent
    destination = helper.home / "helper"
    destination.mkdir(parents=True, exist_ok=True)
    # Keep entries working if the source checkout moves. Re-register after pulling
    # helper updates. Running from this installed copy is also supported.
    if source != destination / "msw_helper":
        shutil.copytree(source, destination / "msw_helper", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (destination / "msw").write_text(
        "from msw_helper.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    apps = data_home / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    wine = shutil.which(helper.wine) or helper.wine
    command = " ".join(exec_argument(arg) for arg in
                       (sys.executable, destination / "msw", "--data-dir", helper.home, "--wine", wine))
    common = ("[Desktop Entry]\nType=Application\nVersion=1.0\nTerminal=false\n"
              "Icon=applications-games\nCategories=Game;\n")
    (apps / APP_ID).write_text(common + "Name=MapleStory Worlds (Wine)\n"
                              "Comment=Install, update and play MapleStory Worlds\n"
                              f"Exec={command} gui\n", encoding="utf-8")
    (apps / PROTOCOL_ID).write_text(common + "Name=MapleStory Worlds website links\n"
                                   "NoDisplay=true\nMimeType=x-scheme-handler/ngm;\n"
                                   f"Exec={command} open %u\n", encoding="utf-8")
    result = subprocess.run(["xdg-mime", "default", PROTOCOL_ID, "x-scheme-handler/ngm"],
                            capture_output=True, text=True)
    if result.returncode:
        raise HelperError("Could not register the ngm:// handler: " + result.stderr.strip())
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(apps)], check=True)
    actual = subprocess.run(["xdg-mime", "query", "default", "x-scheme-handler/ngm"],
                            capture_output=True, text=True, check=True).stdout.strip()
    if actual != PROTOCOL_ID:
        raise HelperError("The desktop did not select the Worlds handler. Set it in your default applications.")
    helper.emit("Application menu entry and ngm:// website links registered.")
    return apps / APP_ID
