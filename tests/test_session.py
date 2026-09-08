from pathlib import Path
import os
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from msw_helper.cli import main
from msw_helper.core import Helper, HelperError, make_uri
from msw_helper.maintenance import mark_installation
from msw_helper.session import (ACTIVE_APPS, QUIT_MESSAGE, QuitLog, finish_quit,
                                native_processes, shutdown_prefix, terminate_processes, watch_session)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helper = Helper(self.root / 'worlds', emit=lambda _: None)
        mark_installation(self.helper)
        self.helper.prefix.mkdir()
        (self.helper.prefix / 'system.reg').touch()

    def player_log(self, content=b'Initialized.\n'):
        path = self.helper.prefix / 'drive_c/users/player/AppData/LocalLow/nexon/MapleStory Worlds/Player.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_quit_log_ignores_previous_quit_and_requires_a_complete_exact_line(self):
        path = self.player_log(QUIT_MESSAGE + b'\n')
        log = QuitLog(self.helper)
        self.assertFalse(log.poll())
        with path.open('ab') as stream:
            stream.write(b'Quoted: ' + QUIT_MESSAGE + b'\n' + QUIT_MESSAGE[:20])
        self.assertFalse(log.poll())
        with path.open('ab') as stream:
            stream.write(QUIT_MESSAGE[20:] + b'\r')
        self.assertFalse(log.poll())
        with path.open('ab') as stream:
            stream.write(b'\n')
        self.assertTrue(log.poll())
        self.assertFalse(log.poll())

    def test_quit_log_handles_creation_rotation_and_truncation(self):
        log = QuitLog(self.helper)
        path = self.player_log(QUIT_MESSAGE + b'\n')
        self.assertTrue(log.poll())
        path.rename(path.with_name('Player-prev.log'))
        path.write_bytes(QUIT_MESSAGE + b'\n' + b'x' * 500)
        self.assertTrue(log.poll())
        path.write_bytes(QUIT_MESSAGE + b'\n')
        self.assertTrue(log.poll())

    def test_confirmed_quit_bounds_a_stuck_game_before_prefix_cleanup(self):
        with patch('msw_helper.session.native_processes', return_value={11: 'msw.exe'}), \
                patch('msw_helper.session.time.monotonic', side_effect=[0, 6]), \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            finish_quit(self.helper)
        shutdown.assert_called_once_with(self.helper)

    def test_watcher_detects_quit_while_game_is_still_running(self):
        path = self.player_log()
        def quit_now(*args):
            with path.open('ab') as stream:
                stream.write(QUIT_MESSAGE + b'\n')
            return False
        active = {10: 'wineserver', 11: 'msw.exe', 12: 'vuplex webview.vuplex'}
        with patch('msw_helper.session.native_processes', return_value=active), \
                patch('msw_helper.session.QuitLog.wait', side_effect=quit_now), \
                patch('msw_helper.session.finish_quit') as finish:
            watch_session(self.helper)
        finish.assert_called_once_with(self.helper)

    def test_watcher_does_not_treat_old_quit_or_cancel_as_confirmation(self):
        for content in (QUIT_MESSAGE + b'\n', b'Quit\nCancel\n'):
            self.player_log(content)
            with self.subTest(content=content), \
                    patch('msw_helper.session.native_processes',
                          side_effect=[{10: 'wineserver', 11: 'msw.exe'}, {}]), \
                    patch('msw_helper.session.QuitLog.wait', return_value=False), \
                    patch('msw_helper.session.finish_quit') as finish, \
                    patch('msw_helper.session.shutdown_prefix') as shutdown:
                watch_session(self.helper)
                finish.assert_not_called()
                shutdown.assert_not_called()

    def test_quit_for_previous_game_cannot_stop_a_replacement(self):
        path = self.player_log()
        waits = []
        def quit_once(*args):
            if not waits:
                with path.open('ab') as stream:
                    stream.write(QUIT_MESSAGE + b'\n')
            waits.append(True)
            return False
        active = {10: 'wineserver', 11: 'msw.exe'}
        replacement = {10: 'wineserver', 20: 'msw.exe'}
        with patch('msw_helper.session.native_processes',
                   side_effect=[active, active, replacement, replacement, {}]), \
                patch('msw_helper.session.QuitLog.wait', side_effect=quit_once), \
                patch('msw_helper.session.finish_quit') as finish, \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            watch_session(self.helper)
        finish.assert_not_called()
        shutdown.assert_not_called()

    def test_confirmed_quit_preserves_other_active_apps_and_operations(self):
        for name in ACTIVE_APPS - {'msw.exe'}:
            active = {10: 'wineserver', 11: 'msw.exe', 12: name}
            with self.subTest(name=name), \
                    patch('msw_helper.session.native_processes', side_effect=[active, {}]), \
                    patch('msw_helper.session.QuitLog.poll', return_value=True), \
                    patch('msw_helper.session.QuitLog.wait', return_value=False), \
                    patch('msw_helper.session.finish_quit') as finish:
                watch_session(self.helper)
                finish.assert_not_called()
        with self.helper.locked(), \
                patch('msw_helper.session.native_processes',
                      side_effect=[{10: 'wineserver', 11: 'msw.exe'}, {}]), \
                patch('msw_helper.session.QuitLog.poll', return_value=True), \
                patch('msw_helper.session.QuitLog.wait', return_value=False), \
                patch('msw_helper.session.finish_quit') as finish:
            watch_session(self.helper)
            finish.assert_not_called()

    def test_watcher_cleans_orphans_after_its_wine_server_exits(self):
        active = {10: 'wineserver', 11: 'msw.exe'}
        orphan = {12: 'vuplex webview.vuplex'}
        with patch('msw_helper.session.native_processes', side_effect=[active, orphan, orphan]), \
                patch('msw_helper.session.QuitLog.wait', return_value=False), \
                patch.object(self.helper, 'processes') as wine, \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            watch_session(self.helper)
        shutdown.assert_called_once_with(self.helper)
        wine.assert_not_called()

    def test_native_termination_rechecks_pid_before_signalling(self):
        for replacement in ({}, {11: 'unrelated.exe'}):
            with self.subTest(replacement=replacement), \
                    patch('msw_helper.session.native_processes',
                          side_effect=[{11: 'msw.exe'}, replacement]), \
                    patch('msw_helper.session.os.pidfd_open', return_value=99), \
                    patch('msw_helper.session.os.close') as close, \
                    patch('msw_helper.session.signal.pidfd_send_signal') as kill:
                terminate_processes(self.helper, {'msw.exe'})
                kill.assert_not_called()
                close.assert_called_once_with(99)

    def test_native_termination_kills_only_selected_processes_in_owned_prefix(self):
        # Real child processes exercise /proc, prefix isolation and pidfds.
        children = []
        try:
            for prefix, name in ((self.helper.prefix, 'msw.exe'),
                                 (self.helper.prefix, 'Vuplex WebView.vuplex'),
                                 (self.root / 'other-prefix', 'Vuplex WebView.vuplex')):
                # exec with the desired argv[0], without a shell or user input.
                child = subprocess.Popen([name, '30'], executable='/usr/bin/sleep',
                                         env=dict(os.environ, WINEPREFIX=str(prefix)))
                children.append(child)
            terminate_processes(self.helper, {'vuplex webview.vuplex'})
            self.assertEqual(children[1].wait(timeout=5), -signal.SIGKILL)
            self.assertIsNone(children[0].poll())
            self.assertIsNone(children[2].poll())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_shutdown_verifies_and_clears_processes_even_when_wineserver_times_out(self):
        for error in (None, subprocess.TimeoutExpired('wineserver', 5)):
            with self.subTest(error=error), \
                    patch('msw_helper.session.shutil.which', return_value='/test/wine'), \
                    patch('msw_helper.session.Path.is_file', return_value=True), \
                    patch('msw_helper.session.subprocess.run', side_effect=error), \
                    patch('msw_helper.session.native_processes',
                          side_effect=[{10: 'wineserver', 12: 'vuplex webview.vuplex'}, {}]), \
                    patch('msw_helper.session.terminate_processes') as terminate:
                shutdown_prefix(self.helper)
                terminate.assert_called_once_with(self.helper, {'wineserver', 'vuplex webview.vuplex'})

    def test_shutdown_does_not_report_success_with_surviving_processes(self):
        with patch('msw_helper.session.shutil.which', return_value='/test/wine'), \
                patch('msw_helper.session.Path.is_file', return_value=True), \
                patch('msw_helper.session.subprocess.run'), \
                patch('msw_helper.session.native_processes', return_value={12: 'vuplex webview.vuplex'}), \
                patch('msw_helper.session.terminate_processes'), \
                patch('msw_helper.session.time.monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(HelperError, 'shutdown is incomplete'):
                shutdown_prefix(self.helper)

    def test_native_scan_scopes_prefix_and_recognizes_renamed_chromium(self):
        proc = self.root / 'proc'
        proc.mkdir()
        for pid, prefix, image, name in (
                (10, self.helper.prefix, b'/usr/bin/wineserver', 'wineserver'),
                (11, self.helper.prefix, br'C:\Nexon\Vuplex WebView.vuplex', 'CrBrowserMain'),
                (12, self.helper.prefix, br'C:\Nexon\msw.exe', 'msw.exe'),
                (13, self.root / 'other-prefix', b'msw.exe', 'msw.exe'),
                (14, Path(str(self.helper.prefix) + '-other'), b'msw.exe', 'msw.exe')):
            process = proc / str(pid)
            process.mkdir()
            (process / 'environ').write_bytes(f'WINEPREFIX={prefix}\0'.encode())
            (process / 'cmdline').write_bytes(image + b'\0private launch argument\0')
            (process / 'comm').write_text(name + '\n')
        (proc / '15').mkdir()  # A process that exits during inspection.
        (proc / 'self').mkdir()
        self.assertEqual(native_processes(self.helper.prefix, proc),
                         {10: 'wineserver', 11: 'vuplex webview.vuplex', 12: 'msw.exe'})

    def test_process_list_ignores_header_and_diagnostics(self):
        output = ('"Image Name","PID","Session Name"\n'
                  '"MSW.exe","32","Console"\n'
                  '"Vuplex WebView.vuplex","48","Console"\n'
                  'wine: warning,details\n')
        with patch.object(self.helper, 'run', return_value=output):
            self.assertEqual(self.helper.processes(), {'msw.exe', 'vuplex webview.vuplex'})

    def test_unreadable_unrelated_process_does_not_disable_scan(self):
        proc = self.root / 'proc'
        for pid in ('10', '11'):
            process = proc / pid
            process.mkdir(parents=True)
            (process / 'environ').write_bytes(f'WINEPREFIX={self.helper.prefix}\0'.encode())
            (process / 'cmdline').write_bytes(b'msw.exe\0')
            (process / 'comm').write_text('msw.exe\n')
        read_bytes = Path.read_bytes
        def read(path):
            if path == proc / '10/environ':
                raise PermissionError('Non-dumpable desktop service')
            return read_bytes(path)
        with patch.object(Path, 'read_bytes', read):
            self.assertEqual(native_processes(self.helper.prefix, proc), {11: 'msw.exe'})

    def test_idle_check_preserves_each_active_app(self):
        for name in ACTIVE_APPS:
            with self.subTest(name=name), \
                    patch.object(self.helper, 'processes', return_value={name, 'vuplex webview.vuplex'}), \
                    patch('msw_helper.session.shutdown_prefix') as shutdown:
                with self.assertRaisesRegex(HelperError, 'Stop Worlds'):
                    self.helper.ensure_idle()
                shutdown.assert_not_called()

    def test_idle_check_cleans_leftover_browser(self):
        with patch.object(self.helper, 'processes', return_value={'vuplex webview.vuplex'}), \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            self.helper.ensure_idle()
            shutdown.assert_called_once_with(self.helper)

    def test_stop_closes_game_before_shutdown(self):
        events = []
        with patch.object(self.helper, 'require_wine'), \
                patch('msw_helper.session.native_processes', side_effect=[{10: 'msw.exe'}, {}]), \
                patch.object(self.helper, 'run', side_effect=lambda args, **kw: events.append(args)), \
                patch('msw_helper.session.shutdown_prefix', side_effect=lambda h: events.append('shutdown')):
            self.helper.stop()
        self.assertEqual(events, [['taskkill', '/im', 'msw.exe'], 'shutdown'])

    def test_stop_eventually_shuts_down_stuck_game(self):
        with patch.object(self.helper, 'require_wine'), \
                patch('msw_helper.session.native_processes', return_value={10: 'msw.exe'}), \
                patch.object(self.helper, 'run'), \
                patch('msw_helper.session.time.monotonic', side_effect=[0, 9]), \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            self.helper.stop()
            shutdown.assert_called_once_with(self.helper)

    def test_stop_falls_back_when_graceful_close_times_out(self):
        with patch.object(self.helper, 'require_wine'), \
                patch('msw_helper.session.native_processes', return_value={10: 'msw.exe'}), \
                patch.object(self.helper, 'run', side_effect=HelperError('Closing Worlds timed out')), \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            self.helper.stop()
            shutdown.assert_called_once_with(self.helper)

    def test_unowned_prefix_cannot_be_stopped(self):
        (self.helper.home / 'installation.json').unlink()
        with patch.object(self.helper, 'require_wine') as wine, \
                patch('msw_helper.session.subprocess.run') as run:
            with self.assertRaisesRegex(HelperError, 'ownership'):
                self.helper.stop()
            with self.assertRaisesRegex(HelperError, 'ownership'):
                shutdown_prefix(self.helper)
            wine.assert_not_called()
            run.assert_not_called()

    def test_stop_without_prefix_does_not_start_wine(self):
        (self.helper.prefix / 'system.reg').unlink()
        with patch.object(self.helper, 'require_wine') as wine, \
                patch('msw_helper.session.subprocess.run') as run:
            self.helper.stop()
            wine.assert_not_called()
            run.assert_not_called()

    def test_watcher_waits_for_game_then_cleans_background(self):
        active = {10: 'wineserver', 11: 'msw.exe', 12: 'vuplex webview.vuplex'}
        idle = {10: 'wineserver', 12: 'vuplex webview.vuplex'}
        with patch('msw_helper.session.native_processes', side_effect=[active, idle, idle]), \
                patch('msw_helper.session.time.sleep'), \
                patch.object(self.helper, 'processes', return_value={'vuplex webview.vuplex'}), \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            watch_session(self.helper)
            shutdown.assert_called_once_with(self.helper)

    def test_watcher_rechecks_for_active_apps_through_wine(self):
        idle = {10: 'wineserver', 12: 'vuplex webview.vuplex'}
        with patch('msw_helper.session.native_processes', side_effect=[idle, idle, {}]), \
                patch('msw_helper.session.time.sleep'), \
                patch.object(self.helper, 'processes', return_value={'msw.exe'}), \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            watch_session(self.helper)
            shutdown.assert_not_called()

    def test_watcher_retries_a_failed_wine_check(self):
        idle = {10: 'wineserver', 12: 'vuplex webview.vuplex'}
        with patch('msw_helper.session.native_processes', return_value=idle), \
                patch('msw_helper.session.time.sleep'), \
                patch.object(self.helper, 'processes', side_effect=[HelperError('Wine timed out'), set()]), \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            watch_session(self.helper)
            shutdown.assert_called_once_with(self.helper)

    def test_watcher_retries_a_failed_shutdown(self):
        idle = {10: 'wineserver'}
        with patch('msw_helper.session.native_processes', return_value=idle), \
                patch('msw_helper.session.time.sleep'), \
                patch.object(self.helper, 'processes', return_value=set()), \
                patch('msw_helper.session.shutdown_prefix',
                      side_effect=[subprocess.TimeoutExpired('wineserver', 30), None]) as shutdown:
            watch_session(self.helper)
            self.assertEqual(shutdown.call_count, 2)

    def test_watcher_leaves_stopped_or_replaced_server_alone(self):
        active = {10: 'wineserver', 11: 'msw.exe'}
        for replacement in ({}, {20: 'wineserver'}):
            with self.subTest(replacement=replacement), \
                    patch('msw_helper.session.native_processes', side_effect=[active, replacement]), \
                    patch('msw_helper.session.time.sleep'), \
                    patch.object(self.helper, 'processes') as wine, \
                    patch('msw_helper.session.shutdown_prefix') as shutdown:
                watch_session(self.helper)
                wine.assert_not_called()
                shutdown.assert_not_called()

    def test_watcher_rechecks_server_after_acquiring_lock(self):
        with patch('msw_helper.session.native_processes', side_effect=[{10: 'wineserver'}, {20: 'wineserver'}]), \
                patch.object(self.helper, 'processes') as wine, \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            watch_session(self.helper)
            wine.assert_not_called()
            shutdown.assert_not_called()

    def test_watcher_does_not_interrupt_an_operation(self):
        with self.helper.locked(), \
                patch('msw_helper.session.native_processes', side_effect=[{10: 'wineserver'}, {}]), \
                patch('msw_helper.session.time.sleep'), \
                patch.object(self.helper, 'processes') as wine, \
                patch('msw_helper.session.shutdown_prefix') as shutdown:
            watch_session(self.helper)
            wine.assert_not_called()
            shutdown.assert_not_called()

    def test_ngm_starts_watcher_after_launch_handoff(self):
        with patch.object(self.helper, 'dxvk_ready', return_value=True), \
                patch.object(self.helper, 'setup_dxvk'), patch.object(self.helper, 'run'), \
                patch.object(self.helper, 'processes', side_effect=[{'ngm64.exe'}, {'msw.exe'}]), \
                patch.object(self.helper.cancel, 'wait'), \
                patch('msw_helper.session.start_watcher') as watcher:
            self.helper.ngm(make_uri('launch'))
            watcher.assert_called_once_with(self.helper)

    def test_stop_command_dispatches_to_selected_installation(self):
        with patch('msw_helper.cli.Helper', return_value=self.helper) as helper, \
                patch.object(self.helper, 'stop') as stop:
            self.assertEqual(main(['stop', '--data-dir', str(self.helper.home)]), 0)
            helper.assert_called_once_with(self.helper.home, None)
            stop.assert_called_once_with()
