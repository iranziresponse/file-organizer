"""MUELE credentials are scoped to their local profile and connection."""

from threading import Event
from types import SimpleNamespace
from unittest import mock

from organizer.core import muele_api, muele_downloader
from organizer.models import IntegrationConnection

from .helpers import SandboxedPathsTestCase


def install_fake_keyring(test_case):
    values = {}
    keyring = SimpleNamespace(
        set_password=lambda service, key, value: values.__setitem__((service, key), value),
        get_password=lambda service, key: values.get((service, key)),
        delete_password=lambda service, key: values.pop((service, key), None),
        errors=SimpleNamespace(PasswordDeleteError=type("PasswordDeleteError", (Exception,), {})),
    )
    test_case.enterContext(mock.patch.dict("sys.modules", {"keyring": keyring}))


class ConnectionTokenKeyringTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        install_fake_keyring(self)
        self.profile_a = self.make_profile(name="Profile A")
        self.profile_b = self.make_profile(name="Profile B", is_active=False)
        self.connection_a = IntegrationConnection.objects.create(
            profile=self.profile_a, provider="muele", display_name="MUELE", status="connected",
        )
        self.connection_b = IntegrationConnection.objects.create(
            profile=self.profile_b, provider="muele", display_name="MUELE", status="connected",
        )

    def test_each_connection_keeps_its_own_token(self):
        muele_api.store_connection_token(self.connection_a, "token-for-a")
        muele_api.store_connection_token(self.connection_b, "token-for-b")

        self.assertEqual(muele_api.load_connection_token(self.connection_a), "token-for-a")
        self.assertEqual(muele_api.load_connection_token(self.connection_b), "token-for-b")

    def test_clearing_one_connections_token_does_not_touch_the_other(self):
        muele_api.store_connection_token(self.connection_a, "token-for-a")
        muele_api.store_connection_token(self.connection_b, "token-for-b")

        muele_api.clear_connection_token(self.connection_a)

        self.assertIsNone(muele_api.load_connection_token(self.connection_a))
        self.assertEqual(muele_api.load_connection_token(self.connection_b), "token-for-b")

    def test_pending_login_tokens_are_isolated_by_profile(self):
        values = {}
        keyring = SimpleNamespace(
            set_password=lambda service, key, value: values.__setitem__((service, key), value),
            get_password=lambda service, key: values.get((service, key)),
            delete_password=lambda service, key: values.pop((service, key), None),
            errors=SimpleNamespace(PasswordDeleteError=type("PasswordDeleteError", (Exception,), {})),
        )
        with mock.patch.dict("sys.modules", {"keyring": keyring}):
            muele_api.store_profile_pending_token(self.profile_a, "pending-a")
            muele_api.store_profile_pending_token(self.profile_b, "pending-b")

            self.assertEqual(muele_api.load_profile_pending_token(self.profile_a), "pending-a")
            self.assertEqual(muele_api.load_profile_pending_token(self.profile_b), "pending-b")
            muele_api.clear_profile_pending_token(self.profile_a)

            self.assertIsNone(muele_api.load_profile_pending_token(self.profile_a))
            self.assertEqual(muele_api.load_profile_pending_token(self.profile_b), "pending-b")


class LegacyGlobalTokenIsolationTests(SandboxedPathsTestCase):
    """An old token without an owner must never be attached to a profile."""

    def setUp(self):
        super().setUp()
        install_fake_keyring(self)
        self.profile = self.make_profile()
        self.connection = IntegrationConnection.objects.create(
            profile=self.profile, provider="muele", display_name="MUELE", status="connected",
        )

    def test_does_not_adopt_an_unowned_global_token_when_another_profile_exists(self):
        other_profile = self.make_profile(name="Another profile", is_active=False)
        IntegrationConnection.objects.create(
            profile=other_profile, provider="muele", display_name="MUELE",
        )
        with mock.patch.object(muele_api, "load_token", return_value="unowned-token") as load_token, \
             mock.patch.object(muele_api, "clear_token") as clear_token:
            result = muele_api.load_connection_token(self.connection)

        self.assertIsNone(result)
        load_token.assert_not_called()
        clear_token.assert_not_called()


class ConnectionTokenKeyringFallbackTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        install_fake_keyring(self)
        self.profile = self.make_profile()
        self.connection = IntegrationConnection.objects.create(
            profile=self.profile, provider="muele", display_name="MUELE",
        )

    def test_store_returns_false_and_a_message_without_keyring(self):
        with mock.patch.dict("sys.modules", {"keyring": None}):
            ok, message = muele_api.store_connection_token(self.connection, "a-token")

        self.assertFalse(ok)
        self.assertIsNotNone(message)

    def test_load_returns_none_without_keyring(self):
        with mock.patch.dict("sys.modules", {"keyring": None}):
            self.assertIsNone(muele_api.load_connection_token(self.connection))

    def test_clear_never_raises_without_keyring(self):
        with mock.patch.dict("sys.modules", {"keyring": None}):
            muele_api.clear_connection_token(self.connection)  # should not raise


class RunMueleSyncMultiProfileTests(SandboxedPathsTestCase):
    def test_background_loop_uses_each_connections_own_token(self):
        profile_a = self.make_profile(name="Profile A")
        profile_b = self.make_profile(name="Profile B", is_active=False)
        IntegrationConnection.objects.create(
            profile=profile_a, provider="muele", display_name="MUELE", status="connected",
        )
        IntegrationConnection.objects.create(
            profile=profile_b, provider="muele", display_name="MUELE", status="connected",
        )

        tokens_by_profile = {"Profile A": "token-for-a", "Profile B": "token-for-b"}
        calls = []

        def fake_load_connection_token(connection):
            return tokens_by_profile[connection.profile.name]

        def fake_sync_profile_courses(profile, token=None, log=None):
            calls.append((profile.name, token))
            return {"downloaded": 0, "skipped": 0, "errors": 0}

        def fake_sync_assignments(profile, token=None, log=None):
            return 0

        stop_event = Event()

        def stop_after_one_pass(*args, **kwargs):
            stop_event.set()
            return 300  # poll_seconds arg to stop_event.wait, irrelevant once set

        with mock.patch.object(muele_api, "load_connection_token", side_effect=fake_load_connection_token), \
             mock.patch.object(muele_downloader, "sync_profile_courses", side_effect=fake_sync_profile_courses), \
             mock.patch.object(muele_downloader, "sync_assignments", side_effect=fake_sync_assignments), \
             mock.patch.object(stop_event, "wait", side_effect=stop_after_one_pass):
            muele_downloader.run_muele_sync(stop_event=stop_event, poll_seconds=0)

        self.assertEqual(len(calls), 2)
        self.assertIn(("Profile A", "token-for-a"), calls)
        self.assertIn(("Profile B", "token-for-b"), calls)
