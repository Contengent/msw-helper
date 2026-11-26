"""Exercise the real GTK launcher with a mocked game backend and browser window.

Needs a display (run under Xvfb or a desktop session). No Wine process, Nexon
account or network access is used.
"""
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib

from msw_helper import accounts
from msw_helper.app import Launcher
from msw_helper.core import Helper, make_uri


def pump(seconds=0.2, until=None):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + 5
    end = time.monotonic() + seconds
    while time.monotonic() < (deadline if until else end):
        while context.iteration(False):
            pass
        if until and until():
            return
        time.sleep(0.01)
    assert until is None, "condition was not reached"


def row_for(win, account_id):
    return next(row for row in win.rows if row.account_id == account_id)


class FakeLaunchWindow:
    """Stands in for the browser window: the site 'returns' a launch link."""
    opened = []

    def __init__(self, parent, account, world, *, mode="play", on_link=None, on_signed_in=None, on_closed=None):
        FakeLaunchWindow.opened.append((account.id, world["name"], mode))
        if on_closed:
            GLib.idle_add(lambda: on_closed(True) and False)
        if mode == "play" and on_link:
            GLib.idle_add(lambda: on_link(make_uri("launch")) and False)
        elif on_signed_in:
            GLib.idle_add(lambda: on_signed_in() and False)

    def present(self):
        pass

    def close(self):
        pass


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    installed = set()
    launched = []
    clones = []
    env = {"XDG_DATA_HOME": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config")}
    with patch.dict(os.environ, env), \
            patch.object(Helper, "verify", lambda self: {"installed": self.home in installed, "missing": []}), \
            patch.object(Helper, "install", lambda self: (self.game_dir.mkdir(parents=True), installed.add(self.home))), \
            patch.object(Helper, "open_uri", lambda self, uri: launched.append((self.home, uri))), \
            patch("msw_helper.desktop.register", lambda helper: None), \
            patch("msw_helper.accounts.clone_install",
                  lambda source, target: (clones.append((source.home, target.home)),
                                          target.game_dir.mkdir(parents=True), installed.add(target.home))), \
            patch("msw_helper.web.check_signed_in", lambda account, callback: callback(False)), \
            patch("msw_helper.web.LaunchWindow", FakeLaunchWindow):
        os.environ.pop("MSW_HOME", None)
        app = Launcher()
        app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
        app.register(None)
        app.activate()
        win = app.window
        pump()
        assert win.stack.get_visible_child_name() == "empty"
        assert win.title.get_subtitle() == ""

        # First account: Install downloads (no other account to copy from).
        main = accounts.Accounts().add("Main")
        win.refresh()
        assert win.stack.get_visible_child_name() == "list"
        row = row_for(win, "main")
        assert row.button.get_label() == "Install"
        row.button.emit("clicked")
        pump(until=lambda: not win.state(main).worker)
        assert main.helper().home in installed
        row = row_for(win, "main")
        assert row.button.get_label() == "Play", row.button.get_label()
        assert row.get_subtitle() == "Sign-in needed · No world chosen", row.get_subtitle()

        # No world chosen yet: Play asks for one instead of opening a page.
        asked = []
        with patch.object(win, "change_world", lambda account, then=None: asked.append((account.id, then))):
            row.button.emit("clicked")
        assert asked == [("main", win.play)], asked
        assert not FakeLaunchWindow.opened
        accounts.Accounts().set_world("main", "ChronoStory", "6697fa1ed9cd4de7bea6daffed99a637")
        win.refresh()
        assert win.title.get_subtitle() == "ChronoStory"
        row = row_for(win, "main")
        assert row.get_subtitle() == "Sign-in needed", row.get_subtitle()

        # One button: opens the world as this account and hands the link to its prefix.
        row.button.emit("clicked")
        pump(until=lambda: launched and not win.state(main).worker)
        assert FakeLaunchWindow.opened[-1] == ("main", "ChronoStory", "play")
        assert len(launched) == 1 and launched[0][0] == main.helper().home, launched
        assert launched[0][1].startswith("ngm://launch/")
        assert win.state(main).signed_in is True

        # Second account: copied from the first instead of downloaded.
        alt = accounts.Accounts().add("Alt")
        accounts.Accounts().set_world("alt", "ChronoStory", "6697fa1ed9cd4de7bea6daffed99a637")
        win.refresh()
        row_for(win, "alt").button.emit("clicked")
        pump(until=lambda: not win.state(alt).worker)
        assert clones == [(main.helper().home, alt.helper().home)], clones
        assert row_for(win, "alt").button.get_label() == "Play"

        # Play on Alt goes to Alt's prefix, not Main's.
        row_for(win, "alt").button.emit("clicked")
        pump(until=lambda: len(launched) == 2 and not win.state(alt).worker)
        assert launched[1][0] == alt.helper().home

        # A running game turns the button into Stop.
        with patch("msw_helper.session.native_processes",
                   lambda prefix: {1: "msw.exe"} if prefix == alt.helper().prefix else {}):
            win._poll_running()
            assert row_for(win, "alt").button.get_label() == "Stop"
            assert row_for(win, "main").button.get_label() == "Play"
            win._poll_running()

        # Each account has its own world; mixed worlds are named on the rows.
        accounts.Accounts().set_world("alt", "Other World", "0123456789abcdef0123456789abcdef")
        accounts.Accounts().rename("alt", "Second")
        win.refresh()
        assert win.title.get_subtitle() == "", win.title.get_subtitle()
        assert row_for(win, "alt").get_title() == "Second"
        assert row_for(win, "alt").get_subtitle().endswith(" · Other World"), row_for(win, "alt").get_subtitle()
        assert row_for(win, "main").get_subtitle().endswith(" · ChronoStory"), row_for(win, "main").get_subtitle()
        row_for(win, "alt").button.emit("clicked")
        pump(until=lambda: len(launched) == 3 and not win.state(alt).worker)
        assert FakeLaunchWindow.opened[-1] == ("alt", "Other World", "play"), FakeLaunchWindow.opened
        row_for(win, "main").button.emit("clicked")
        pump(until=lambda: len(launched) == 4 and not win.state(main).worker)
        assert FakeLaunchWindow.opened[-1] == ("main", "ChronoStory", "play"), FakeLaunchWindow.opened

        # Back to one shared world: the title names it again.
        accounts.Accounts().set_world("alt", "ChronoStory", "6697fa1ed9cd4de7bea6daffed99a637")
        win.refresh()
        assert win.title.get_subtitle() == "ChronoStory"
        assert row_for(win, "alt").get_subtitle() == "Signed in", row_for(win, "alt").get_subtitle()

        assert win._close_request() is False
        win.destroy()
        pump()
print("Launcher smoke test passed")
