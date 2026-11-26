"""MapleStory Worlds launcher: one row per account, one Play button each.

Workers never touch GTK; they post results back with ``GLib.idle_add``.
"""
import collections
import sys
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import __version__
from . import accounts as accounts_mod
from .core import Cancelled, HelperError
from .desktop import GUI_CLASS, ICON_NAME, RESOURCES

ICON = ICON_NAME.rsplit(".", 1)[0]
CSS = b"""
.account-row { padding-top: 6px; padding-bottom: 6px; }
.play-button { min-width: 56px; font-weight: bold; }
"""


class AccountState:
    def __init__(self):
        self.busy = None          # Short status while something is happening.
        self.worker = False       # A background operation is running.
        self.helper = None        # The running operation's Helper, for Cancel.
        self.running = False      # The game is running in this account's prefix.
        self.installed = False
        self.signed_in = None     # True / False / None (unknown).
        self.launch_window = None
        self.log = collections.deque(maxlen=500)


class Launcher(Adw.Application):
    def __init__(self, wine=None):
        super().__init__(application_id=GUI_CLASS, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.wine = wine
        self.window = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        # The bundled icon works before (and without) desktop registration.
        Gtk.IconTheme.get_for_display(display).add_search_path(str(RESOURCES))
        Gtk.Window.set_default_icon_name(ICON)
        provider = Gtk.CssProvider()
        provider.load_from_bytes(GLib.Bytes.new(CSS))
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, default_width=460, default_height=340, title="MapleStory Worlds")
        self.app = app
        self.wine = app.wine
        self.states = {}
        self.rows = []
        self.log_views = {}
        self.registry = accounts_mod.Accounts()
        self._actions()

        self.title = Adw.WindowTitle(title="MapleStory Worlds")
        header = Adw.HeaderBar()
        header.set_title_widget(self.title)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add account")
        add.set_action_name("win.add-account")
        header.pack_start(add)
        menu = Gio.Menu()
        menu.append("Change world…", "win.change-world")
        menu.append("Register website links", "win.register")
        menu.append("Open data folder", "win.open-data")
        menu.append("About", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))

        self.toasts = Adw.ToastOverlay()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.toasts.set_child(self.stack)

        empty = Adw.StatusPage(icon_name=ICON, title="Add your first account",
                               description="Each account gets its own game install and stays signed in.",
                               css_classes=["compact"])
        start = Gtk.Button(label="Add account", halign=Gtk.Align.CENTER, action_name="win.add-account")
        start.add_css_class("pill")
        start.add_css_class("suggested-action")
        empty.set_child(start)
        self.stack.add_named(empty, "empty")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.group = Adw.PreferencesGroup(title="Accounts")
        box.append(self.group)
        clamp = Adw.Clamp(maximum_size=640, child=box)
        self.stack.add_named(Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER), "list")

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(self.toasts)
        self.set_content(view)
        self.connect("close-request", self._close_request)
        self.refresh()
        self.poll_source = GLib.timeout_add_seconds(3, self._poll_running)

    # --- actions -------------------------------------------------------------
    def _actions(self):
        for name, handler in (("add-account", self.add_account), ("change-world", self.change_world),
                              ("register", self.register_links), ("open-data", self.open_data),
                              ("about", self.about)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            self.add_action(action)
        for name, handler in (("signin", self.sign_in), ("signout", self.sign_out),
                              ("update", self.update_game), ("stop", self.stop_game),
                              ("winecfg", self.wine_settings), ("log", self.show_log),
                              ("rename", self.rename), ("remove", self.remove), ("cancel", self.cancel)):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate", lambda _a, param, h=handler: self._with_account(param.get_string(), h))
            self.add_action(action)

    def _with_account(self, account_id, handler):
        try:
            handler(self.registry.get(account_id))
        except HelperError as exc:
            self.toast(str(exc))

    # --- state -----------------------------------------------------------------
    def state(self, account):
        return self.states.setdefault(account.id, AccountState())

    @property
    def world(self):
        return self.registry.world

    def refresh(self):
        self.registry = accounts_mod.Accounts()
        self.title.set_subtitle(self.world["name"])
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        from .session import ACTIVE_APPS, native_processes
        for account in self.registry.accounts:
            state = self.state(account)
            helper = account.helper(wine=self.wine)
            state.installed = helper.game_dir.is_dir() and helper.verify()["installed"]
            state.running = bool(set(native_processes(helper.prefix).values()) & ACTIVE_APPS)
            row = self._row(account)
            self.group.add(row)
            self.rows.append(row)
            if state.installed and state.signed_in is None:
                self._check_signed_in(account)
        self.stack.set_visible_child_name("list" if self.registry.accounts else "empty")

    def _check_signed_in(self, account):
        from .web import check_signed_in

        def result(signed_in):
            self.state(account).signed_in = signed_in
            self._update_row(account.id)
        check_signed_in(account, result)

    def _row(self, account):
        row = Adw.ActionRow(title=account.name, css_classes=["account-row"])
        row.account_id = account.id
        row.add_prefix(Adw.Avatar(size=40, text=account.name, show_initials=True))
        row.spinner = Adw.Spinner(visible=False, valign=Gtk.Align.CENTER)
        row.add_suffix(row.spinner)
        row.button = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["play-button", "pill"])
        row.button.connect("clicked", lambda *_: self._with_account(account.id, self.primary))
        row.add_suffix(row.button)
        row.menu = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER,
                                  css_classes=["flat"], tooltip_text="More")
        row.add_suffix(row.menu)
        self._fill_row(row, account)
        return row

    def _menu(self, account, state):
        target = GLib.Variant.new_string(account.id)
        menu = Gio.Menu()

        def item(section, label, action):
            entry = Gio.MenuItem.new(label, None)
            entry.set_action_and_target_value(f"win.{action}", target)
            section.append_item(entry)
        main = Gio.Menu()
        if state.worker:
            item(main, "Cancel", "cancel")
        item(main, "Sign in again", "signin")
        item(main, "Sign out", "signout")
        if state.running:
            item(main, "Stop game", "stop")
        menu.append_section(None, main)
        game = Gio.Menu()
        if state.installed or account.helper().game_dir.is_dir():
            item(game, "Update / repair game", "update")
        item(game, "Wine settings", "winecfg")
        item(game, "Show log", "log")
        menu.append_section(None, game)
        manage = Gio.Menu()
        item(manage, "Rename…", "rename")
        item(manage, "Remove…", "remove")
        menu.append_section(None, manage)
        return menu

    def _fill_row(self, row, account):
        state = self.state(account)
        row.set_title(GLib.markup_escape_text(account.name))
        button = row.button
        for css in ("suggested-action", "destructive-action"):
            button.remove_css_class(css)
        row.spinner.set_visible(bool(state.busy))
        if state.busy:
            row.set_subtitle(GLib.markup_escape_text(state.busy))
            button.set_label("Play" if state.installed else "Install")
            button.set_sensitive(False)
        elif state.running:
            row.set_subtitle("Playing")
            button.set_label("Stop")
            button.add_css_class("destructive-action")
            button.set_sensitive(True)
        elif not state.installed:
            row.set_subtitle("Game not installed yet")
            button.set_label("Install")
            button.add_css_class("suggested-action")
            button.set_sensitive(True)
        else:
            row.set_subtitle({True: "Signed in", False: "Sign-in needed", None: "Ready"}[state.signed_in])
            button.set_label("Play")
            button.add_css_class("suggested-action")
            button.set_sensitive(True)
        row.menu.set_menu_model(self._menu(account, state))

    def _update_row(self, account_id):
        for row in self.rows:
            if row.account_id == account_id:
                try:
                    self._fill_row(row, self.registry.get(account_id))
                except HelperError:
                    pass
        return False

    def _poll_running(self):
        from .session import ACTIVE_APPS, native_processes
        for account in self.registry.accounts:
            state = self.state(account)
            try:
                running = bool(set(native_processes(account.helper().prefix).values()) & ACTIVE_APPS)
            except OSError:
                continue
            if running != state.running:
                state.running = running
                self._update_row(account.id)
        return True

    # --- messages ------------------------------------------------------------
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def error(self, heading, text):
        dialog = Adw.AlertDialog(heading=heading, body=text)
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def log(self, account_id, text):
        state = self.states.setdefault(account_id, AccountState())
        state.log.append(text)
        buffer = self.log_views.get(account_id)
        if buffer is not None:
            buffer.insert(buffer.get_end_iter(), text + "\n")
        return False

    # --- background work -------------------------------------------------------
    def run_worker(self, account, label, operation, *, done=None, quiet_cancel=False):
        state = self.state(account)
        if state.worker:
            self.toast(f"{account.name} is busy: {state.busy}")
            return
        state.worker, state.busy = True, label
        self.log(account.id, label)
        account_id, name = account.id, account.name

        def emit(text):
            GLib.idle_add(self.log, account_id, text)
        helper = account.helper(wine=self.wine, emit=emit)
        state.helper = helper
        self._update_row(account_id)

        def work():
            error = None
            try:
                operation(helper)
            except Exception as exc:  # Reported in the GUI, never lost.
                error = exc
            GLib.idle_add(finish, error)

        def finish(error):
            state.worker, state.busy, state.helper = False, None, None
            if isinstance(error, Cancelled):
                if not quiet_cancel:
                    self.toast(f"{name}: cancelled")
            elif error is not None:
                self.log(account_id, f"Error: {error}")
                self.error(name, str(error))
            elif done:
                done()
            self.refresh()
            return False
        threading.Thread(target=work, daemon=True).start()

    # --- the one button ----------------------------------------------------------
    def primary(self, account):
        state = self.state(account)
        if state.busy:
            return
        if state.running:
            self.stop_game(account)
        elif not state.installed:
            self.install(account)
        else:
            self.play(account)

    def play(self, account):
        from .web import LaunchWindow
        state = self.state(account)
        if state.launch_window is not None:
            state.launch_window.present()
            return
        state.busy = f"Opening {self.world['name']}…"
        self._update_row(account.id)

        def on_link(uri):
            state.signed_in = True
            self.run_worker(account, "Starting the game…", lambda helper: helper.open_uri(uri),
                            done=lambda: self.toast(f"{account.name}: game started"))

        def on_closed(captured):
            state.launch_window = None
            if not captured and not state.worker:
                state.busy = None
                self._update_row(account.id)
        state.launch_window = LaunchWindow(self, account, dict(self.world), mode="play",
                                           on_link=on_link, on_closed=on_closed)

    def sign_in(self, account):
        from .web import LaunchWindow
        state = self.state(account)
        if state.launch_window is not None:
            state.launch_window.present()
            return

        def on_signed_in():
            state.signed_in = True
            self.toast(f"{account.name} is signed in")
            self._update_row(account.id)

        def on_closed(_):
            state.launch_window = None
        state.launch_window = LaunchWindow(self, account, dict(self.world), mode="signin",
                                           on_signed_in=on_signed_in, on_closed=on_closed)

    def sign_out(self, account):
        from .web import sign_out
        state = self.state(account)
        if state.launch_window is not None:
            state.launch_window.close()

        def done():
            state.signed_in = False
            self.toast(f"{account.name} signed out")
            self._update_row(account.id)
        sign_out(account, done)

    def install(self, account, *, allow_download=False):
        others = [a for a in self.registry.accounts if a.id != account.id and self.state(a).installed]
        sources = [a for a in others if not self.state(a).running and not self.state(a).worker]
        if others and not sources and not allow_download:
            dialog = Adw.AlertDialog(
                heading="Copy or download?",
                body=f"{others[0].name}'s game is open. Close it and press Install again to copy its "
                     "game files (quick, no download) — or download a fresh copy now (several minutes).")
            dialog.add_response("later", "Later")
            dialog.add_response("download", "Download now")
            dialog.set_close_response("later")
            dialog.connect("response", lambda _d, choice: choice == "download"
                           and self.install(account, allow_download=True))
            dialog.present(self)
            return
        first = not others
        if sources:
            source = sources[0]

            def operation(helper):
                accounts_mod.clone_install(source.helper(wine=self.wine), helper)
            label = f"Copying the game from {source.name}…"
        else:
            def operation(helper):
                helper.install()
                if first:
                    from .desktop import register
                    try:
                        register(helper)
                    except (HelperError, OSError) as exc:
                        helper.emit(f"Website links were not registered: {exc}")
            label = "Installing the game (several minutes)…"
        self.run_worker(account, label, operation,
                        done=lambda: self.toast(f"{account.name} is ready. Press Play."))

    def update_game(self, account):
        self.run_worker(account, "Checking and repairing game files…", lambda h: h.update(),
                        done=lambda: self.toast("Repair finished. If a game window opened, close it and press Play."))

    def stop_game(self, account):
        dialog = Adw.AlertDialog(heading=f"Stop {account.name}?",
                                 body="The game will be closed. Unsaved progress may be lost.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("stop", "Stop")
        dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def response(_dialog, choice):
            if choice == "stop":
                self.run_worker(account, "Stopping…", lambda h: h.stop())
        dialog.connect("response", response)
        dialog.present(self)

    def wine_settings(self, account):
        self.run_worker(account, "Wine settings are open…", lambda h: h.winecfg())

    def cancel(self, account):
        helper = self.state(account).helper
        if helper is not None:
            helper.cancel.set()
            self.toast("Cancelling…")

    # --- dialogs -------------------------------------------------------------------
    def _ask_text(self, heading, body, label, initial, confirm, callback):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        group = Adw.PreferencesGroup()
        entry = Adw.EntryRow(title=label, text=initial)
        group.add(entry)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", confirm)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")
        entry.connect("entry-activated", lambda *_: (dialog.close(), callback(entry.get_text())))

        def response(_dialog, choice):
            if choice == "ok":
                callback(entry.get_text())
        dialog.connect("response", response)
        dialog.present(self)
        entry.grab_focus()

    def add_account(self):
        def create(name):
            try:
                account = accounts_mod.Accounts().add(name)
            except HelperError as exc:
                self.error("Couldn't add account", str(exc))
                return
            self.refresh()
            account = self.registry.get(account.id)
            self.install(account)
            self.sign_in(account)  # Sign in while the game installs.
        self._ask_text("Add account", "Give it a name you'll recognise, such as Main or Alt. "
                       "You'll sign in to Nexon next.", "Account name", "", "Add", create)

    def rename(self, account):
        def apply(name):
            try:
                accounts_mod.Accounts().rename(account.id, name)
            except HelperError as exc:
                self.error("Couldn't rename", str(exc))
            self.refresh()
        self._ask_text("Rename account", "", "Account name", account.name, "Rename", apply)

    def remove(self, account):
        state = self.state(account)
        if state.running or state.worker:
            self.error("Account is in use", f"Stop {account.name} and wait for it to finish first.")
            return
        dialog = Adw.AlertDialog(
            heading=f"Remove {account.name}?",
            body="“Delete everything” removes this account's game install, Wine prefix and Nexon "
                 f"sign-in from your computer.\n\n{account.data_dir}")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("list", "Remove from list")
        dialog.add_response("delete", "Delete everything")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def response(_dialog, choice):
            from .web import forget_session
            if choice == "list":
                accounts_mod.Accounts().remove(account.id)
                self.states.pop(account.id, None)
                self.refresh()
            elif choice == "delete":
                if state.launch_window is not None:
                    state.launch_window.close()
                forget_session(account)

                def operation(helper):
                    helper.uninstall()
                    from .maintenance import DIRECTORIES
                    import shutil
                    for name in DIRECTORIES:  # The web profile may exist without a game install.
                        shutil.rmtree(account.data_dir / name, ignore_errors=True)
                    accounts_mod.Accounts().remove(account.id)
                self.run_worker(account, "Deleting…", operation,
                                done=lambda: (self.states.pop(account.id, None),
                                              self.toast(f"{account.name} removed")))
        dialog.connect("response", response)
        dialog.present(self)

    def change_world(self):
        dialog = Adw.AlertDialog(heading="Choose world",
                                 body="Open the world on maplestoryworlds.nexon.com and copy its page link.")
        group = Adw.PreferencesGroup()
        name = Adw.EntryRow(title="World name", text=self.world["name"])
        link = Adw.EntryRow(title="World page link", text=accounts_mod.world_url(self.world["id"]))
        group.add(name)
        group.add(link)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("default", "Use ChronoStory")
        dialog.add_response("ok", "Save")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("cancel")

        def response(_dialog, choice):
            try:
                if choice == "ok":
                    accounts_mod.Accounts().set_world(name.get_text(), accounts_mod.parse_world(link.get_text()))
                elif choice == "default":
                    accounts_mod.Accounts().set_world(accounts_mod.DEFAULT_WORLD["name"],
                                                      accounts_mod.DEFAULT_WORLD["id"])
                else:
                    return
            except HelperError as exc:
                self.error("Couldn't change world", str(exc))
                return
            self.refresh()
            self.toast(f"Play now opens {self.world['name']}")
        dialog.connect("response", response)
        dialog.present(self)

    def show_log(self, account):
        dialog = Adw.Dialog(title=f"{account.name} · log", content_width=640, content_height=440)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        folder = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Open log folder")
        folder.connect("clicked", lambda *_: self._open_path(account.helper().logs))
        header.pack_start(folder)
        view.add_top_bar(header)
        text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
        buffer = text.get_buffer()
        buffer.set_text("\n".join(self.state(account).log) or "Nothing yet.")
        self.log_views[account.id] = buffer
        dialog.connect("closed", lambda *_: self.log_views.pop(account.id, None))
        view.set_content(Gtk.ScrolledWindow(child=text, vexpand=True))
        dialog.set_child(view)
        dialog.present(self)

    def register_links(self):
        installed = [a for a in self.registry.accounts if self.state(a).installed]
        if not installed:
            self.error("Nothing to register", "Install an account first.")
            return
        from .desktop import register
        self.run_worker(installed[0], "Registering website links…", lambda h: register(h),
                        done=lambda: self.toast("Menu entry and website links registered"))

    def _open_path(self, path):
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(path)).get_uri(), None)
        except GLib.Error as exc:
            self.toast(f"Couldn't open {path}: {exc.message}")

    def open_data(self):
        self._open_path(accounts_mod.default_home())

    def about(self):
        Adw.AboutDialog(application_name="MapleStory Worlds Launcher", application_icon=ICON,
                        version=__version__, license_type=Gtk.License.GPL_3_0,
                        comments="Unofficial launcher that runs MapleStory Worlds under Wine, "
                                 "with one install and sign-in per account.").present(self)

    def _close_request(self, *_):
        busy = [a.name for a in self.registry.accounts if self.state(a).worker]
        if busy:
            self.error("Still working", "Wait for " + ", ".join(busy) + " to finish (or cancel it) before closing.")
            return True
        for state in self.states.values():
            if state.launch_window is not None:
                state.launch_window.close()
        GLib.source_remove(self.poll_source)
        return False


def run_app(wine=None) -> int:
    GLib.set_prgname(GUI_CLASS)
    GLib.set_application_name("MapleStory Worlds")
    return Launcher(wine).run([sys.argv[0]])
