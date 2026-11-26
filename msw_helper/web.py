"""Each account's private Nexon web profile, and the window that starts a world.

The Worlds website starts the game by building an ``ngm://launch/`` link in the
page from the signed-in profile's cookies and opening it in a hidden frame. The
launch window loads the chosen world page in the account's own profile, lets the
site's Play logic run, and catches that link before it can leave the launcher;
the launcher then hands it to the account's Wine prefix. If the profile is not
signed in, the site sends the window to Nexon's real sign-in page instead.
"""
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
gi.require_version("Soup", "3.0")
from gi.repository import Adw, GLib, Gtk, Soup, WebKit

from .accounts import WORLD_HOST, world_url
from .core import HelperError, atomic_json, validate_uri

SITE_PATTERN = WORLD_HOST + "/*"
# The site only starts the launcher in browsers it recognises (Chrome, Firefox,
# Opera, macOS Safari); WebKitGTK on Linux is none of them, so its Play button
# silently does nothing. Identify as Chrome on the Worlds pages only — Nexon's
# sign-in and the Cloudflare check are left untouched.
CHROME_SHIM = ("if (!window.chrome) { Object.defineProperty(window, 'chrome', "
               "{value: {runtime: {}}, configurable: true}); }")
STATE_JS = """JSON.stringify((() => {
  const store = window.$nuxt && window.$nuxt.$store;
  const cookie = document.cookie.split('; ').some(c => c.startsWith('NPP='));
  return {ready: !!document.querySelector('button.btn_play') && typeof window.NGM === 'object' && !!store,
          signedIn: store ? !!store.getters.isLogin : cookie};
})())"""
CLICK_JS = "(() => { const b = document.querySelector('button.btn_play'); if (!b) return 'missing'; b.click(); return 'clicked'; })()"
LOGIN_DOMAINS = ("signin.nexon.com", "accounts.google.com", "appleid.apple.com",
                 "facebook.com", "x.com", "twitter.com")


def is_sign_in_page(uri: str) -> bool:
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if host == "www.nexon.com" and parts.path.startswith("/account"):
        return True
    return any(host == domain or host.endswith("." + domain) for domain in LOGIN_DOMAINS)
# Presses of the site's Play button per window, counted separately for signed-in
# presses (which start the game) and signed-out ones (which open sign-in).
MAX_PRESSES = 2
# After a press the site reloads the world page several times on its way to
# the launcher or the sign-in page; don't press again during that. Reaching the
# sign-in page was observed to take about 12 seconds.
PRESS_COOLDOWN = {"signed_in": 10, "signed_out": 30}
START_TIMEOUT = 25

_sessions = {}
_restored = set()


def session_for(account) -> WebKit.NetworkSession:
    """One persistent WebKit profile per account, created once per process."""
    key = str(account.web_dir)
    session = _sessions.get(key)
    if session is None:
        account.web_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(account.web_dir, 0o700)
        session = WebKit.NetworkSession.new(str(account.web_dir / "data"), str(account.web_dir / "cache"))
        session.get_cookie_manager().set_persistent_storage(
            str(account.web_dir / "cookies.sqlite"), WebKit.CookiePersistentStorage.SQLITE)
        _sessions[key] = session
    return session


def forget_session(account):
    _sessions.pop(str(account.web_dir), None)
    _restored.discard(str(account.web_dir))


def _saved_cookies(account) -> Path:
    return account.web_dir / "session-cookies.json"


def _nexon(domain: str) -> bool:
    domain = domain.lstrip(".")
    return domain == "nexon.com" or domain.endswith(".nexon.com")


def save_session(account):
    """Keep Nexon's browser-session cookies across launcher restarts.

    A normal browser forgets session cookies when it closes; keeping them is
    what lets an account stay signed in between launcher runs. The file is
    private to the user, beside WebKit's own cookie database.
    """
    manager = session_for(account).get_cookie_manager()

    def finished(manager, result):
        try:
            cookies = manager.get_all_cookies_finish(result)
        except GLib.Error:
            return
        rows = [{"name": c.get_name(), "value": c.get_value(), "domain": c.get_domain(),
                 "path": c.get_path(), "secure": c.get_secure(), "http_only": c.get_http_only(),
                 "same_site": int(c.get_same_site_policy())}
                for c in cookies if c.get_expires() is None and _nexon(c.get_domain())]
        path = _saved_cookies(account)
        if rows:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_json(path, {"saved_at": int(time.time()), "cookies": rows})
            os.chmod(path, 0o600)
        else:
            path.unlink(missing_ok=True)
    manager.get_all_cookies(None, finished)


def restore_session(account, done):
    """Put saved session cookies back once per run, then call ``done``."""
    key = str(account.web_dir)
    manager = session_for(account).get_cookie_manager()
    rows = []
    if key not in _restored:
        _restored.add(key)
        try:
            data = json.loads(_saved_cookies(account).read_text())
            rows = [r for r in data.get("cookies", []) if isinstance(r, dict) and _nexon(str(r.get("domain", "")))]
        except (OSError, ValueError, AttributeError):
            rows = []
    if not rows:
        GLib.idle_add(lambda: done() and False)
        return
    pending = [len(rows)]

    def added(manager, result):
        try:
            manager.add_cookie_finish(result)
        except GLib.Error:
            pass
        pending[0] -= 1
        if pending[0] == 0:
            done()
    for row in rows:
        try:
            cookie = Soup.Cookie.new(str(row["name"]), str(row["value"]), str(row["domain"]), str(row["path"]), -1)
            cookie.set_secure(bool(row.get("secure")))
            cookie.set_http_only(bool(row.get("http_only")))
            cookie.set_same_site_policy(Soup.SameSitePolicy(int(row.get("same_site", 0))))
        except (KeyError, TypeError, ValueError):
            pending[0] -= 1
            continue
        manager.add_cookie(cookie, None, added)
    if pending[0] == 0:
        done()


def check_signed_in(account, callback):
    """Report whether the profile holds a Nexon sign-in cookie (a hint, not proof)."""
    def check():
        manager = session_for(account).get_cookie_manager()

        def finished(manager, result):
            try:
                cookies = manager.get_cookies_finish(result)
            except GLib.Error:
                cookies = []
            callback(any(c.get_name() == "NPP" for c in cookies))
        manager.get_cookies(WORLD_HOST + "/", None, finished)
    restore_session(account, check)


def sign_out(account, done):
    """Delete the account's Nexon cookies and site data."""
    _saved_cookies(account).unlink(missing_ok=True)
    manager = session_for(account).get_website_data_manager()

    def finished(manager, result):
        try:
            manager.clear_finish(result)
        except GLib.Error:
            pass
        done()
    manager.clear(WebKit.WebsiteDataTypes.ALL, 0, None, finished)


class LaunchWindow(Adw.Window):
    """Open the world page as one account and catch the game launch link.

    ``mode`` is "play" (press the site's Play button and hand over the link) or
    "signin" (finish as soon as the profile is signed in). The page stays
    visible so the user can sign in, pass a site check, or press Play by hand.
    """

    def __init__(self, parent, account, world, *, mode="play", on_link=None, on_signed_in=None, on_closed=None):
        super().__init__(transient_for=parent, default_width=1080, default_height=780)
        self.account, self.world, self.mode = account, world, mode
        self.on_link, self.on_signed_in, self.on_closed = on_link, on_signed_in, on_closed
        self.done = False
        self.presses = {"signed_in": 0, "signed_out": 0}
        self.cooldown_until = 0.0
        self.polls = 0
        self.redirects = 0
        self.poll_source = None
        self.watchdog = None
        verb = "Sign in" if mode == "signin" else "Starting"
        self.set_title(f"{verb} · {account.name}")
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=world["name"], subtitle=account.name))
        reload = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=f"Back to {world['name']}")
        reload.connect("clicked", lambda *_: self.load_world())
        header.pack_start(reload)
        self.banner = Adw.Banner(revealed=True)
        self.view = WebKit.WebView(network_session=session_for(account))
        self.view.get_user_content_manager().add_script(WebKit.UserScript.new(
            CHROME_SHIM, WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.START, [SITE_PATTERN], None))
        self.view.connect("decide-policy", self._policy)
        self.view.connect("load-changed", self._load_changed)
        self.view.connect("create", self._popup)
        self.view.connect("web-process-terminated", lambda *_: self._status(
            "The page stopped unexpectedly. Use the reload button to try again."))
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.banner)
        toolbar.set_content(self.view)
        self.set_content(toolbar)
        self.connect("close-request", self._close_request)
        self._status(f"Opening {world['name']}…")
        restore_session(account, self.load_world)
        self.present()

    # --- page flow ---------------------------------------------------------
    def load_world(self):
        if not self.done:
            self.polls = 0
            self.view.load_uri(world_url(self.world["id"]))
        return False

    def _status(self, text):
        self.banner.set_title(text)

    def _on_world_page(self, uri) -> bool:
        return f"/play/{self.world['id']}" in (uri or "")

    def _load_changed(self, view, event):
        if event != WebKit.LoadEvent.FINISHED or self.done:
            return
        uri = view.get_uri() or ""
        if is_sign_in_page(uri):
            self.cooldown_until = 0.0  # Press Play as soon as sign-in returns to the world.
            self._status(f"Sign in as “{self.account.name}” and tick “Login automatically in the future” "
                         f"to stay signed in. {self.world['name']} starts afterwards.")
            return
        if uri.startswith(WORLD_HOST):
            self.polls = 0
            self._schedule_poll(300)

    def _schedule_poll(self, delay):
        if self.poll_source:
            GLib.source_remove(self.poll_source)
        self.poll_source = GLib.timeout_add(delay, self._poll)

    def _poll(self):
        self.poll_source = None
        if not self.done:
            self.view.evaluate_javascript(STATE_JS, -1, None, None, None, self._on_state)
        return False

    def _on_state(self, view, result):
        if self.done:
            return
        try:
            state = json.loads(view.evaluate_javascript_finish(result).to_string())
        except (GLib.Error, ValueError, TypeError):
            state = {"ready": False, "signedIn": False}
        uri = view.get_uri() or ""
        if not uri.startswith(WORLD_HOST):
            return  # Never interrupt Nexon's sign-in pages (2-step verification, etc.).
        if not self._on_world_page(uri):
            # Back on the Worlds site after signing in, but not on the world.
            if state.get("signedIn") and self.redirects < 3:
                self.redirects += 1
                self.load_world()
            elif not state.get("signedIn"):
                self.polls += 1
                if self.polls < 20:
                    self._schedule_poll(500)
            return
        if not state.get("ready"):
            self.polls += 1
            if self.polls < 80:
                self._schedule_poll(500)
            else:
                self._status("The page is taking a while. Press Play on the page, or use reload.")
            return
        if state.get("signedIn"):
            save_session(self.account)
            if self.mode == "signin":
                self._finish_signed_in()
                return
        now = time.monotonic()
        if now < self.cooldown_until:
            self._schedule_poll(int((self.cooldown_until - now) * 1000) + 100)
            return
        kind = "signed_in" if state.get("signedIn") else "signed_out"
        if self.presses[kind] >= MAX_PRESSES:
            self._status("Press Play on the page to start.")
            return
        self.presses[kind] += 1
        self.cooldown_until = now + PRESS_COOLDOWN[kind]
        if kind == "signed_in":
            self._status(f"Starting {self.world['name']}…")
            self._arm_watchdog()
        else:
            self._status("Opening Nexon sign-in…")
        # Logged in: the site builds the launch link. Logged out: it opens sign-in.
        view.evaluate_javascript(CLICK_JS, -1, None, None, None, None)

    def _arm_watchdog(self):
        if self.watchdog:
            GLib.source_remove(self.watchdog)

        def expired():
            self.watchdog = None
            if not self.done:
                self._status("Didn't start? Press Play on the page, or check for a message there.")
            return False
        self.watchdog = GLib.timeout_add_seconds(START_TIMEOUT, expired)

    # --- link capture ------------------------------------------------------
    def _policy(self, view, decision, kind):
        if kind not in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
            return False
        uri = decision.get_navigation_action().get_request().get_uri() or ""
        scheme = uri.split(":", 1)[0].lower()
        if scheme == "ngm":
            decision.ignore()
            self._captured(uri)
            return True
        if scheme not in ("http", "https", "about", "blob", "data"):
            decision.ignore()  # Never hand other app links to the desktop.
            return True
        return False

    def _captured(self, uri):
        if self.done:
            return
        try:
            validate_uri(uri)
        except HelperError as exc:
            self._status(str(exc))
            return
        self.done = True
        save_session(self.account)
        callback = self.on_link
        self._teardown()
        self.destroy()
        if callback:
            callback(uri)

    def _finish_signed_in(self):
        self.done = True
        callback = self.on_signed_in
        self._teardown()
        self.destroy()
        if callback:
            callback()

    # --- sign-in pop-ups (Google, Apple, …) ---------------------------------
    def _popup(self, view, action):
        popup = WebKit.WebView(related_view=view)
        window = Adw.Window(transient_for=self, modal=True, default_width=520, default_height=700,
                            title="Sign in")
        window.set_content(popup)
        popup.connect("ready-to-show", lambda *_: window.present())
        popup.connect("close", lambda *_: window.destroy())
        return popup

    # --- closing -------------------------------------------------------------
    def _teardown(self):
        for source in (self.poll_source, self.watchdog):
            if source:
                GLib.source_remove(source)
        self.poll_source = self.watchdog = None
        closed, self.on_closed = self.on_closed, None
        if closed:
            closed(self.done)

    def _close_request(self, *_):
        self._teardown()
        return False
