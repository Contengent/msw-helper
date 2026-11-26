import unittest

try:
    from msw_helper import web
    from gi.repository import WebKit
except (ImportError, ValueError) as exc:  # GTK/WebKit typelibs not installed.
    web = None
    SKIP = str(exc)
else:
    SKIP = ""


class FakeRequest:
    def __init__(self, uri):
        self.uri = uri

    def get_uri(self):
        return self.uri


class FakeDecision:
    def __init__(self, uri):
        self.request = FakeRequest(uri)
        self.ignored = False

    def get_navigation_action(self):
        return self

    def get_request(self):
        return self.request

    def ignore(self):
        self.ignored = True


class FakeWindow:
    def __init__(self):
        self.captured = []

    def _captured(self, uri):
        self.captured.append(uri)


@unittest.skipIf(web is None, SKIP)
class WebTests(unittest.TestCase):
    def test_sign_in_pages(self):
        for uri in ("https://www.nexon.com/account/en/login", "https://signin.nexon.com/login?gid=2037",
                    "https://accounts.google.com/o/oauth2", "https://appleid.apple.com/auth",
                    "https://www.facebook.com/dialog/oauth", "https://x.com/i/oauth2/authorize"):
            self.assertTrue(web.is_sign_in_page(uri), uri)
        for uri in ("https://maplestoryworlds.nexon.com/en/play/6697fa1ed9cd4de7bea6daffed99a637",
                    "https://maplestoryworlds.nexon.com/?next=https://x.com/",
                    "https://www.nexon.com/", "https://box.com/", "https://evilx.com/", ""):
            self.assertFalse(web.is_sign_in_page(uri), uri)

    def policy(self, uri, kind=None):
        window, decision = FakeWindow(), FakeDecision(uri)
        handled = web.LaunchWindow._policy(window, None, decision,
                                           kind or WebKit.PolicyDecisionType.NAVIGATION_ACTION)
        return handled, decision.ignored, window.captured

    def test_launch_link_is_captured_not_opened(self):
        uri = "ngm://launch/%20-mode:launch"
        self.assertEqual(self.policy(uri), (True, True, [uri]))
        self.assertEqual(self.policy(uri, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION), (True, True, [uri]))

    def test_other_app_links_are_blocked(self):
        for uri in ("nexonplug://?game=1", "mailto:a@b.c", "file:///etc/passwd", "steam://run/1"):
            self.assertEqual(self.policy(uri), (True, True, []), uri)

    def test_web_pages_load_normally(self):
        for uri in ("https://maplestoryworlds.nexon.com/en/play/x", "about:blank"):
            self.assertEqual(self.policy(uri), (False, False, []), uri)
        response = WebKit.PolicyDecisionType.RESPONSE
        self.assertEqual(self.policy("ngm://launch/", response), (False, False, []))

    def test_shim_only_targets_worlds_pages(self):
        self.assertEqual(web.SITE_PATTERN, "https://maplestoryworlds.nexon.com/*")
        self.assertIn("window.chrome", web.CHROME_SHIM)


if __name__ == "__main__":
    unittest.main()
