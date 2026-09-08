import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from msw_helper.cli import main
from msw_helper.core import Helper, HelperError
from msw_helper import desktop
from msw_helper.maintenance import mark_installation, stop_prefix, uninstall_plan


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'xdg-data'
        self.config = self.root / 'xdg-config'
        self.helper = Helper(self.root / 'worlds', emit=lambda _: None)
        env = patch.dict(os.environ, XDG_DATA_HOME=str(self.data), XDG_CONFIG_HOME=str(self.config))
        env.start()
        self.addCleanup(env.stop)
        self.current = 'previous.desktop'
        self.calls = []
        self.data.joinpath('applications').mkdir(parents=True)
        self.config.mkdir()
        (self.data / 'applications/previous.desktop').write_text('[Desktop Entry]\nName=Previous\n')
        def command(args, **kwargs):
            self.calls.append(args)
            if args[:3] == ['xdg-mime', 'query', 'default']:
                return subprocess.CompletedProcess(args, 0, self.current + '\n', '')
            if args[:2] == ['xdg-mime', 'default']:
                self.current = args[2]
            return subprocess.CompletedProcess(args, 0, '', '')
        mock = patch('msw_helper.desktop.subprocess.run', side_effect=command)
        mock.start()
        self.addCleanup(mock.stop)
        # These tests exercise filesystem behavior; executable availability and
        # actual xdg-mime / GLib dispatch are covered by the desktop smoke test.
        which = patch('msw_helper.desktop.shutil.which', side_effect=lambda name: '/usr/bin/' + name)
        which.start()
        self.addCleanup(which.stop)

    def install(self, helper=None):
        helper = helper or self.helper
        desktop.register(helper)
        for name in ('prefix', 'cache', 'logs'):
            (helper.home / name).mkdir(exist_ok=True)
            (helper.home / name / 'owned-file').write_text('fixture')
        return helper

    def test_registration_installs_tracked_game_and_manager_entries(self):
        self.install()
        state = desktop.read_state(self.helper)
        self.assertEqual(state['previous_ngm'], 'previous.desktop')
        self.assertEqual(set(state['files']), desktop.OWNED_FILES - {desktop.LEGACY_ICON_PATH})
        for path, digest in state['files'].items():
            self.assertEqual(hashlib.sha256((self.data / path).read_bytes()).hexdigest(), digest)
        game = (self.data / 'applications' / desktop.APP_ID).read_text()
        manager = (self.data / 'applications' / desktop.MANAGER_ID).read_text()
        self.assertIn('StartupWMClass=msw.exe\n', game)
        self.assertIn(' play\n', game)
        self.assertIn('StartupWMClass=Msw-helper-wine\n', manager)
        self.assertIn(' gui\n', manager)
        self.assertTrue(os.access(self.helper.home / 'helper/msw', os.X_OK))
        self.assertTrue((self.helper.home / 'helper/msw_helper/resources' / desktop.APP_ID).is_file())
        desktop.register(self.helper)
        self.assertEqual(desktop.read_state(self.helper)['previous_ngm'], 'previous.desktop')

    def test_uninstall_removes_owned_files_and_restores_handler_without_following_wine_links(self):
        self.install()
        unrelated = self.helper.home / 'my-notes.txt'
        unrelated.write_text('keep')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'important').write_text('keep')
        (self.helper.prefix / 'z:').symlink_to(outside, target_is_directory=True)
        mime = self.config / 'mimeapps.list'
        mime.write_text('# keep comment\n[Default Applications]\ntext/plain=editor.desktop;\n'
                        f'{desktop.MIME_TYPE}={desktop.PROTOCOL_ID};previous.desktop;\n'
                        '[Added Associations]\n' + f'{desktop.MIME_TYPE}={desktop.PROTOCOL_ID};previous.desktop;\n')
        self.helper.uninstall()
        self.assertFalse(self.helper.prefix.exists())
        self.assertFalse((self.helper.home / 'helper').exists())
        for path in desktop.OWNED_FILES:
            self.assertFalse((self.data / path).exists())
        self.assertEqual(unrelated.read_text(), 'keep')
        self.assertEqual((outside / 'important').read_text(), 'keep')
        self.assertIn('# keep comment\n', mime.read_text())
        self.assertIn('text/plain=editor.desktop;', mime.read_text())
        self.assertNotIn(desktop.PROTOCOL_ID, mime.read_text())
        self.assertEqual(self.current, 'previous.desktop')
        self.helper.uninstall()  # Idempotent, including when unrelated files remain.

    def test_uninstall_preserves_later_user_default_and_modified_desktop_file(self):
        self.install()
        manager = self.data / 'applications' / desktop.MANAGER_ID
        manager.write_text(manager.read_text() + '# user customization\n')
        self.current = 'another-game.desktop'
        mime = self.config / 'kde-mimeapps.list'
        mime.write_text(f'[Default Applications]\n{desktop.MIME_TYPE}=another-game.desktop;{desktop.PROTOCOL_ID};\n')
        self.helper.uninstall()
        self.assertTrue(manager.exists())
        self.assertIn('# user customization', manager.read_text())
        self.assertEqual(self.current, 'another-game.desktop')
        self.assertIn('another-game.desktop;', mime.read_text())
        self.assertNotIn(desktop.PROTOCOL_ID, mime.read_text())

    def test_missing_desktop_files_do_not_leave_stale_handler_references(self):
        self.install()
        for name in desktop.DESKTOP_IDS:
            (self.data / 'applications' / name).unlink()
        mime = self.config / 'mimeapps.list'
        mime.write_text(f'[Default Applications]\n{desktop.MIME_TYPE}={desktop.PROTOCOL_ID};\n')
        self.helper.uninstall()
        self.assertNotIn(desktop.PROTOCOL_ID, mime.read_text())
        self.assertEqual(self.current, 'previous.desktop')
        self.assertFalse((self.data / desktop.ICON_PATH).exists())

    def test_uninstall_older_prefix_preserves_newer_prefix_desktop_files_and_icon(self):
        self.install()
        other = self.install(Helper(self.root / 'other-worlds', emit=lambda _: None))
        expected = {name: (self.data / name).read_bytes() for name in desktop.read_state(other)['files']}
        self.assertEqual(uninstall_plan(self.helper)['desktop_files'], [])
        self.helper.uninstall()
        for name, content in expected.items():
            self.assertEqual((self.data / name).read_bytes(), content)
        self.assertEqual(self.current, desktop.PROTOCOL_ID)
        self.assertTrue(other.prefix.exists())

    def test_registration_migrates_owned_placeholder_icon(self):
        self.install()
        bundled = self.helper.home / 'helper/msw_helper/resources/msw-helper-wine.svg'
        bundled.write_bytes(b'old bundled placeholder')
        state = desktop.read_state(self.helper)
        legacy = self.data / desktop.LEGACY_ICON_PATH
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'old placeholder icon')
        state['files'][desktop.LEGACY_ICON_PATH] = hashlib.sha256(legacy.read_bytes()).hexdigest()
        (self.helper.home / 'desktop-state.json').write_text(json.dumps(state))
        desktop.register(self.helper)
        self.assertFalse(bundled.exists())
        self.assertFalse(legacy.exists())
        self.assertTrue((self.data / desktop.ICON_PATH).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertNotIn(desktop.LEGACY_ICON_PATH, desktop.read_state(self.helper)['files'])

    def test_unowned_or_linked_prefix_is_not_removed(self):
        self.helper.prefix.mkdir(parents=True)
        (self.helper.prefix / 'unrelated').write_text('keep')
        with self.assertRaisesRegex(HelperError, 'ownership'):
            self.helper.uninstall()
        self.assertTrue((self.helper.prefix / 'unrelated').exists())
        marked = Helper(self.root / 'marked', emit=lambda _: None)
        mark_installation(marked)
        marked.prefix.symlink_to(self.helper.prefix)
        with self.assertRaisesRegex(HelperError, 'symlink'):
            marked.uninstall()
        self.assertTrue((self.helper.prefix / 'unrelated').exists())

    def test_registration_cannot_claim_unrelated_existing_data(self):
        for name in ('prefix', 'cache', 'logs', 'helper'):
            with self.subTest(directory=name):
                helper = Helper(self.root / name, emit=lambda _: None)
                path = helper.home / name / 'unrelated-file'
                path.parent.mkdir(parents=True)
                path.write_text('keep')
                with self.assertRaisesRegex(HelperError, 'Cannot claim existing files'):
                    desktop.register(helper)
                self.assertFalse((helper.home / 'installation.json').exists())
                self.assertEqual(path.read_text(), 'keep')

    def test_install_rejects_unowned_data_before_starting_wine(self):
        self.helper.prefix.mkdir(parents=True)
        (self.helper.prefix / 'unrelated-file').write_text('keep')
        with patch.object(self.helper, 'require_wine'), patch.object(self.helper, 'run') as wine:
            with self.assertRaisesRegex(HelperError, 'Cannot claim existing files'):
                self.helper.install()
            wine.assert_not_called()
        self.assertFalse((self.helper.home / 'installation.json').exists())

    def test_corrupt_legacy_record_is_an_actionable_error(self):
        self.helper.prefix.mkdir(parents=True)
        (self.helper.home / 'state.json').write_text('[]')
        with self.assertRaisesRegex(HelperError, 'ownership'):
            self.helper.uninstall()

    def test_wine_settings_before_install_records_ownership_and_holds_lock(self):
        other = Helper(self.helper.home)
        def wine(args, **kwargs):
            self.assertTrue((self.helper.home / 'installation.json').exists())
            with self.assertRaisesRegex(HelperError, 'in progress'):
                with other.locked():
                    pass
            return ''
        with patch.object(self.helper, 'require_wine'), patch.object(self.helper, 'run', side_effect=wine) as run:
            self.helper.winecfg()
            run.assert_any_call(['winecfg'], timeout=None, label='Wine settings')
        self.assertIn(str(self.helper.prefix), uninstall_plan(self.helper)['paths'])

    def test_busy_game_blocks_removal_before_files_or_services_change(self):
        self.install()
        (self.helper.prefix / 'system.reg').write_text('fixture')
        with patch.object(self.helper, 'require_wine'), \
                patch.object(self.helper, 'processes', return_value={'msw.exe'}), \
                patch('msw_helper.session.subprocess.run') as stop:
            with self.assertRaisesRegex(HelperError, 'Close Worlds'):
                self.helper.uninstall()
            stop.assert_not_called()
        self.assertTrue(self.helper.prefix.exists())
        self.assertTrue((self.data / 'applications' / desktop.APP_ID).exists())

    def test_shutdown_uses_matching_wineserver_and_only_the_owned_prefix(self):
        self.install()
        (self.helper.prefix / 'system.reg').write_text('fixture')
        binary = self.root / 'custom-wine/bin/wine'
        binary.parent.mkdir(parents=True)
        binary.touch()
        binary.with_name('wineserver').touch()
        with patch.object(self.helper, 'require_wine'), patch.object(self.helper, 'ensure_idle'), \
                patch('msw_helper.session.shutil.which', return_value=str(binary)), \
                patch('msw_helper.session.subprocess.run') as stop:
            stop_prefix(self.helper)
            self.assertEqual([call.args[0] for call in stop.call_args_list],
                             [[str(binary.with_name('wineserver')), '-k'], [str(binary.with_name('wineserver')), '-w']])
            for call in stop.call_args_list:
                self.assertEqual(call.kwargs['env']['WINEPREFIX'], str(self.helper.prefix))

    def test_failed_shutdown_preserves_installation(self):
        self.install()
        with patch('msw_helper.maintenance.stop_prefix', side_effect=HelperError('still running')):
            with self.assertRaisesRegex(HelperError, 'still running'):
                self.helper.uninstall()
        self.assertTrue(self.helper.prefix.exists())
        self.assertTrue((self.data / 'applications' / desktop.APP_ID).exists())

    def test_interrupted_legacy_uninstall_remains_resumable(self):
        self.helper.prefix.mkdir(parents=True)
        registry = self.helper.prefix / 'system.reg'
        registry.write_text('legacy fixture')
        (self.helper.home / 'state.json').write_text(json.dumps({'game_dir': str(self.helper.game_dir)}))
        def partial_remove(path):
            registry.unlink()
            raise OSError('interrupted removal')
        with patch('msw_helper.maintenance.stop_prefix'), \
                patch('msw_helper.maintenance.shutil.rmtree', side_effect=partial_remove):
            with self.assertRaisesRegex(OSError, 'interrupted removal'):
                self.helper.uninstall()
        self.assertTrue((self.helper.home / 'installation.json').exists())
        self.helper.uninstall()
        self.assertFalse(self.helper.prefix.exists())

    def test_cli_dry_run_is_read_only_and_removal_requires_confirmation(self):
        self.install()
        with patch('msw_helper.cli.Helper', return_value=self.helper), \
                patch.object(self.helper, 'uninstall') as remove, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['uninstall', '--dry-run']), 0)
            self.assertIn(str(self.helper.prefix), json.loads(output.getvalue())['paths'])
            with patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['uninstall']), 1)
            remove.assert_not_called()
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=EOFError):
                self.assertEqual(main(['uninstall']), 0)
            remove.assert_not_called()
            self.assertEqual(main(['uninstall', '--yes']), 0)
            remove.assert_called_once_with()

    def test_mime_cleanup_preserves_symlink_and_other_types(self):
        actual = self.root / 'managed-mimeapps.list'
        actual.write_text(f'[Default Applications]\n{desktop.MIME_TYPE}={desktop.PROTOCOL_ID};\n'
                          'text/plain=editor.desktop;\n# comment\n')
        link = self.config / 'mimeapps.list'
        link.symlink_to(actual)
        desktop.remove_mime_references(link)
        self.assertTrue(link.is_symlink())
        self.assertEqual(actual.read_text(), '[Default Applications]\ntext/plain=editor.desktop;\n# comment\n')

    def test_manifest_cannot_request_arbitrary_external_file_removal(self):
        self.install()
        state = desktop.read_state(self.helper)
        state['files']['../important'] = 'anything'
        (self.helper.home / 'desktop-state.json').write_text(json.dumps(state))
        with self.assertRaisesRegex(HelperError, 'ownership manifest'):
            self.helper.uninstall()
        self.assertTrue(self.helper.prefix.exists())


if __name__ == '__main__':
    unittest.main()
