from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from organizer.core import muele_api, muele_sync
from organizer.models import AppSettings, IntegrationConnection, MueleCourse
from organizer.views.dashboard import _service_mesh_context
from organizer.views.integrations import _publishing_ready

from .helpers import SandboxedPathsTestCase


class MueleCourseImportHonestyTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_profile(setup_path="makerere")
        self.connection = IntegrationConnection.objects.create(
            profile=self.profile,
            provider="muele",
            display_name="Makerere MUELE",
            status="connected",
        )

    def test_failed_course_request_sets_error_without_faking_a_sync(self):
        known_good = timezone.now() - timedelta(days=3)
        self.connection.last_sync_at = known_good
        self.connection.save(update_fields=["last_sync_at"])

        with mock.patch.object(muele_api, "load_connection_token", return_value="valid-token"), \
             mock.patch.object(muele_api, "get_courses", return_value=([], "MUELE is unavailable")):
            result = muele_sync.import_courses_for_profile(self.profile)

        self.connection.refresh_from_db()
        self.assertEqual(result["errors"], ["MUELE is unavailable"])
        self.assertEqual(self.connection.status, "error")
        self.assertEqual(self.connection.last_sync_at, known_good)

    def test_successful_empty_course_list_is_a_verified_sync(self):
        with mock.patch.object(muele_api, "load_connection_token", return_value="valid-token"), \
             mock.patch.object(muele_api, "get_courses", return_value=([], None)):
            result = muele_sync.import_courses_for_profile(self.profile)

        self.connection.refresh_from_db()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.connection.status, "connected")
        self.assertIsNotNone(self.connection.last_sync_at)

    def test_missing_token_is_not_left_as_connected(self):
        with mock.patch.object(muele_api, "load_connection_token", return_value=None):
            result = muele_sync.import_courses_for_profile(self.profile)

        self.connection.refresh_from_db()
        self.assertEqual(result["errors"], ["No MUELE token configured"])
        self.assertEqual(self.connection.status, "error")
        self.assertIsNone(self.connection.last_sync_at)


class MueleConnectionSaveTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_profile(setup_path="makerere")
        self.url = reverse("muele_connect")

    def _save_payload(self):
        return {
            "action": "save_connection",
            "username": "Student",
            "college": "COCIS",
            "sync_targets": ["course_files", "assignments"],
        }

    def test_course_fetch_failure_does_not_create_a_connected_connection(self):
        with mock.patch.object(muele_api, "load_profile_pending_token", return_value="pending-token"), \
             mock.patch.object(muele_api, "verify_token", return_value=({"fullname": "Student"}, None)), \
             mock.patch.object(muele_api, "get_courses", return_value=([], "MUELE is unavailable")), \
             mock.patch.object(muele_api, "store_connection_token") as store_token:
            response = self.client.post(self.url, self._save_payload())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.url)
        self.assertFalse(IntegrationConnection.objects.filter(
            profile=self.profile, provider="muele"
        ).exists())
        store_token.assert_not_called()

    def test_keyring_save_failure_does_not_claim_the_new_connection(self):
        with mock.patch.object(muele_api, "load_profile_pending_token", return_value="pending-token"), \
             mock.patch.object(muele_api, "verify_token", return_value=({"fullname": "Student"}, None)), \
             mock.patch.object(muele_api, "get_courses", return_value=([], None)), \
             mock.patch.object(muele_api, "load_connection_token", return_value=None), \
             mock.patch.object(muele_api, "store_connection_token", return_value=(False, "keyring unavailable")):
            response = self.client.post(self.url, self._save_payload())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.url)
        self.assertFalse(IntegrationConnection.objects.filter(
            profile=self.profile, provider="muele"
        ).exists())

    def test_failed_reconnect_preserves_the_existing_connection_and_sync_time(self):
        known_good = timezone.now() - timedelta(days=2)
        connection = IntegrationConnection.objects.create(
            profile=self.profile,
            provider="muele",
            display_name="Makerere MUELE",
            status="connected",
            username="Existing account",
            last_sync_at=known_good,
            config={"college": "Old college", "sync_targets": ["course_files"]},
        )

        with mock.patch.object(muele_api, "load_profile_pending_token", return_value="pending-token"), \
             mock.patch.object(muele_api, "verify_token", return_value=({"fullname": "Student"}, None)), \
             mock.patch.object(muele_api, "get_courses", return_value=([], "MUELE is unavailable")), \
             mock.patch.object(muele_api, "store_connection_token") as store_token:
            response = self.client.post(self.url, self._save_payload())

        connection.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.url)
        self.assertEqual(connection.status, "connected")
        self.assertEqual(connection.username, "Existing account")
        self.assertEqual(connection.last_sync_at, known_good)
        self.assertEqual(connection.config["college"], "Old college")
        store_token.assert_not_called()

    def test_successfully_verified_courses_are_saved_with_connection_token(self):
        courses = [{
            "id": 27,
            "fullname": "Artificial Intelligence",
            "shortname": "CSC2114",
        }]
        with mock.patch.object(muele_api, "load_profile_pending_token", return_value="pending-token"), \
             mock.patch.object(muele_api, "verify_token", return_value=({"fullname": "Student"}, None)), \
             mock.patch.object(muele_api, "get_courses", return_value=(courses, None)), \
             mock.patch.object(muele_api, "load_connection_token", return_value=None), \
             mock.patch.object(muele_api, "store_connection_token", return_value=(True, None)) as store_token, \
             mock.patch.object(muele_api, "clear_profile_pending_token") as clear_token:
            response = self.client.post(self.url, self._save_payload())

        connection = IntegrationConnection.objects.get(profile=self.profile, provider="muele")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("muele_courses"))
        self.assertEqual(connection.status, "connected")
        self.assertIsNotNone(connection.last_sync_at)
        self.assertEqual(connection.username, "Student")
        self.assertEqual(MueleCourse.objects.get(connection=connection).course_code, "CSC2114")
        store_token.assert_called_once_with(connection, "pending-token")
        clear_token.assert_called_once_with(self.profile)

    def test_login_does_not_succeed_when_site_info_verification_fails(self):
        with mock.patch.object(muele_api, "generate_token", return_value=("unverified-token", None)), \
             mock.patch.object(muele_api, "verify_token", return_value=(None, "token rejected")), \
             mock.patch.object(muele_api, "clear_profile_pending_token") as clear_token, \
             mock.patch.object(muele_api, "load_profile_pending_token", return_value=None):
            response = self.client.post(self.url, {
                "action": "login",
                "login_username": "student",
                "login_password": "password",
            })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["token_status"], "invalid")
        self.assertIsNone(response.context["user_info"])
        clear_token.assert_called_once_with(self.profile)


class ConnectionStatusTruthTests(SandboxedPathsTestCase):
    def test_folder_watcher_is_not_active_when_a_watched_folder_is_unwritable(self):
        self.make_profile()
        app_settings = AppSettings.get_solo()
        app_settings.downloads_path = "C:/Downloads"
        app_settings.save(update_fields=["downloads_path"])
        folder_health = [{
            "exists": True,
            "is_dir": True,
            "readable": True,
            "writable": False,
        }]

        with mock.patch(
            "organizer.views.integrations.diagnostics.check_all_watched_folders",
            return_value=folder_health,
        ), mock.patch(
            "organizer.views.integrations.diagnostics.get_watcher_status",
            return_value={"running": True},
        ):
            response = self.client.get(reverse("connections_home"))

        cards = {
            card["title"]: card
            for group in response.context["groups"]
            for card in group["cards"]
        }
        self.assertEqual(cards["Local Folder Watcher"]["status"], "error")
        self.assertEqual(cards["Local Folder Watcher"]["status_label"], "Not running")
        self.assertIn("folders or heartbeat need attention", cards["Local Folder Watcher"]["detail"])

    def test_saved_muele_and_timetable_details_do_not_look_connected(self):
        profile = self.make_profile(setup_path="makerere")
        IntegrationConnection.objects.create(
            profile=profile,
            provider="muele",
            display_name="Makerere MUELE",
            username="student",
            last_sync_at=timezone.now() - timedelta(days=5),
            status="error",
        )
        IntegrationConnection.objects.create(
            profile=profile,
            provider="mak_timetable",
            display_name="Makerere Timetable",
            status="configured",
            config={"group": "SE-2"},
        )

        with mock.patch.object(muele_api, "load_connection_token", return_value=None):
            response = self.client.get(reverse("connections_home"))
        cards = {
            card["title"]: card
            for group in response.context["groups"]
            for card in group["cards"]
        }

        self.assertEqual(cards["Makerere MUELE"]["status"], "error")
        self.assertEqual(cards["Makerere MUELE"]["status_label"], "Needs attention")
        self.assertEqual(cards["Makerere Timetable"]["status"], "saved")
        self.assertEqual(cards["Makerere Timetable"]["status_label"], "Saved · not verified")

    def test_dashboard_mesh_does_not_promote_saved_learning_details_to_live(self):
        profile = self.make_profile(setup_path="makerere")
        IntegrationConnection.objects.create(
            profile=profile,
            provider="muele",
            display_name="Makerere MUELE",
            username="student",
            status="error",
        )
        IntegrationConnection.objects.create(
            profile=profile,
            provider="mak_timetable",
            display_name="Makerere Timetable",
            status="configured",
            config={"group": "SE-2"},
        )
        app_status = [{
            "label": "Downloads folder",
            "value": "Watching Downloads",
            "detail": "Heartbeat received",
            "state": "live",
        }]

        with mock.patch("organizer.core.diagnostics.get_watcher_status", return_value={"running": True}), \
             mock.patch.object(muele_api, "load_connection_token", return_value=None):
            mesh = _service_mesh_context(profile, app_status)

        learning_items = {
            item["name"]: item
            for lane in mesh["lanes"]
            if lane["title"] == "Learning tools"
            for item in lane["items"]
        }
        self.assertEqual(learning_items["Makerere MUELE"]["state"], "warning")
        self.assertEqual(learning_items["Makerere Timetable"]["state"], "warning")

    def test_publishing_channel_is_ready_only_after_it_is_connected(self):
        profile = self.make_profile()
        configured = IntegrationConnection.objects.create(
            profile=profile,
            provider="custom_website",
            display_name="Personal website",
            status="configured",
        )
        channels = IntegrationConnection.objects.filter(pk=configured.pk)
        self.assertFalse(_publishing_ready(channels))

        configured.status = "connected"
        configured.save(update_fields=["status"])
        self.assertTrue(_publishing_ready(channels))
