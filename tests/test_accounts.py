import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from msw_helper import accounts
from msw_helper.core import HelperError, make_uri
from msw_helper.maintenance import MARKER


class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "data"),
                                      "XDG_CONFIG_HOME": str(self.root / "config")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MSW_HOME", None)

    # --- registry -----------------------------------------------------------
    def test_add_creates_claimed_instance_under_default_home(self):
        account = accounts.Accounts().add("My Main")
        self.assertEqual(account.id, "my-main")
        self.assertEqual(account.data_dir, accounts.instances_root() / "my-main")
        self.assertEqual(account.web_dir, account.data_dir / "web")
        # Claimed before any web profile or prefix exists, so uninstall may remove them.
        self.assertTrue((account.data_dir / MARKER).is_file())
        reloaded = accounts.Accounts()
        self.assertEqual([a.id for a in reloaded.accounts], ["my-main"])
        self.assertEqual(reloaded.accounts[0].data_dir, account.data_dir)

    def test_same_name_is_rejected_but_slug_clashes_get_unique_ids(self):
        registry = accounts.Accounts()
        registry.add("Alt")
        with self.assertRaises(HelperError):
            accounts.Accounts().add("alt")
        second = accounts.Accounts().add("Alt!")
        self.assertEqual(second.id, "alt-2")

    def test_blank_name_rejected(self):
        with self.assertRaises(HelperError):
            accounts.Accounts().add("   ")

    def test_rename_and_remove(self):
        accounts.Accounts().add("Temp")
        accounts.Accounts().add("Other")
        with self.assertRaises(HelperError):
            accounts.Accounts().rename("temp", "other")
        accounts.Accounts().rename("temp", "Renamed")
        self.assertEqual(accounts.Accounts().get("temp").name, "Renamed")
        accounts.Accounts().remove("temp")
        self.assertEqual([a.id for a in accounts.Accounts().accounts], ["other"])
        with self.assertRaises(HelperError):
            accounts.Accounts().get("temp")

    def test_resolve_by_id_or_name(self):
        accounts.Accounts().add("Second Account")
        registry = accounts.Accounts()
        self.assertEqual(registry.resolve("second-account").id, "second-account")
        self.assertEqual(registry.resolve("Second Account").id, "second-account")
        with self.assertRaises(HelperError):
            registry.resolve("missing")

    def test_default_adopts_existing_single_account_install(self):
        home = accounts.default_home()
        (home / "prefix").mkdir(parents=True)
        (home / "prefix/system.reg").write_text("WINE REGISTRY\n")
        registry = accounts.Accounts()
        self.assertEqual(registry.default().id, "default")
        self.assertEqual(registry.default().data_dir, home)
        registry.add("Alt")
        self.assertEqual([a.id for a in accounts.Accounts().accounts], ["default", "alt"])

    def test_default_synthetic_when_empty(self):
        registry = accounts.Accounts()
        self.assertEqual(registry.accounts, [])
        self.assertEqual(registry.default().data_dir, accounts.default_home())

    def test_invalid_rows_are_ignored_on_load(self):
        accounts.atomic_json(accounts.registry_path(), {"version": 1, "accounts": [
            {"id": "ok", "name": "Fine", "data_dir": str(self.root / "ok")},
            {"id": "Bad Id", "name": "x", "data_dir": "/x"},
            {"id": "ok", "name": "dup", "data_dir": "/dup"},
            {"id": "rel", "name": "relative", "data_dir": "relative/path"},
            {"name": "noid", "data_dir": "/y"}]})
        self.assertEqual([a.id for a in accounts.Accounts().accounts], ["ok"])

    def test_route_sends_browser_links_to_first_account(self):
        main = accounts.Accounts().add("Main")
        accounts.Accounts().add("Alt")
        self.assertEqual(accounts.route(make_uri("launch")).home, main.data_dir)

    # --- world --------------------------------------------------------------
    def test_new_accounts_have_no_world(self):
        self.assertIsNone(accounts.Accounts().add("Main").world)
        self.assertIsNone(accounts.Accounts().get("main").world)
        self.assertEqual(accounts.world_url("6697fa1ed9cd4de7bea6daffed99a637"),
                         "https://maplestoryworlds.nexon.com/en/play/6697fa1ed9cd4de7bea6daffed99a637")

    def test_parse_world_accepts_page_links_and_ids(self):
        world_id = "0123456789abcdef0123456789abcdef"
        for text in (world_id, f"https://maplestoryworlds.nexon.com/en/play/{world_id}",
                     f"https://maplestoryworlds.nexon.com/play/{world_id}/",
                     f"  https://maplestoryworlds.nexon.com/ko/play/{world_id}?tab=info  "):
            self.assertEqual(accounts.parse_world(text), world_id, text)
        for text in ("", "chronostory", "https://evil.example/play/" + world_id,
                     "http://maplestoryworlds.nexon.com/en/play/" + world_id,
                     "https://maplestoryworlds.nexon.com/group/6AK9I"):
            with self.assertRaises(HelperError, msg=text):
                accounts.parse_world(text)

    def test_set_world_is_per_account_and_persists(self):
        world_id = "0123456789abcdef0123456789abcdef"
        registry = accounts.Accounts()
        registry.add("Main")
        registry.add("Alt")
        accounts.Accounts().set_world("alt", "Other World", world_id)
        reloaded = accounts.Accounts()
        self.assertEqual(reloaded.get("alt").world, {"name": "Other World", "id": world_id})
        self.assertIsNone(reloaded.get("main").world)
        # New accounts don't inherit another account's world.
        self.assertIsNone(accounts.Accounts().add("Third").world)
        with self.assertRaises(HelperError):
            accounts.Accounts().set_world("main", "Bad", "not-an-id")
        with self.assertRaises(HelperError):
            accounts.Accounts().set_world("nobody", "Other World", world_id)

    def test_old_shared_world_carries_over_to_existing_accounts(self):
        world_id = "0123456789abcdef0123456789abcdef"
        path = accounts.registry_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 1, "world": {"name": "Old World", "id": world_id},
                                    "accounts": [{"id": "main", "name": "Main",
                                                  "data_dir": str(self.root / "main")}]}))
        registry = accounts.Accounts()
        self.assertEqual(registry.get("main").world, {"name": "Old World", "id": world_id})
        registry.rename("main", "Main 2")  # Saving writes the world onto the account itself.
        saved = json.loads(path.read_text())
        self.assertNotIn("world", saved)
        self.assertEqual(saved["accounts"][0]["world"]["name"], "Old World")


class CloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "data"),
                                      "XDG_CONFIG_HOME": str(self.root / "config")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MSW_HOME", None)
        registry = accounts.Accounts()
        self.source = registry.add("Main").helper(emit=lambda _: None)
        self.target = accounts.Accounts().add("Alt").helper(emit=lambda _: None)
        prefix = self.source.prefix
        (prefix / "dosdevices").mkdir(parents=True)
        (prefix / "dosdevices/c:").symlink_to("../drive_c", target_is_directory=True)
        (prefix / "dosdevices/z:").symlink_to("/", target_is_directory=True)
        (prefix / "system.reg").write_text("WINE REGISTRY Version 2\n")
        self.source.game_dir.mkdir(parents=True)
        (self.source.game_dir / "msw.exe").write_text("game")
        user = prefix / "drive_c/users/me"
        local_low = user / "AppData/LocalLow/nexon/MapleStory Worlds"
        for path in ("resource_cache/raw/a.bin", "Vuplex.WebView/chromium-cache/Cookies",
                     "backtrace/crash.dmp", "Player.log", "GamePreview/p.png"):
            (local_low / path).parent.mkdir(parents=True, exist_ok=True)
            (local_low / path).write_text("x")
        (user / "AppData/Local/Temp").mkdir(parents=True)
        (user / "AppData/Local/Temp/tmp.txt").write_text("x")
        cookies = prefix / "drive_c/ProgramData/Nexon/NGM/NGM64.exe.WebView2/EBWebView"
        cookies.mkdir(parents=True)
        (cookies / "Cookies").write_text("x")
        (prefix / "drive_c/ProgramData/Nexon/NGM/NGM64.exe").write_text("ngm")
        (self.source.home / "dxvk.json").write_text('{"version": "3.1"}')
        self.local_low_rel = local_low.relative_to(prefix)

    def clone(self):
        calls = []
        with patch.object(type(self.source), "verify", return_value={"installed": True}), \
                patch.object(type(self.source), "require_wine"), \
                patch.object(type(self.source), "run", side_effect=lambda args, **kw: calls.append(args) or ""), \
                patch.object(type(self.source), "finish", side_effect=lambda op: calls.append(["finish", op])), \
                patch("msw_helper.session.native_processes", return_value={}), \
                patch("msw_helper.session.shutdown_prefix") as shutdown:
            accounts.clone_install(self.source, self.target)
        return calls, shutdown

    def test_clone_copies_game_but_not_account_data(self):
        calls, shutdown = self.clone()
        prefix = self.target.prefix
        local_low = prefix / self.local_low_rel
        self.assertEqual((self.target.game_dir / "msw.exe").read_text(), "game")
        self.assertTrue((local_low / "resource_cache/raw/a.bin").is_file())
        self.assertTrue((local_low / "GamePreview/p.png").is_file())
        self.assertTrue((prefix / "drive_c/ProgramData/Nexon/NGM/NGM64.exe").is_file())
        for gone in ("Vuplex.WebView", "backtrace", "Player.log"):
            self.assertFalse((local_low / gone).exists(), gone)
        self.assertFalse((prefix / "drive_c/ProgramData/Nexon/NGM/NGM64.exe.WebView2").exists())
        self.assertFalse((prefix / "drive_c/users/me/AppData/Local/Temp").exists())
        # Drive links are copied as links, never followed into /.
        self.assertTrue((prefix / "dosdevices/z:").is_symlink())
        self.assertEqual(os.readlink(prefix / "dosdevices/z:"), "/")
        self.assertEqual((self.target.home / "dxvk.json").read_text(), '{"version": "3.1"}')
        self.assertFalse((self.target.home / "prefix.partial").exists())
        # The game's saved login (Unity PlayerPrefs) is deleted, then the copy is verified.
        self.assertIn(["reg", "delete", accounts.GAME_PREFS_KEY, "/f"], calls)
        self.assertEqual(calls[-1], ["finish", "clone"])
        shutdown.assert_called_once_with(self.target)

    def test_clone_refuses_running_source(self):
        for running in ({1: "msw.exe"}, {2: "wineserver"}, {3: "vuplex webview.vuplex"}):
            with patch.object(type(self.source), "verify", return_value={"installed": True}), \
                    patch.object(type(self.source), "require_wine"), \
                    patch("msw_helper.session.native_processes", return_value=running):
                with self.assertRaises(HelperError, msg=running):
                    accounts.clone_install(self.source, self.target)
            self.assertFalse(self.target.prefix.exists())

    def test_clone_ignores_native_browser_left_by_the_game(self):
        # Firefox opened from the game inherits WINEPREFIX but doesn't use the prefix.
        with patch("msw_helper.session.native_processes", return_value={4: "firefox", 5: "web content"}), \
                patch.object(type(self.source), "verify", return_value={"installed": True}), \
                patch.object(type(self.source), "require_wine"), \
                patch.object(type(self.source), "run", return_value=""), \
                patch.object(type(self.source), "finish"), \
                patch("msw_helper.session.shutdown_prefix"):
            accounts.clone_install(self.source, self.target)
        self.assertTrue((self.target.game_dir / "msw.exe").is_file())

    def test_clone_refuses_existing_target_prefix(self):
        self.target.prefix.mkdir(parents=True)
        with patch.object(type(self.source), "verify", return_value={"installed": True}), \
                patch.object(type(self.source), "require_wine"), \
                patch("msw_helper.session.native_processes", return_value={}):
            with self.assertRaises(HelperError):
                accounts.clone_install(self.source, self.target)

    def test_failed_copy_leaves_no_partial_prefix(self):
        with patch.object(type(self.source), "verify", return_value={"installed": True}), \
                patch.object(type(self.source), "require_wine"), \
                patch("msw_helper.session.native_processes", return_value={}), \
                patch("msw_helper.accounts.shutil.copytree", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                accounts.clone_install(self.source, self.target)
        self.assertFalse(self.target.prefix.exists())
        self.assertFalse((self.target.home / "prefix.partial").exists())


if __name__ == "__main__":
    unittest.main()
