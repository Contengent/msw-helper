"""Exercise actual GLib desktop parsing without changing the user's associations."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from msw_helper.core import make_uri
from msw_helper.desktop import exec_argument

with tempfile.TemporaryDirectory(prefix='msw desktop % $ " ` \\ ') as temp:
    directory = Path(temp)
    recorder = directory / "record.py"
    result = directory / "arguments.json"
    recorder.write_text("import json, pathlib, sys\n"
                        "pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\n")
    entry = directory / "test.desktop"
    command = " ".join(exec_argument(arg) for arg in (sys.executable, recorder, result))
    entry.write_text("[Desktop Entry]\nType=Application\nName=Worlds protocol test\n"
                     "Terminal=false\nMimeType=x-scheme-handler/ngm;\n"
                     f"Exec={command} %u\n")
    subprocess.run(["desktop-file-validate", str(entry)], check=True)
    uri = make_uri("launch") + "%20-token%3A'A+B%2520%26C'"
    subprocess.run(["gio", "launch", str(entry), uri], check=True)
    deadline = time.monotonic() + 5
    while not result.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert json.loads(result.read_text()) == [uri], "Desktop changed the launch URL"
print("Desktop protocol smoke test passed")
