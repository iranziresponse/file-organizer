"""organizer.core.pulse -- the dashboard live strip (get_snapshot) and the
activity film (get_activity_stream).

Every badge here is proven to go from its resting state to its active state
by creating the one database row that backs it, which is the acceptance
criterion for the live dashboard work.
"""

from datetime import time, timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from organizer.core import pulse
from organizer.models import (
    AssignmentItem,
    BackgroundTask,
    MoveEvent,
    Notification,
    ReviewItem,
    TimetableEntry,
)

from .helpers import SandboxedPathsTestCase


class PulseSnapshotTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.make_settings()
        self.profile = self.make_profile()

    def test_no_profile_is_a_calm_resting_snapshot(self):
        snap = pulse.get_snapshot(None)

        self.assertFalse(snap["has_profile"])
        self.assertFalse(snap["active"])
        self.assertIsNone(snap["in_flight"])
        self.assertIsNone(snap["lecture"])
        self.assertIsNone(snap["needs_you"])
        self.assertIsNone(snap["deadline"])

    def test_sorting_badge_rests_on_the_watched_folder_name(self):
        snap = pulse.get_snapshot(self.profile)

        self.assertEqual(snap["sorting"]["state"], "resting")
        self.assertIn("Downloads", snap["sorting"]["label"])
        self.assertFalse(snap["active"])

    def test_snapshot_query_failure_is_marked_instead_of_looking_idle(self):
        with mock.patch("organizer.core.pulse._sorting_badge", side_effect=RuntimeError("database unavailable")):
            snap = pulse.get_snapshot(self.profile)

        self.assertTrue(snap["pulse_error"])
        self.assertFalse(snap["active"])

    def test_sorting_badge_goes_active_with_a_large_folder_sort_task(self):
        BackgroundTask.objects.create(
            profile=self.profile, kind="large_folder_sort", status="running",
            progress_current=3, progress_total=10,
        )

        snap = pulse.get_snapshot(self.profile)

        self.assertEqual(snap["sorting"]["state"], "active")
        self.assertEqual(snap["sorting"]["detail"], "3 of 10")
        self.assertTrue(snap["active"])

    def test_sorting_badge_goes_active_on_a_recent_move_burst(self):
        MoveEvent.objects.create(
            profile=self.profile, filename="a.pdf",
            destination_path=str(self.profile_root / "a.pdf"),
            method="course_code", success=True,
        )

        snap = pulse.get_snapshot(self.profile)

        self.assertEqual(snap["sorting"]["state"], "active")
        self.assertTrue(snap["active"])

    def test_in_flight_badge_is_hidden_until_a_task_is_queued(self):
        self.assertIsNone(pulse.get_snapshot(self.profile)["in_flight"])

        BackgroundTask.objects.create(
            profile=self.profile, kind="muele_sync", status="running",
            progress_current=0, progress_total=None,
        )

        badge = pulse.get_snapshot(self.profile)["in_flight"]
        self.assertIsNotNone(badge)
        self.assertEqual(badge["label"], "MUELE sync")
        # progress_total is null -> indeterminate -> spinner, not a 0% bar.
        self.assertTrue(badge["indeterminate"])
        self.assertIsNone(badge["detail"])

    def test_needs_you_badge_counts_queued_review_items(self):
        self.assertIsNone(pulse.get_snapshot(self.profile)["needs_you"])

        ReviewItem.objects.create(
            profile=self.profile, title="Revisit graphs", due_at=timezone.now(),
            status="queued",
        )

        badge = pulse.get_snapshot(self.profile)["needs_you"]
        self.assertEqual(badge["count"], 1)
        self.assertEqual(badge["url"], reverse("review_queue"))

    def test_deadline_badge_only_shows_within_the_horizon(self):
        AssignmentItem.objects.create(
            profile=self.profile, title="Far assignment", source="muele",
            status="open", due_at=timezone.now() + timedelta(days=40),
        )
        self.assertIsNone(pulse.get_snapshot(self.profile)["deadline"])

        AssignmentItem.objects.create(
            profile=self.profile, title="Soon assignment", source="muele",
            status="open", due_at=timezone.now() + timedelta(days=3),
        )
        badge = pulse.get_snapshot(self.profile)["deadline"]
        self.assertIn("Soon assignment", badge["label"])

    def test_lecture_badge_active_while_a_teaching_entry_is_running(self):
        now = timezone.localtime()
        TimetableEntry.objects.create(
            profile=self.profile, kind="teaching", source="manual",
            weekday=now.weekday(), raw_group="SE-2",
            start_time=(now - timedelta(minutes=10)).time(),
            end_time=(now + timedelta(minutes=40)).time(),
            course_code="CSC101", room="LLT1",
        )

        badge = pulse.get_snapshot(self.profile)["lecture"]
        self.assertEqual(badge["state"], "active")
        self.assertIn("CSC101", badge["label"])
        self.assertEqual(badge["detail"], "LLT1")

    def test_lecture_badge_rests_on_the_next_entry_today(self):
        now = timezone.localtime()
        if now.time() > time(23, 0):
            self.skipTest("no room for a 'later today' entry near midnight")
        TimetableEntry.objects.create(
            profile=self.profile, kind="teaching", source="manual",
            weekday=now.weekday(), raw_group="SE-2",
            start_time=(now + timedelta(minutes=45)).time(),
            end_time=None,
            course_code="CSC202",
        )

        badge = pulse.get_snapshot(self.profile)["lecture"]
        self.assertEqual(badge["state"], "resting")
        self.assertIn("CSC202", badge["label"])


class PulseEndpointTests(SandboxedPathsTestCase):
    def test_endpoint_returns_the_snapshot_shape(self):
        self.make_settings()
        self.make_profile()

        response = self.client.get(reverse("pulse_data"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["has_profile"])
        self.assertIn("sorting", payload)
        self.assertIn("active", payload)
        # The same poll carries the activity film's rows.
        self.assertIn("activity", payload)
        self.assertIsInstance(payload["activity"], list)

    def test_endpoint_is_safe_with_no_profile(self):
        response = self.client.get(reverse("pulse_data"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["has_profile"])

    def test_endpoint_marks_a_failed_activity_query_as_degraded(self):
        self.make_profile()

        with mock.patch("organizer.core.pulse._build_activity_stream", side_effect=RuntimeError("database unavailable")):
            response = self.client.get(reverse("pulse_data"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["activity"], [])
        self.assertTrue(response.json()["activity_error"])


class ActivityStreamTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_profile()

    def test_empty_without_activity(self):
        self.assertEqual(pulse.get_activity_stream(self.profile), [])

    def test_stream_query_failure_is_reported_separately_from_an_empty_stream(self):
        with mock.patch("organizer.core.pulse._build_activity_stream", side_effect=RuntimeError("database unavailable")):
            rows, failed = pulse.get_activity_stream_with_status(self.profile)

        self.assertEqual(rows, [])
        self.assertTrue(failed)

    def test_unions_and_orders_sources_newest_first(self):
        old = timezone.now() - timedelta(hours=5)
        mid = timezone.now() - timedelta(hours=2)

        ev = MoveEvent.objects.create(
            profile=self.profile, filename="notes.pdf",
            destination_path=str(self.profile_root / "notes.pdf"),
            method="course_code", success=True,
        )
        MoveEvent.objects.filter(pk=ev.pk).update(timestamp=old)

        note = Notification.objects.create(profile=self.profile, title="Deadline soon")
        Notification.objects.filter(pk=note.pk).update(created_at=mid)

        task = BackgroundTask.objects.create(
            profile=self.profile, kind="timetable_sync", status="done",
        )
        BackgroundTask.objects.filter(pk=task.pk).update(finished_at=timezone.now())

        stream = pulse.get_activity_stream(self.profile)

        self.assertEqual(len(stream), 3)
        self.assertEqual(stream[0]["kind"], "task_done")
        self.assertEqual(stream[1]["kind"], "notification")
        self.assertEqual(stream[2]["kind"], "move")
        for row in stream:
            self.assertIn("when_short", row)
            self.assertTrue(row["url"])

    def test_respects_the_limit(self):
        for i in range(40):
            MoveEvent.objects.create(
                profile=self.profile, filename=f"f{i}.pdf",
                destination_path=str(self.profile_root / f"f{i}.pdf"),
                method="course_code", success=True,
            )

        self.assertEqual(len(pulse.get_activity_stream(self.profile, limit=10)), 10)
