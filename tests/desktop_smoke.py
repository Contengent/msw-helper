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

# Exercise registration, the actual generated entries, and self-uninstall in an
# isolated XDG tree. No Wine process or user's real associations are involved.
import os
from unittest.mock import patch
from msw_helper.core import Helper
from msw_helper import desktop

with tempfile.TemporaryDirectory(prefix='msw lifecycle % $ " ` \\ ') as temp:
    directory = Path(temp)
    data = directory / 'data'
    config = directory / 'config'
    apps = data / 'applications'
    apps.mkdir(parents=True)
    config.mkdir()
    env = dict(XDG_DATA_HOME=str(data), XDG_CONFIG_HOME=str(config),
               XDG_DATA_DIRS=str(directory / 'system-data'), XDG_CONFIG_DIRS=str(directory / 'system-config'),
               XDG_CURRENT_DESKTOP='', DE='generic')
    with patch.dict(os.environ, env):
        previous = apps / 'previous.desktop'
        previous.write_text('[Desktop Entry]\nType=Application\nName=Previous\nExec=true\n'
                            'MimeType=x-scheme-handler/ngm;\n')
        subprocess.run(['update-desktop-database', str(apps)], check=True)
        subprocess.run(['xdg-mime', 'default', previous.name, desktop.MIME_TYPE], check=True)
        helper = Helper(directory / 'worlds', emit=lambda _: None)
        desktop.register(helper)
        desktop.register(helper)  # Re-registration must keep the original default.
        state = desktop.read_state(helper)
        assert state['previous_ngm'] == previous.name
        for name in desktop.DESKTOP_IDS:
            subprocess.run(['desktop-file-validate', str(apps / name)], check=True)
        launcher = helper.home / 'helper/msw'
        installed_source = launcher.read_text()
        result = directory / 'recorded.json'
        launcher.write_text('import json, pathlib, sys\n' +
                            f'pathlib.Path({str(result)!r}).write_text(json.dumps(sys.argv[1:]))\n')
        for name, command, url in ((desktop.APP_ID, 'play', None),
                                    (desktop.MANAGER_ID, 'gui', None),
                                    (desktop.PROTOCOL_ID, 'open', uri)):
            result.unlink(missing_ok=True)
            subprocess.run(['gio', 'launch', str(apps / name), *([url] if url else [])], check=True)
            deadline = time.monotonic() + 5
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            args = json.loads(result.read_text())
            assert args[:2] == ['--data-dir', str(helper.home)]
            assert args[4:] == [command, *([url] if url else [])], args
        launcher.write_text(installed_source)
        helper.prefix.mkdir()
        (helper.prefix / 'fixture-game').write_text('disposable game data')
        subprocess.run([sys.executable, str(launcher), '--data-dir', str(helper.home),
                        'uninstall', '--yes'], check=True)
        assert not helper.prefix.exists()
        assert not launcher.exists()
        assert previous.exists()
        assert desktop.default_handler(state) == previous.name
        for name in desktop.OWNED_FILES:
            assert not (data / name).exists()
print('Installed desktop entry / protocol / self-uninstall smoke test passed')
