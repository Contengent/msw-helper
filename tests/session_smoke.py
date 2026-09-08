"""Exercise a frozen Wine client in a disposable prefix; requires Wine, clang and lld-link."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from msw_helper.core import Helper
from msw_helper.maintenance import mark_installation
from msw_helper.session import QUIT_MESSAGE, native_processes, start_watcher, terminate_processes


def wait_until(check, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError('Timed out waiting for the test session')


with tempfile.TemporaryDirectory(prefix='msw-shutdown-test-') as temp:
    root = Path(temp)
    kernel = Path('/usr/lib/wine/x86_64-windows/libkernel32.a')
    if not kernel.is_file():
        raise SystemExit('This smoke test needs Wine development libraries at ' + str(kernel))
    source = root / 'client.c'
    source.write_text('__declspec(dllimport) void __stdcall Sleep(unsigned long);\n'
                      '__declspec(dllimport) void __stdcall ExitProcess(unsigned int);\n'
                      'void mainCRTStartup(void) { Sleep(60000); ExitProcess(0); }\n')
    subprocess.run(['clang', '--target=x86_64-w64-windows-gnu', '-c', str(source),
                    '-o', str(root / 'client.obj')], check=True, timeout=20)
    subprocess.run(['lld-link', '/entry:mainCRTStartup', '/subsystem:console', '/nodefaultlib',
                    '/out:' + str(root / 'msw.exe'), str(root / 'client.obj'), str(kernel)],
                   check=True, timeout=20)
    helper = Helper(root / 'worlds', emit=lambda message: print(message, flush=True))
    mark_installation(helper)
    helper.prefix.mkdir()
    children = []
    try:
        with (root / 'wine.log').open('w') as log:
            children.append(subprocess.Popen([helper.wine, str(root / 'msw.exe')],
                                             env=helper.env(), stdout=log, stderr=log))
        wait_until(lambda: (helper.prefix / 'system.reg').exists()
                   and 'msw.exe' in native_processes(helper.prefix).values(), 60)
        time.sleep(0.5)
        game_pid = next(pid for pid, name in native_processes(helper.prefix).items() if name == 'msw.exe')
        # Stop the client at the kernel level: it cannot respond to Wine's close.
        os.kill(game_pid, signal.SIGSTOP)
        for prefix in (helper.prefix, root / 'unrelated-prefix'):
            children.append(subprocess.Popen(['Vuplex WebView.vuplex', '60'],
                                             executable=shutil.which('sleep'),
                                             env=dict(os.environ, WINEPREFIX=str(prefix))))
        player_log = helper.prefix / 'drive_c/users/test/AppData/LocalLow/nexon/MapleStory Worlds/Player.log'
        player_log.parent.mkdir(parents=True)
        player_log.write_bytes(b'Initialized.\n')
        start_watcher(helper)
        # Allow the detached watcher to baseline the log before emitting Quit.
        time.sleep(1)
        started = time.monotonic()
        with player_log.open('ab') as stream:
            stream.write(QUIT_MESSAGE + b'\n')
        wait_until(lambda: not native_processes(helper.prefix), 20)
        wait_until(lambda: 'Worlds session fully stopped.' in
                   (helper.logs / 'session.log').read_text(), 3)
        report = (helper.logs / 'session.log').read_text()
        assert 'Worlds confirmed Quit.' in report, report
        assert children[-1].poll() is None, 'Another prefix was interrupted'
        print(report, end='')
        print(f'Frozen Wine client and orphan browser cleared in {time.monotonic() - started:.1f}s; '
              'unrelated prefix survived.', flush=True)
    finally:
        # Always clean up only this disposable prefix and our own native fixtures.
        subprocess.run(['wineserver', '-k'], env=helper.env(), timeout=5, check=False)
        terminate_processes(helper, set(native_processes(helper.prefix).values()))
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
