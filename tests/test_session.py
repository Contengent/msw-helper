from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from msw_helper.cli import main
from msw_helper.core import Helper, HelperError, make_uri
from msw_helper.maintenance import mark_installation
from msw_helper.session import ACTIVE_APPS, native_processes, shutdown_prefix, watch_session


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helper = Helper(self.root / 'worlds', emit=lambda _: None)
        mark_installation(self.helper)
        self.helper.prefix.mkdir()
        (self.helper.prefix / 'system.reg').touch()

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
