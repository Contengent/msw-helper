import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

from msw_helper.cli import parser
from msw_helper.core import (Cancelled, DXVK_DLLS, DXVK_URL, DXVK_VERSION, Helper, HelperError, MANIFEST, REQUIRED_DATA,
                             REQUIRED_PE, make_uri, official_url, redact, validate_uri)
from msw_helper.desktop import exec_argument


def pe(path, machine=0x8664):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = bytearray(512)
    content[:2] = b"MZ"
    struct.pack_into("<I", content, 0x3C, 128)
    content[128:132] = b"PE\0\0"
    struct.pack_into("<H", content, 132, machine)
    path.write_bytes(content)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.helper = Helper(self.root, emit=lambda _: None)

    def client(self):
        for name in REQUIRED_PE:
            pe(self.helper.game_dir / name)
        for name in (*REQUIRED_DATA, MANIFEST):
            path = self.helper.game_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture game data")

    def test_ngm_alone_is_never_an_installed_game(self):
        pe(self.helper.ngm_dir / "NGM64.exe")
        report = self.helper.verify()
        self.assertTrue(report["ngm"])
        self.assertFalse(report["installed"])
        with self.assertRaises(HelperError):
            self.helper.finish("install")
        self.assertFalse((self.root / "state.json").exists())

    def test_missing_and_wrong_architecture_client_files_fail_verification(self):
        self.client()
        self.assertTrue(self.helper.verify()["installed"])
        pe(self.helper.game_dir / "GameAssembly.dll", machine=0x14C)
        self.assertFalse(self.helper.verify()["installed"])
        pe(self.helper.game_dir / "GameAssembly.dll")
        (self.helper.game_dir / REQUIRED_DATA[0]).unlink()
        self.assertFalse(self.helper.verify()["installed"])

    def test_atomic_state_only_after_verified_files(self):
        self.client()
        with patch.object(self.helper, "reg") as registry:
            self.helper.finish("install")
        self.assertEqual(registry.call_count, 2)
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual(state["last_successful_operation"], "install")
        self.assertEqual(len(state["manifest_sha256"]), 64)

    def test_update_repairs_missing_data(self):
        self.client()
        path = self.helper.game_dir / REQUIRED_DATA[0]
        path.unlink()
        def repair(_):
            path.write_bytes(b"repaired")
            manifest = self.helper.game_dir / MANIFEST
            old = manifest.stat().st_mtime_ns
            os.utime(manifest, ns=(old + 1, old + 1))
        with contextlib.ExitStack() as stack:
            for name in ("require_wine", "ensure_idle", "setup_webview", "setup_ngm", "reg"):
                stack.enter_context(patch.object(self.helper, name))
            stack.enter_context(patch.object(self.helper, "ngm", side_effect=repair))
            self.helper.update()
        self.assertEqual(path.read_bytes(), b"repaired")
        self.assertEqual(json.loads((self.root / "state.json").read_text())["last_successful_operation"], "update")

    def test_cancelled_noop_update_does_not_claim_success(self):
        self.client()
        with contextlib.ExitStack() as stack:
            for name in ("require_wine", "ensure_idle", "setup_webview", "setup_ngm", "reg", "ngm"):
                stack.enter_context(patch.object(self.helper, name))
            stack.enter_context(patch.object(self.helper, "processes", return_value=set()))
            with self.assertRaisesRegex(HelperError, "could not be confirmed"):
                self.helper.update()
        self.assertFalse((self.root / "state.json").exists())

    def test_concurrent_operations_are_rejected(self):
        other = Helper(self.root)
        with self.helper.locked():
            with self.assertRaisesRegex(HelperError, "in progress"):
                with other.locked():
                    self.fail("Concurrent operation acquired the lock")
        with other.locked():
            pass

    def test_download_truncation_preserves_previous_file(self):
        target = self.root / "download.exe"
        pe(target)
        old = target.read_bytes()
        class Response(io.BytesIO):
            headers = {"Content-Length": "1000"}
            def geturl(self):
                return "https://platform.nexon.com/test"
        with patch("msw_helper.core.urlopen", return_value=Response(b"short")):
            with self.assertRaisesRegex(HelperError, "Incomplete"):
                self.helper.download("https://platform.nexon.com/test", target)
        self.assertEqual(target.read_bytes(), old)
        self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_cancel_before_download(self):
        self.helper.cancel.set()
        with patch("msw_helper.core.urlopen") as request:
            with self.assertRaises(Cancelled):
                self.helper.download("https://platform.nexon.com/test", self.root / "file")
            request.assert_not_called()

    def test_pinned_download_rejects_wrong_digest_and_reuses_verified_archive(self):
        target = self.root / "archive"
        target.write_bytes(b"old download")
        binary = b"verified upstream archive"
        sha = hashlib.sha256(binary).hexdigest()
        class Response(io.BytesIO):
            headers = {}
            def geturl(self):
                return DXVK_URL
        with patch("msw_helper.core.urlopen", return_value=Response(b"wrong download")):
            with self.assertRaisesRegex(HelperError, "validation failed"):
                self.helper.download(DXVK_URL, target, sha256=sha)
        self.assertEqual(target.read_bytes(), b"old download")
        with patch("msw_helper.core.urlopen", return_value=Response(binary)) as request:
            self.helper.download(DXVK_URL, target, sha256=sha)
            self.helper.download(DXVK_URL, target, sha256=sha)
            self.assertEqual(request.call_count, 1)

    def dxvk_archive(self, *, invalid_second=False):
        sample = self.root / "fixture.dll"
        pe(sample)
        binary = sample.read_bytes()
        archive = self.root / "dxvk.tar.gz"
        with tarfile.open(archive, "w:gz") as package:
            for name in DXVK_DLLS:
                data = b"invalid" if invalid_second and name == DXVK_DLLS[1] else binary
                info = tarfile.TarInfo(f"dxvk-{DXVK_VERSION}/x64/{name}")
                info.size = len(data)
                package.addfile(info, io.BytesIO(data))
            unwanted = tarfile.TarInfo("../../outside")
            unwanted.size = 3
            package.addfile(unwanted, io.BytesIO(b"bad"))
        return archive

    def test_dxvk_install_preserves_symlink_target_and_repairs_changed_dll(self):
        archive = self.dxvk_archive()
        host_dll = self.root / "host.dll"
        host_dll.write_bytes(b"host Wine builtin")
        target = self.helper.prefix / "drive_c/windows/system32/d3d11.dll"
        target.parent.mkdir(parents=True)
        target.symlink_to(host_dll)
        with patch.object(self.helper, "download", return_value=archive), patch.object(self.helper, "reg") as registry:
            self.helper.setup_dxvk()
            for dll in ("d3d11", "dxgi"):
                registry.assert_any_call(r"HKCU\Software\Wine\DllOverrides", dll, "builtin")
                for app in ("msw.exe", "Vuplex WebView.vuplex"):
                    registry.assert_any_call(r"HKCU\Software\Wine\AppDefaults" + "\\" + app +
                                             r"\DllOverrides", dll, "native,builtin")
            self.assertTrue(self.helper.dxvk_ready())
            self.assertEqual(host_dll.read_bytes(), b"host Wine builtin")
            self.assertFalse(target.is_symlink())
            target.write_bytes(b"corrupt DLL")
            self.assertFalse(self.helper.dxvk_ready())
            self.helper.setup_dxvk()
            self.assertTrue(self.helper.dxvk_ready())
        self.assertFalse((self.root / "outside").exists())

    def test_invalid_dxvk_archive_leaves_existing_graphics_untouched(self):
        archive = self.dxvk_archive(invalid_second=True)
        target = self.helper.prefix / "drive_c/windows/system32/d3d11.dll"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"previous graphics")
        with patch.object(self.helper, "download", return_value=archive), patch.object(self.helper, "reg") as registry:
            with self.assertRaises(HelperError):
                self.helper.setup_dxvk()
            registry.assert_not_called()
        self.assertEqual(target.read_bytes(), b"previous graphics")
        self.assertFalse((self.root / "dxvk.json").exists())

    def test_graphics_migration_waits_for_game_to_close_before_consuming_link(self):
        with patch.object(self.helper, "ensure_idle", side_effect=HelperError("Close Worlds")), \
                patch.object(self.helper, "setup_dxvk") as setup, patch.object(self.helper, "run") as launch:
            with self.assertRaisesRegex(HelperError, "Close Worlds"):
                self.helper.ngm(make_uri("launch"))
            setup.assert_not_called()
            launch.assert_not_called()

    def test_component_cache_checks_both_publisher_revision_and_local_digest(self):
        target = self.root / "component.exe"
        pe(target)
        binary = target.read_bytes()
        class Response(io.BytesIO):
            headers = {"Content-Length": str(len(binary))}
            def geturl(self):
                return "https://platform.nexon.com/test"
        with patch("msw_helper.core.urlopen", side_effect=lambda *a, **k: Response(binary)) as request:
            self.helper.download("https://platform.nexon.com/test", target, crc=1, executable=True)
            self.helper.download("https://platform.nexon.com/test", target, crc=1, executable=True)
            self.assertEqual(request.call_count, 1)
            target.write_bytes(binary + b"corruption")
            self.helper.download("https://platform.nexon.com/test", target, crc=1, executable=True)
            self.assertEqual(request.call_count, 2)
            self.helper.download("https://platform.nexon.com/test", target, crc=2, executable=True)
            self.assertEqual(request.call_count, 3)
        self.assertEqual(target.read_bytes(), binary)

    def test_webview_payload_is_complete_verified_and_independent_of_temporary_source(self):
        directory = self.helper.prefix / "drive_c/Program Files (x86)/Microsoft/Temp/unpacking"
        source = directory / "MicrosoftEdge_X64_123.0.exe.temporary"
        pe(source)
        binary = source.read_bytes()
        digest = hashlib.sha256(binary).hexdigest()
        manifest = directory / "OfflineManifest.gup"
        manifest.write_text('<response><app appid="{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}">'
                            '<updatecheck><manifest><packages><package name="MicrosoftEdge_X64_123.0.exe" '
                            f'hash_sha256="{digest}" size="{len(binary)}" />'
                            '</packages></manifest></updatecheck></app></response>')
        self.helper.cache.mkdir()
        source.write_bytes(binary[:-1])
        self.assertIsNone(self.helper.stage_webview_payload())
        source.write_bytes(binary[:-1] + b"x")
        self.assertIsNone(self.helper.stage_webview_payload())
        source.write_bytes(binary)
        staged = self.helper.stage_webview_payload()
        self.assertEqual(staged.parent, self.helper.cache)
        source.unlink()
        self.assertEqual(staged.read_bytes(), binary)
        self.assertEqual(self.helper.stage_webview_payload(), staged)

    def test_current_uri_and_browser_encoding_are_preserved(self):
        for uri in (make_uri("launch"),
                    "ngm://launch/%20-mode%3Alaunch%20-game%3A'106603%401585'%20-token%3A'A+B%2520'",
                    "ngm://launch/ -mode:launch -game:'106603@1585' -passarg:'/gamename:World & Friends'"):
            self.assertEqual(validate_uri(uri), uri)
            self.client()
            with patch.object(self.helper, "require_wine"), patch.object(self.helper, "register_game"), patch.object(self.helper, "ngm") as launch:
                self.helper.open_uri(uri)
                launch.assert_called_once_with(uri)

    def test_unrelated_and_malformed_links_rejected(self):
        for uri in ("https://example.com", "ngm://launch/ -game:'65538'", "ngm://launch/",
                    "ngm://launch/ -game:106603 -game:65538", "ngm://launch/ -game:106603%00",
                    "ngm://launch/ -game:106603%gg", "ngm://launch/ -game:106603\n"):
            with self.subTest(uri=uri), self.assertRaises((HelperError, UnicodeError)):
                validate_uri(uri)

    def test_launch_tokens_redacted(self):
        for text in (make_uri("launch"), "Command Line: a secret", "/ticket:SECRET", "-insign_token:'SECRET'"):
            self.assertNotIn("SECRET", redact(text))
            self.assertEqual(redact(text), "[Launch arguments omitted]")

    def test_unexpected_metadata_download_hosts_rejected(self):
        self.assertEqual(official_url("http://platform.nexon.com/NGM/Bin/NGM64.exe"),
                         "https://platform.nexon.com/NGM/Bin/NGM64.exe")
        for url in ("file:///etc/passwd", "https://nexon.com.evil.example/file", "https://user@platform.nexon.com/file"):
            with self.assertRaises(HelperError):
                official_url(url)

    def test_options_work_before_or_after_subcommand(self):
        for args in (["--data-dir", "/tmp/test", "verify"], ["verify", "--data-dir", "/tmp/test"]):
            self.assertEqual(parser().parse_args(args).data_dir, Path("/tmp/test"))

    def test_desktop_field_codes_are_not_interpreted_in_paths(self):
        self.assertEqual(exec_argument("/tmp/100% test"), '"/tmp/100%% test"')
        with self.assertRaises(HelperError):
            exec_argument("/tmp/a\nb")


if __name__ == "__main__":
    unittest.main()
