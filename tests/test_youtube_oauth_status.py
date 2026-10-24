from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from yt_library.youtube_data_api import (
    YOUTUBE_READONLY_SCOPE,
    YouTubeDataApiNotConfigured,
    youtube_oauth_status,
)


class YouTubeOAuthStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "token.json"
        self.token = {"token": "fake-access-secret", "refresh_token": "fake-refresh-secret",
                      "client_id": "fake-client-id", "client_secret": "fake-client-secret",
                      "scopes": [YOUTUBE_READONLY_SCOPE], "expiry": "2099-01-01T00:00:00Z"}

    def status(self, **overrides):
        self.path.write_text(json.dumps({**self.token, **overrides}), encoding="utf-8")
        before = self.path.read_bytes()
        with patch("google.oauth2.credentials.Credentials.refresh") as refresh:
            result = youtube_oauth_status(self.path, enabled=True)
            refresh.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        for secret in ("fake-access-secret", "fake-refresh-secret", "fake-client-id", "fake-client-secret"):
            self.assertNotIn(secret, json.dumps(result))
        return result

    def test_disabled_does_not_read_credentials_or_import_google(self):
        with patch.object(Path, "is_file") as exists, patch("yt_library.youtube_data_api._google_dependencies") as dependencies:
            self.assertEqual(youtube_oauth_status(self.path, enabled=False)["status"], "disabled")
            exists.assert_not_called()
            dependencies.assert_not_called()

    def test_missing_token_is_not_configured(self):
        self.assertEqual(youtube_oauth_status(self.path, enabled=True)["status"], "missing")

    def test_ready_is_local_readiness_not_live_verification(self):
        result = self.status()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["expiresAt"], "2099-01-01T00:00:00Z")
        self.assertIn("not live verification", result["message"])

    def test_expired_refreshable_token_is_not_reported_as_rejected(self):
        self.assertEqual(self.status(expiry="2020-01-01T00:00:00Z")["status"], "refresh_pending")

    def test_expired_without_refresh_requires_sign_in(self):
        self.assertEqual(self.status(expiry="2020-01-01T00:00:00Z", refresh_token=None)["status"], "expired")

    def test_scope_and_incomplete_credentials(self):
        self.assertEqual(self.status(scopes=["other-scope"])["status"], "scope_missing")
        self.assertEqual(self.status(client_secret="")["status"], "invalid")

    def test_replaced_token_is_rechecked_without_a_service_restart(self):
        self.assertEqual(self.status(expiry="2020-01-01T00:00:00Z")["status"], "refresh_pending")
        self.assertEqual(self.status()["status"], "ready")

    def test_invalid_file_and_parser_errors_do_not_leak_secrets(self):
        self.path.write_text("not-json secret-value", encoding="utf-8")
        result = youtube_oauth_status(self.path, enabled=True)
        self.assertEqual(result["status"], "invalid")
        self.assertNotIn("secret-value", json.dumps(result))
        with patch("yt_library.youtube_data_api._google_dependencies", side_effect=ValueError("secret-value")):
            self.assertNotIn("secret-value", json.dumps(youtube_oauth_status(self.path, enabled=True)))

    def test_unreadable_and_missing_dependencies_are_safe(self):
        self.status()
        for error, expected in ((PermissionError("secret-value"), "unreadable"),
                                (YouTubeDataApiNotConfigured("secret-value"), "unavailable")):
            with patch("yt_library.youtube_data_api._google_dependencies", side_effect=error):
                result = youtube_oauth_status(self.path, enabled=True)
                self.assertEqual(result["status"], expected)
                self.assertNotIn("secret-value", json.dumps(result))
