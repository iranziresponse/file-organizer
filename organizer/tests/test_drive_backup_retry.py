from unittest import mock

from django.urls import reverse

from organizer.core import drive_api, jobs, sorting
from organizer.models import BackgroundTask, MoveEvent, Notification

from .helpers import SandboxedPathsTestCase
from .test_jobs import ImmediateThread


class RetryFailedDriveBackupsTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_profile()

    def test_reports_nothing_to_retry_when_drive_backup_is_off(self):
        with mock.patch.object(drive_api, "load_drive_config", return_value=None):
            summary = sorting.retry_failed_drive_backups(self.profile)
        self.assertIn("isn't turned on", summary)

    def test_retries_a_failed_backup_and_marks_it_successful(self):
        target = self.profile_root / "notes.pdf"
        target.write_bytes(b"x")
        MoveEvent.objects.create(
            profile=self.profile, filename="notes.pdf", destination_path=str(target),
            method="course_code", success=True, drive_backup_status="failed",
        )

        with mock.patch.object(drive_api, "load_drive_config", return_value={"enabled": True}), \
                mock.patch.object(drive_api, "backup_file", return_value=True) as backup:
            summary = sorting.retry_failed_drive_backups(self.profile)

        backup.assert_called_once()
        self.assertIn("1 succeeded", summary)
        event = MoveEvent.objects.get()
        self.assertEqual(event.drive_backup_status, "success")

    def test_a_backup_that_fails_again_stays_failed(self):
        target = self.profile_root / "notes.pdf"
        target.write_bytes(b"x")
        MoveEvent.objects.create(
            profile=self.profile, filename="notes.pdf", destination_path=str(target),
            method="course_code", success=True, drive_backup_status="failed",
        )

        with mock.patch.object(drive_api, "load_drive_config", return_value={"enabled": True}), \
                mock.patch.object(drive_api, "backup_file", return_value=False):
            sorting.retry_failed_drive_backups(self.profile)

        self.assertEqual(MoveEvent.objects.get().drive_backup_status, "failed")

    def test_new_backup_failures_are_rate_limited_per_profile(self):
        events = []
        for index in range(2):
            target = self.profile_root / f"notes-{index}.pdf"
            target.write_bytes(b"x")
            events.append(MoveEvent.objects.create(
                profile=self.profile,
                filename=target.name,
                destination_path=str(target),
                method="course_code",
                success=True,
            ))

        with mock.patch.object(drive_api, "load_drive_config", return_value={"enabled": True}), \
                mock.patch.object(drive_api, "backup_file", return_value=False):
            for event in events:
                sorting._backup_and_record(event.pk, event.destination_path)

        self.assertEqual(
            Notification.objects.filter(
                profile=self.profile,
                title="Google Drive backup needs attention",
            ).count(),
            1,
        )

    def test_skips_a_file_that_no_longer_exists_where_orch_left_it(self):
        MoveEvent.objects.create(
            profile=self.profile, filename="gone.pdf",
            destination_path=str(self.profile_root / "gone.pdf"),
            method="course_code", success=True, drive_backup_status="failed",
        )

        with mock.patch.object(drive_api, "load_drive_config", return_value={"enabled": True}), \
                mock.patch.object(drive_api, "backup_file") as backup:
            summary = sorting.retry_failed_drive_backups(self.profile)

        backup.assert_not_called()
        self.assertIn("skipped", summary)

    def test_only_touches_this_profiles_failed_backups(self):
        other = self.make_profile(name="Other", is_active=False)
        target = self.profile_root / "notes.pdf"
        target.write_bytes(b"x")
        MoveEvent.objects.create(
            profile=other, filename="notes.pdf", destination_path=str(target),
            method="course_code", success=True, drive_backup_status="failed",
        )

        with mock.patch.object(drive_api, "load_drive_config", return_value={"enabled": True}), \
                mock.patch.object(drive_api, "backup_file") as backup:
            sorting.retry_failed_drive_backups(self.profile)

        backup.assert_not_called()


class DriveBackupRetryViewTests(SandboxedPathsTestCase):
    def test_get_is_not_allowed(self):
        response = self.client.get(reverse("drive_backup_retry"))
        self.assertEqual(response.status_code, 405)

    def test_without_an_active_profile_redirects(self):
        response = self.client.post(reverse("drive_backup_retry"))
        self.assertRedirects(response, reverse("dashboard"))

    def test_enqueues_a_background_task(self):
        self.make_profile()
        with mock.patch.object(jobs.threading, "Thread", ImmediateThread), \
                mock.patch.object(sorting, "retry_failed_drive_backups", return_value="Retried 0 backup(s)."):
            response = self.client.post(reverse("drive_backup_retry"))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        task = BackgroundTask.objects.get(pk=data["task_id"])
        self.assertEqual(task.kind, "drive_backup")
        self.assertEqual(task.status, "done")
