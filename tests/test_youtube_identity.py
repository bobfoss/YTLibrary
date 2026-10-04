from __future__ import annotations

import http.cookiejar
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_library.youtube_identity import YoutubeAccountIdentity, account_channel_id


CHANNEL = "UC" + "a" * 22
SECOND = "UC" + "b" * 22


def account_data(channel=CHANNEL):
    return {"channelOptionsRenderer": {"avatarEndpoint": {
        "urlEndpoint": {"url": f"https://www.youtube.com/channel/{channel}"},
    }}}


def page(channel=CHANNEL):
    return ('ytcfg.set({"LOGGED_IN":true});var ytInitialData = '
            + json.dumps(account_data(channel)) + ';').encode(), "text/html"


class YoutubeIdentityTests(unittest.TestCase):
    def test_only_account_channel_settings_identify_the_channel(self):
        self.assertEqual(account_channel_id(account_data()), CHANNEL)
        with self.assertRaises(RuntimeError):
            account_channel_id({"browseEndpoint": {"browseId": CHANNEL}})
        with self.assertRaises(RuntimeError):
            account_channel_id({"accounts": [account_data(), account_data(SECOND)]})
        with self.assertRaises(RuntimeError):
            account_channel_id(account_data("invalid"))

    def test_cache_refreshes_after_cookie_change_expiry_and_does_not_expose_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            cookie = Path(temp) / "cookies.txt"
            cookie.write_text("test", encoding="utf-8")
            identity = YoutubeAccountIdentity(cookie, "configured-proxy")
            with patch("yt_library.youtube_identity.load_cookie_jar", return_value=http.cookiejar.CookieJar()), \
                 patch("yt_library.youtube_identity.socks5_proxy_handlers", return_value=[]) as proxy, \
                 patch("yt_library.youtube_identity.request_bytes", return_value=page()) as request, \
                 patch("yt_library.youtube_identity.time.monotonic", return_value=1) as clock:
                first = identity()
                self.assertEqual(set(first), {"channel_id", "checked_at"})
                self.assertEqual(first["channel_id"], CHANNEL)
                first["channel_id"] = "mutated caller copy"
                self.assertEqual(identity()["channel_id"], CHANNEL)
                self.assertEqual(request.call_count, 1)
                proxy.assert_called_once_with("configured-proxy")
                cookie.write_text("replacement cookie", encoding="utf-8")
                request.return_value = page(SECOND)
                self.assertEqual(identity()["channel_id"], SECOND)
                self.assertEqual(request.call_count, 2)
                clock.return_value = 902
                identity()
                self.assertEqual(request.call_count, 3)
                clock.return_value = 1803
                request.return_value = (b'ytcfg.set({"LOGGED_IN":false});', "text/html")
                with self.assertRaisesRegex(RuntimeError, "identity is unavailable"):
                    identity()
                with self.assertRaises(RuntimeError):
                    identity()
                self.assertEqual(request.call_count, 4)
                clock.return_value = 1864
                request.return_value = page()
                self.assertEqual(identity()["channel_id"], CHANNEL)

    def test_missing_cookie_does_not_reuse_cached_identity(self):
        identity = YoutubeAccountIdentity(Path("this-cookie-does-not-exist.txt"))
        with self.assertRaisesRegex(RuntimeError, "cookie file is unavailable"):
            identity()
