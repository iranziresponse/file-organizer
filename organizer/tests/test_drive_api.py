import json
from unittest import mock

import requests
from django.urls import reverse

from organizer.core import drive_api
from organizer.models import IntegrationConnection, Notification

from .helpers import SandboxedPathsTestCase


class LoadDriveConfigTests(SandboxedPathsTestCase):
    def test_returns_none_when_no_config_file_exists(self):
        self.assertIsNone(drive_api.load_drive_config())

    def test_reads_a_saved_config(self):
        self.drive_config_path.write_text(
            json.dumps({"enabled": True, "client_id": "abc", "client_secret": "xyz"}), encoding="utf-8"
        )

        config = drive_api.load_drive_config()

        self.assertEqual(config["client_id"], "abc")

    def test_corrupted_config_file_is_treated_as_missing(self):
        self.drive_config_path.write_text("not valid json", encoding="utf-8")

        self.assertIsNone(drive_api.load_drive_config())


class BuildAuthUrlTests(SandboxedPathsTestCase):
    def test_returns_none_when_not_configured(self):
        self.assertIsNone(drive_api.build_auth_url("http://127.0.0.1:8765/callback/", "state123"))

    def test_includes_client_id_redirect_and_state(self):
        self.drive_config_path.write_text(
            json.dumps({"client_id": "abc.apps.googleusercontent.com"}), encoding="utf-8"
        )

        url = drive_api.build_auth_url("http://127.0.0.1:8765/integrations/drive/callback/", "state123")

        self.assertIn("client_id=abc.apps.googleusercontent.com", url)
        self.assertIn("state=state123", url)
        self.assertIn("drive.file", url)
        self.assertIn("access_type=offline", url)

    def test_missing_keyring_package_is_treated_as_not_connected(self):
        with mock.patch.dict("sys.modules", {"keyring": None}):
            self.assertIsNone(drive_api.load_refresh_token())
            self.assertFalse(drive_api.is_connected())


class ExchangeCodeForTokensTests(SandboxedPathsTestCase):
    def test_returns_error_when_not_configured(self):
        result, error = drive_api.exchange_code_for_tokens("code123", "http://127.0.0.1:8765/callback/")

        self.assertIsNone(result)
        self.assertIsNotNone(error)

    def test_parses_a_successful_token_response(self):
        self.drive_config_path.write_text(
            json.dumps({"client_id": "abc", "client_secret": "shh"}), encoding="utf-8"
        )
        fake_response = mock.Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "access_token": "tok123",
            "refresh_token": "refresh456",
            "expires_in": 3600,
        }

        with mock.patch("organizer.core.drive_api.requests.post", return_value=fake_response):
            result, error = drive_api.exchange_code_for_tokens("code123", "http://127.0.0.1:8765/callback/")

        self.assertIsNone(error)
        self.assertEqual(result["refresh_token"], "refresh456")

    def test_network_failure_returns_an_error_never_raises(self):
        self.drive_config_path.write_text(
            json.dumps({"client_id": "abc", "client_secret": "shh"}), encoding="utf-8"
        )

        with mock.patch(
            "organizer.core.drive_api.requests.post",
            side_effect=requests.ConnectionError("no network"),
        ):
            result, error = drive_api.exchange_code_for_tokens("code123", "http://127.0.0.1:8765/callback/")

        self.assertIsNone(result)
        self.assertIsNotNone(error)


class BackupFileTests(SandboxedPathsTestCase):
    def test_returns_false_when_not_configured(self):
        self.assertFalse(drive_api.backup_file(str(self.profile_root / "notes.pdf")))

    def test_returns_false_when_disabled(self):
        self.drive_config_path.write_text(json.dumps({"enabled": False}), encoding="utf-8")

        self.assertFalse(drive_api.backup_file(str(self.profile_root / "notes.pdf")))

    def test_returns_false_when_enabled_but_not_connected(self):
        self.drive_config_path.write_text(
            json.dumps({"enabled": True, "client_id": "abc", "client_secret": "shh"}), encoding="utf-8"
        )
        # No refresh token stored (keyring isn't installed in this
        # environment), so get_valid_access_token() should come back empty.

        self.assertFalse(drive_api.backup_file(str(self.profile_root / "notes.pdf")))

    def test_returns_false_for_a_file_that_no_longer_exists(self):
        self.drive_config_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")

        self.assertFalse(drive_api.backup_file(str(self.profile_root / "does-not-exist.pdf")))

    def test_uploads_when_connected_and_enabled(self):
        self.drive_config_path.write_text(
            json.dumps({"enabled": True, "client_id": "abc", "client_secret": "shh"}), encoding="utf-8"
        )
        target = self.profile_root / "notes.pdf"
        target.write_bytes(b"hello")

        fake_search = mock.Mock()
        fake_search.raise_for_status.return_value = None
        fake_search.json.return_value = {"files": [{"id": "folder123"}]}
        fake_upload = mock.Mock()
        fake_upload.raise_for_status.return_value = None

        with mock.patch("organizer.core.drive_api.get_valid_access_token", return_value="tok123"), \
             mock.patch("organizer.core.drive_api.requests.get", return_value=fake_search), \
             mock.patch("organizer.core.drive_api.requests.post", return_value=fake_upload) as post:
            result = drive_api.backup_file(str(target))

        self.assertTrue(result)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["params"], {"uploadType": "multipart"})


class DriveStorageQuotaTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.cache = mock.patch.dict(
            drive_api._storage_quota_cache,
            {"value": None, "updated_at": 0.0, "refreshing": False},
        )
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_verified_near_full_quota_is_cached_and_warned_once(self):
        profile = self.make_profile()
        response = mock.Mock()
        response.json.return_value = {"storageQuota": {"usage": "95", "limit": "100"}}
        with mock.patch.object(drive_api, "get_valid_access_token", return_value="token"), \
                mock.patch.object(drive_api.requests, "get", return_value=response):
            drive_api._refresh_storage_quota(profile)
            drive_api._refresh_storage_quota(profile)

        self.assertEqual(drive_api._storage_quota_cache["value"]["percent"], 95)
        self.assertEqual(
            Notification.objects.filter(
                profile=profile, title="Google Drive storage is almost full"
            ).count(),
            1,
        )

    def test_failed_quota_request_is_not_reported_as_verified_usage(self):
        profile = self.make_profile()
        response = mock.Mock()
        response.raise_for_status.side_effect = requests.ConnectionError("offline")
        with mock.patch.object(drive_api, "get_valid_access_token", return_value="token"), \
                mock.patch.object(drive_api.requests, "get", return_value=response):
            drive_api._refresh_storage_quota(profile)

        self.assertIsNone(drive_api._storage_quota_cache["value"])
        self.assertFalse(Notification.objects.filter(profile=profile).exists())


class DriveConnectViewTests(SandboxedPathsTestCase):
    def test_redirects_to_settings_when_not_configured(self):
        response = self.client.get(reverse("drive_connect"))

        self.assertRedirects(response, reverse("settings_edit"))

    def test_redirects_to_google_when_configured(self):
        self.drive_config_path.write_text(
            json.dumps({"client_id": "abc.apps.googleusercontent.com"}), encoding="utf-8"
        )

        response = self.client.get(reverse("drive_connect"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("accounts.google.com", response.url)
        self.assertIn("client_id=abc.apps.googleusercontent.com", response.url)


class DriveCallbackViewTests(SandboxedPathsTestCase):
    def test_missing_or_mismatched_state_fails_without_exchanging_the_code(self):
        with mock.patch("organizer.core.drive_api.exchange_code_for_tokens") as exchange:
            response = self.client.get(reverse("drive_callback"), {"code": "abc", "state": "wrong"})

        exchange.assert_not_called()
        self.assertRedirects(response, reverse("settings_edit"))

    def test_google_reported_error_is_surfaced_without_exchanging_the_code(self):
        with mock.patch("organizer.core.drive_api.exchange_code_for_tokens") as exchange:
            response = self.client.get(reverse("drive_callback"), {"error": "access_denied"})

        exchange.assert_not_called()
        self.assertRedirects(response, reverse("settings_edit"))

    def test_successful_callback_stores_the_token_and_creates_a_connection(self):
        self.drive_config_path.write_text(
            json.dumps({"client_id": "abc", "client_secret": "shh"}), encoding="utf-8"
        )
        session = self.client.session
        session["drive_oauth_state"] = "state123"
        session.save()

        with mock.patch(
            "organizer.core.drive_api.exchange_code_for_tokens",
            return_value=({"access_token": "tok", "refresh_token": "refresh456"}, None),
        ), mock.patch("organizer.core.drive_api.store_refresh_token", return_value=(True, None)) as store, \
                mock.patch("organizer.core.drive_api.get_account_email", return_value="student@example.com"):
            response = self.client.get(reverse("drive_callback"), {"code": "abc", "state": "state123"})

        store.assert_called_once_with("refresh456")
        self.assertRedirects(response, reverse("settings_edit"))
        connection = IntegrationConnection.objects.get(provider="drive")
        self.assertEqual(connection.config["email"], "student@example.com")

    def test_missing_refresh_token_is_treated_as_a_failed_connection(self):
        self.drive_config_path.write_text(
            json.dumps({"client_id": "abc", "client_secret": "shh"}), encoding="utf-8"
        )
        session = self.client.session
        session["drive_oauth_state"] = "state123"
        session.save()

        with mock.patch(
            "organizer.core.drive_api.exchange_code_for_tokens",
            return_value=({"access_token": "tok"}, None),
        ):
            self.client.get(reverse("drive_callback"), {"code": "abc", "state": "state123"})

        self.assertFalse(IntegrationConnection.objects.filter(provider="drive").exists())
