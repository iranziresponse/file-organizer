import json
from datetime import timedelta
from unittest import mock

from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from organizer.core import paths
from organizer.models import (
    AssignmentItem,
    CourseConfig,
    IntegrationConnection,
    MoveEvent,
    Notification,
    Profile,
    ResourceRecommendation,
    SortDecision,
    TimetableEntry,
)

from .helpers import SandboxedPathsTestCase


class ActivityPingViewTests(SandboxedPathsTestCase):
    def test_no_profile_returns_null(self):
        response = self.client.get(reverse("activity_ping"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["latest"])

    def test_returns_the_most_recent_move_timestamp(self):
        profile = self.make_profile()
        MoveEvent.objects.create(
            profile=profile,
            filename="notes.pdf",
            destination_path=str(self.profile_root / "notes.pdf"),
            method="course_code",
            success=True,
        )

        response = self.client.get(reverse("activity_ping"))

        self.assertIsNotNone(response.json()["latest"])

    def test_no_activity_yet_for_an_active_profile_returns_null(self):
        self.make_profile()
        response = self.client.get(reverse("activity_ping"))
        self.assertIsNone(response.json()["latest"])


class WorkspaceControllerViewTests(SandboxedPathsTestCase):
    @mock.patch("organizer.core.drive_api.storage_quota_snapshot", return_value=None)
    def test_dashboard_controller_surfaces_real_schedule_resources_and_backup_state(self, _quota):
        profile = self.make_profile()
        now = timezone.localtime()
        lesson_time = (now + timedelta(hours=1)).time().replace(second=0, microsecond=0)
        TimetableEntry.objects.create(
            profile=profile,
            kind="teaching",
            source="manual",
            weekday=now.weekday(),
            start_time=lesson_time,
            raw_group="SE-2",
            course_code="CSC2114",
        )
        ResourceRecommendation.objects.create(
            profile=profile,
            subject_code="CSC2114",
            theme="machine learning",
            source_type="youtube",
            title="Machine learning fundamentals",
            query="machine learning fundamentals",
            url="https://www.youtube.com/watch?v=example",
            reason="Based on your saved topics",
        )
        MoveEvent.objects.create(
            profile=profile,
            filename="notes.pdf",
            source_path=str(self.downloads / "notes.pdf"),
            destination_path=str(self.profile_root / "notes.pdf"),
            method="course_code",
            success=True,
            drive_backup_status="failed",
        )
        Notification.objects.create(profile=profile, title="New update")

        response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        cards = response.context["controller_items"]
        self.assertTrue(any(card["kind"] == "schedule" and "CSC2114" in card["detail"] for card in cards))
        self.assertTrue(any(card["kind"] == "resource" and card["external"] for card in cards))
        self.assertTrue(any(card["kind"] == "storage" and "backup" in card["title"].lower() for card in cards))
        self.assertTrue(any(card["kind"] == "notifications" for card in cards))
        self.assertContains(response, "Workspace controller")
        self.assertContains(response, 'target="_blank" rel="noopener noreferrer"')

    @mock.patch("organizer.core.drive_api.storage_quota_snapshot", return_value={"percent": 100})
    def test_dashboard_controller_only_calls_drive_full_when_quota_is_verified(self, _quota):
        profile = self.make_profile()

        response = self.client.get(reverse("dashboard"))

        storage = next(
            item for item in response.context["controller_items"] if item["kind"] == "storage"
        )
        self.assertEqual(storage["title"], "Google Drive is full")
        self.assertIn("Free Drive space", storage["detail"])

    @mock.patch("organizer.core.drive_api.storage_quota_snapshot", return_value=None)
    def test_pulse_endpoint_refreshes_controller_cards(self, _quota):
        profile = self.make_profile()
        AssignmentItem.objects.create(
            profile=profile,
            title="Project submission",
            due_at=timezone.now() + timedelta(hours=3),
        )

        response = self.client.get(reverse("pulse_data"))

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(any(item["kind"] == "deadline" for item in data["controller"]))


class MoveHistoryViewTests(SandboxedPathsTestCase):
    def test_history_loads_one_bounded_page_and_supports_next(self):
        profile = self.make_profile()
        for index in range(56):
            MoveEvent.objects.create(
                profile=profile,
                filename=f"file-{index}.pdf",
                destination_path=str(self.profile_root / f"file-{index}.pdf"),
                method="course_code",
                success=True,
            )

        first_page = self.client.get(reverse("move_history"))

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context["page_obj"].paginator.count, 56)
        self.assertEqual(len(first_page.context["page_obj"].object_list), 50)
        self.assertContains(first_page, "Next")
        self.assertEqual(first_page.content.count(b'class="recent-move-row"'), 50)

        second_page = self.client.get(reverse("move_history"), {"page": 2})

        self.assertEqual(len(second_page.context["page_obj"].object_list), 6)
        self.assertContains(second_page, "Previous")
        self.assertNotContains(second_page, ">Next<")

    def test_history_requires_an_active_profile(self):
        response = self.client.get(reverse("move_history"))

        self.assertRedirects(response, reverse("dashboard"))

    @mock.patch("organizer.views.files.connection")
    def test_compact_database_runs_sqlite_vacuum(self, database):
        self.make_profile()
        database.vendor = "sqlite"
        database.in_atomic_block = False

        response = self.client.post(reverse("move_history_compact"))

        self.assertRedirects(response, reverse("move_history"))
        database.cursor.return_value.__enter__.return_value.execute.assert_called_once_with("VACUUM")

    def test_compaction_rejects_get_requests(self):
        response = self.client.get(reverse("move_history_compact"))

        self.assertEqual(response.status_code, 405)


class MoveHistoryCompactionIntegrationTests(TransactionTestCase):
    def test_sqlite_database_compaction_completes(self):
        Profile.objects.create(
            name="Compaction profile",
            root_path="unused",
            is_active=True,
        )

        response = self.client.post(reverse("move_history_compact"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Database compacted.")


class StatusBarViewTests(SandboxedPathsTestCase):
    def test_no_profile_returns_has_profile_false(self):
        response = self.client.get(reverse("status_bar_data"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["has_profile"])

    def test_reports_watched_folder_and_last_move(self):
        profile = self.make_profile()
        self.make_settings()
        MoveEvent.objects.create(
            profile=profile,
            filename="notes.pdf",
            destination_path=str(self.profile_root / "notes.pdf"),
            method="course_code",
            success=True,
        )

        data = self.client.get(reverse("status_bar_data")).json()

        self.assertTrue(data["has_profile"])
        self.assertEqual(data["watching"], self.downloads.name)
        self.assertIn("notes.pdf", data["last_move"])

    def test_reports_pending_review_count(self):
        from django.utils import timezone

        from organizer.models import ReviewItem

        profile = self.make_profile()
        ReviewItem.objects.create(profile=profile, title="Chapter 3", status="queued", due_at=timezone.now())
        ReviewItem.objects.create(profile=profile, title="Chapter 4", status="done", due_at=timezone.now())

        data = self.client.get(reverse("status_bar_data")).json()

        self.assertEqual(data["pending_review"], 1)

    def test_reports_sync_state_from_connected_integrations(self):
        profile = self.make_profile()

        data = self.client.get(reverse("status_bar_data")).json()
        self.assertEqual(data["sync"], "Local only")

        IntegrationConnection.objects.create(
            profile=profile, provider="muele", display_name="MUELE", status="connected",
        )

        data = self.client.get(reverse("status_bar_data")).json()
        self.assertEqual(data["sync"], "1 connected")

    def test_reports_an_active_background_task(self):
        from organizer.models import BackgroundTask

        profile = self.make_profile()
        BackgroundTask.objects.create(
            profile=profile, kind="muele_sync", status="running",
            progress_current=2, progress_total=5,
        )

        data = self.client.get(reverse("status_bar_data")).json()

        self.assertIsNotNone(data["background_task"])
        self.assertEqual(data["background_task"]["progress_current"], 2)
        self.assertEqual(data["background_task"]["progress_total"], 5)

    def test_base_template_renders_the_status_bar_skeleton(self):
        # The bar itself starts hidden and is shown/populated client-side by
        # status-bar.js (see get_snapshot()'s query cost above) -- rendering
        # it unconditionally here keeps page responses free of any extra
        # queries, unlike an earlier context-processor-based approach that
        # broke organizer.tests.test_query_budgets's per-page query counts.
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'id="app-status-bar"')


class DashboardViewTests(SandboxedPathsTestCase):
    def test_no_profiles_at_all_prompts_the_wizard(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_any_profile"])
        self.assertContains(response, "Create your first profile")

    def test_profiles_exist_but_none_active_prompts_a_choice(self):
        self.make_profile(is_active=False)
        response = self.client.get(reverse("dashboard"))
        self.assertTrue(response.context["has_any_profile"])
        self.assertIsNone(response.context["profile"])
        self.assertContains(response, "Choose a profile")

    def test_app_status_counts_a_connected_drive_even_though_its_profile_less(self):
        # Drive is one Google account per machine, not per academic profile
        # -- its IntegrationConnection row is deliberately profile=None
        # (see views.py's drive_connect). App Status's "Sync" tile must
        # still count it, not just profile-scoped connections like MUELE.
        self.make_profile()
        IntegrationConnection.objects.create(
            profile=None, provider="drive", display_name="Google Drive", status="connected",
        )

        response = self.client.get(reverse("dashboard"))

        sync_item = next(item for item in response.context["app_status_items"] if item["label"] == "Sync")
        self.assertEqual(sync_item["value"], "Connected")
        self.assertIn("Cloud drive", sync_item["detail"])

    def test_shows_recent_moves_and_stats_for_the_active_profile(self):
        profile = self.make_profile()
        other = self.make_profile(name="Other", root_path=str(self.profile_root) + "2", is_active=False)

        MoveEvent.objects.create(
            filename="notes.pdf",
            source_path="C:/Downloads/notes.pdf",
            destination_path=str(self.profile_root / "notes.pdf"),
            method="course_code",
            course_code="CSC2100",
            success=True,
            profile=profile,
        )
        MoveEvent.objects.create(
            filename="other-notes.pdf",
            method="course_code",
            course_code="ZZZ",
            success=True,
            profile=other,
        )

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "notes.pdf")
        self.assertNotContains(response, "other-notes.pdf")
        self.assertEqual(response.context["total_moves"], 1)
        self.assertEqual(response.context["method_counts"][0]["method"], "course_code")
        self.assertEqual(response.context["course_counts"][0]["course_code"], "CSC2100")

    def test_dashboard_renders_live_cockpit_panels(self):
        self.make_profile()

        response = self.client.get(reverse("dashboard"))

        # Tier 1 live strip: the sorting badge in its calm resting state.
        self.assertContains(response, "Watching Downloads")
        # Tier 2: the single primary focus card and the activity film.
        self.assertContains(response, "Next best step")
        self.assertContains(response, "What Orch has been doing")
        # Tier 3: the collapsed "More detail" disclosure and, nested inside,
        # the plain status tiles.
        self.assertContains(response, "More detail")
        self.assertContains(response, "Plain status view")
        self.assertContains(response, "Downloads folder")

    def test_dashboard_shows_a_bounded_glance_of_workspace_tools(self):
        self.make_profile()

        response = self.client.get(reverse("dashboard"))

        tool_items = response.context["dashboard_tool_items"]
        self.assertTrue(tool_items)
        self.assertLessEqual(len(tool_items), 4)
        self.assertContains(response, "Your tools, at a glance")
        self.assertEqual(response.content.count(b"data-tool-glance-item"), len(tool_items))
        self.assertNotIn("Downloads watcher", {item["name"] for item in tool_items})

    def test_dashboard_priority_deck_has_no_duplicate_signals(self):
        # "Academic priority" duplicated the top dashboard panel right
        # above it, and "Safety layer" duplicated the file-watcher tile
        # already shown in the header strip -- both were removed as pure
        # restatements. "Projects" and "Files to check" are the only
        # signals not shown anywhere else on the page, so they stay.
        self.make_profile()

        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, "Academic priority")
        self.assertNotContains(response, "Safety layer")
        self.assertContains(response, "Projects")
        self.assertContains(response, "Files to check")
        self.assertEqual(len(response.context["priority_cards"]), 2)

    def test_search_filters_the_table_but_not_the_stat_boxes(self):
        profile = self.make_profile()
        MoveEvent.objects.create(
            filename="biology_notes.pdf",
            destination_path=str(self.profile_root / "biology_notes.pdf"),
            method="course_code",
            success=True,
            profile=profile,
        )
        MoveEvent.objects.create(
            filename="chemistry_report.docx",
            destination_path=str(self.profile_root / "chemistry_report.docx"),
            method="course_code",
            success=True,
            profile=profile,
        )

        response = self.client.get(reverse("dashboard"), {"q": "bio"})

        table_rows = list(response.context["page_obj"].object_list)
        self.assertEqual([e.filename for e in table_rows], ["biology_notes.pdf"])
        # The overall total and "most recent move" stat still reflect both
        # files -- search narrows the table only, not the profile's real
        # stats, so chemistry_report.docx legitimately still shows up there
        # as the actual most recent move regardless of the search.
        self.assertEqual(response.context["total_moves"], 2)

    def test_search_with_no_matches_shows_a_clear_empty_state(self):
        profile = self.make_profile()
        MoveEvent.objects.create(
            filename="biology_notes.pdf",
            destination_path=str(self.profile_root / "biology_notes.pdf"),
            method="course_code",
            success=True,
            profile=profile,
        )

        response = self.client.get(reverse("dashboard"), {"q": "nonexistent"})

        self.assertContains(response, "No files match")
        self.assertEqual(list(response.context["page_obj"].object_list), [])

    def test_why_panel_shows_explanation_confidence_and_matched_rule(self):
        profile = self.make_profile()
        event = MoveEvent.objects.create(
            filename="notes.pdf",
            destination_path=str(self.profile_root / "notes.pdf"),
            method="course_code",
            success=True,
            profile=profile,
            explanation="Matched subject code CSC2100 in the filename.",
            confidence=92,
        )
        SortDecision.objects.create(
            profile=profile, move_event=event, filename="notes.pdf",
            decision_type="profile_auto", confidence=92, status="moved",
            matched_rule="Filename contains CSC2100",
        )

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "Matched subject code CSC2100 in the filename.")
        self.assertContains(response, "Filename contains CSC2100")
        self.assertContains(response, "92%")

    def test_why_button_is_absent_when_there_is_nothing_to_explain(self):
        profile = self.make_profile()
        MoveEvent.objects.create(
            filename="notes.pdf",
            destination_path=str(self.profile_root / "notes.pdf"),
            method="course_code",
            success=True,
            profile=profile,
        )

        response = self.client.get(reverse("dashboard"))

        self.assertNotContains(response, "why-toggle-btn")

    def test_recent_moves_shows_five_rows_and_a_view_all_link(self):
        profile = self.make_profile()
        for i in range(8):
            MoveEvent.objects.create(
                profile=profile, filename=f"file{i}.pdf",
                destination_path=str(self.profile_root / f"file{i}.pdf"),
                method="course_code", success=True,
            )

        response = self.client.get(reverse("dashboard"))

        # Table trimmed to 5 rows on the dashboard...
        self.assertEqual(response.content.count(b'class="recent-move-row"'), 5)
        # ...with a link to the full page and the paginator still intact.
        self.assertContains(response, reverse("undo_recent"))
        self.assertEqual(response.context["page_obj"].paginator.count, 8)


class SortingReportViewTests(SandboxedPathsTestCase):
    def test_sorting_report_carries_the_pulse_and_distribution_tables(self):
        profile = self.make_profile()
        MoveEvent.objects.create(
            profile=profile, filename="bio.pdf",
            destination_path=str(self.profile_root / "bio.pdf"),
            method="course_code", success=True, course_code="BIO101",
        )

        response = self.client.get(reverse("sorting_report"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sorting pulse")
        self.assertContains(response, "Subject distribution")
        self.assertEqual(response.context["total_moves"], 1)

    def test_sorting_report_is_safe_without_a_profile(self):
        response = self.client.get(reverse("sorting_report"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No active profile")


class SetupItemDismissViewTests(SandboxedPathsTestCase):
    def test_dismissing_an_optional_item_hides_it_from_the_dashboard(self):
        profile = self.make_profile()
        response = self.client.get(reverse("dashboard"))
        muted_item = next(
            item
            for lane in response.context["service_mesh"]["lanes"]
            for item in lane["items"]
            if item["dismissible"]
        )

        self.client.post(reverse("setup_item_dismiss"), {"item_key": muted_item["key"]})
        profile.refresh_from_db()

        self.assertIn(muted_item["key"], profile.dismissed_setup_items)
        response = self.client.get(reverse("dashboard"))
        all_keys = [
            item["key"]
            for lane in response.context["service_mesh"]["lanes"]
            for item in lane["items"]
        ]
        self.assertNotIn(muted_item["key"], all_keys)
        self.assertEqual(response.context["service_mesh"]["hidden_count"], 1)

    def test_a_warning_state_item_cannot_be_dismissed(self):
        self.make_profile()
        response = self.client.get(reverse("dashboard"))
        warning_items = [
            item
            for lane in response.context["service_mesh"]["lanes"]
            for item in lane["items"]
            if item["state"] == "warning"
        ]
        self.assertTrue(all(not item["dismissible"] for item in warning_items))

    def test_restoring_brings_back_every_hidden_item(self):
        profile = self.make_profile()
        profile.dismissed_setup_items = ["resource-radar", "google-drive-backup"]
        profile.save(update_fields=["dismissed_setup_items"])

        self.client.post(reverse("setup_items_restore"))
        profile.refresh_from_db()

        self.assertEqual(profile.dismissed_setup_items, [])

    def test_dismiss_requires_an_active_profile(self):
        response = self.client.post(reverse("setup_item_dismiss"), {"item_key": "resource-radar"})
        self.assertRedirects(response, reverse("dashboard"))


class MoveRelocateViewTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_profile()
        self.source_folder = self.profile_root / "Year 1" / "Semester 1" / "BIO101"
        self.source_folder.mkdir(parents=True)
        self.file_path = self.source_folder / "notes.pdf"
        self.file_path.write_text("content")
        self.event = MoveEvent.objects.create(
            profile=self.profile,
            filename="notes.pdf",
            source_path="C:/Downloads/notes.pdf",
            destination_path=str(self.file_path),
            method="course_code",
            success=True,
        )

    def test_relocates_the_file_and_returns_ok(self):
        new_folder = self.profile_root / "Elsewhere"
        response = self.client.post(
            reverse("move_relocate", args=[self.event.pk]),
            {"new_destination": str(new_folder)},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue((new_folder / "notes.pdf").exists())
        self.assertFalse(self.file_path.exists())

    def test_rejects_a_blank_destination(self):
        response = self.client.post(reverse("move_relocate", args=[self.event.pk]), {"new_destination": ""})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_rejects_a_destination_outside_trusted_roots_unconfirmed(self):
        outside = self.profile_root.parent / "Somewhere Else Entirely"

        response = self.client.post(
            reverse("move_relocate", args=[self.event.pk]),
            {"new_destination": str(outside)},
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["needs_confirmation"])
        self.assertFalse(outside.exists())
        self.assertTrue(self.file_path.exists())

    def test_destination_outside_trusted_roots_succeeds_once_confirmed(self):
        outside = self.profile_root.parent / "Somewhere Else Entirely"

        response = self.client.post(
            reverse("move_relocate", args=[self.event.pk]),
            {"new_destination": str(outside), "confirm_external": "1"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue((outside / "notes.pdf").exists())


class MoveUndoViewTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_profile()
        self.original_folder = self.profile_root / "Downloads"
        self.original_folder.mkdir(parents=True)
        self.dest_folder = self.profile_root / "Year 1" / "Semester 1" / "BIO101"
        self.dest_folder.mkdir(parents=True)
        self.dest_path = self.dest_folder / "notes.pdf"
        self.dest_path.write_text("content")
        self.event = MoveEvent.objects.create(
            profile=self.profile,
            filename="notes.pdf",
            source_path=str(self.original_folder / "notes.pdf"),
            destination_path=str(self.dest_path),
            method="course_code",
            success=True,
        )

    def test_reverts_the_file_to_its_original_location(self):
        response = self.client.post(reverse("move_undo", args=[self.event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(self.dest_path.exists())
        self.assertTrue((self.original_folder / "notes.pdf").exists())

    def test_fails_cleanly_when_the_file_is_already_gone(self):
        self.dest_path.unlink()

        response = self.client.post(reverse("move_undo", args=[self.event.pk]))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["ok"])

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse("move_undo", args=[self.event.pk]))
        self.assertEqual(response.status_code, 405)


class FirstRunChecklistViewTests(SandboxedPathsTestCase):
    def test_loads_with_no_profile(self):
        response = self.client.get(reverse("first_run"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["checklist"][0]["done"])

    def test_loads_with_an_active_profile(self):
        # Regression test: the "Profile configured" and "Subjects added"
        # rows used to build a "profile_edit" URL with no pk, which
        # profile_edit requires -- this 500'd this entire page for every
        # install that had actually created a profile.
        self.make_profile()
        response = self.client.get(reverse("first_run"))
        self.assertEqual(response.status_code, 200)
        profile_row = next(c for c in response.context["checklist"] if c["id"] == "profile")
        self.assertTrue(profile_row["done"])
        self.assertIn("/edit/", profile_row["url"])

    def test_setup_complete_is_false_until_every_required_item_is_done(self):
        response = self.client.get(reverse("first_run"))
        self.assertFalse(response.context["setup_complete"])

    def test_setup_complete_shows_the_ready_banner(self):
        from organizer.models import AppSettings, CourseConfig

        profile = self.make_profile()
        CourseConfig.objects.create(profile=profile, groups=["CSC2100"])
        settings = AppSettings.get_solo()
        settings.downloads_path = str(self.profile_root)
        settings.save()

        response = self.client.get(reverse("first_run"))

        self.assertTrue(response.context["setup_complete"])
        self.assertContains(response, "Orch is ready")

    def test_open_watched_folder_requires_a_real_path(self):
        self.make_profile()
        response = self.client.post(reverse("open_watched_folder"))
        self.assertRedirects(response, reverse("first_run"))

    def test_open_watched_folder_opens_the_real_folder(self):
        from unittest import mock

        from organizer.models import AppSettings

        self.make_profile()
        settings = AppSettings.get_solo()
        settings.downloads_path = str(self.profile_root)
        settings.save()

        with mock.patch("os.startfile") as mocked_startfile:
            response = self.client.post(reverse("open_watched_folder"))

        mocked_startfile.assert_called_once_with(str(self.profile_root))
        self.assertRedirects(response, reverse("first_run"))


class PrivacyPolicyViewTests(SandboxedPathsTestCase):
    def test_loads_with_no_profile(self):
        response = self.client.get(reverse("privacy_policy"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MUELE")
        self.assertContains(response, "Google Drive")
        self.assertContains(response, "GitHub")

    def test_loads_with_an_active_profile(self):
        self.make_profile()
        response = self.client.get(reverse("privacy_policy"))
        self.assertEqual(response.status_code, 200)


class MueleConnectViewTests(SandboxedPathsTestCase):
    def test_shows_connected_state_instead_of_a_blank_login_form(self):
        # Regression test: this page used to show the raw login/token
        # forms first regardless of connection status, so an already
        # connected user saw no acknowledgment of that at all.
        profile = self.make_profile(setup_path="makerere")
        IntegrationConnection.objects.create(
            profile=profile,
            provider="muele",
            display_name="Makerere MUELE",
            username="student@mak.ac.ug",
            status="connected",
        )

        with mock.patch("organizer.core.muele_api.load_connection_token", return_value="fake-token"), \
             mock.patch("organizer.core.muele_api.load_profile_pending_token", return_value=None), \
             mock.patch("organizer.core.muele_api.verify_token", return_value=({
                 "fullname": "Student", "username": "student@mak.ac.ug",
             }, None)):
            response = self.client.get(reverse("muele_connect"))

        self.assertContains(response, "MUELE is connected")
        self.assertContains(response, "student@mak.ac.ug")
        # The login form is still present (for reconnecting), but tucked
        # behind a collapsed <details> rather than shown as the main flow.
        self.assertContains(response, "Reconnect MUELE")

    def test_shows_login_form_directly_when_not_yet_connected(self):
        self.make_profile(setup_path="makerere")

        # Hermetic "no pending token" -- without this, a real pending
        # token left in this machine's OS keyring from an earlier session
        # would make this test attempt a real, slow network verification.
        with mock.patch("organizer.core.muele_api.load_profile_pending_token", return_value=None):
            response = self.client.get(reverse("muele_connect"))

        self.assertNotContains(response, "MUELE is connected")
        self.assertContains(response, "Log in to MUELE")


class ProfilesListViewTests(SandboxedPathsTestCase):
    def test_empty_state(self):
        response = self.client.get(reverse("profiles_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No profiles yet")

    def test_lists_existing_profiles(self):
        self.make_profile(name="University")
        response = self.client.get(reverse("profiles_list"))
        self.assertContains(response, "University")


class ProfileWizardViewTests(SandboxedPathsTestCase):
    def test_get_renders_form(self):
        response = self.client.get(reverse("profile_wizard"))
        self.assertEqual(response.status_code, 200)

    def test_creates_an_active_profile_with_config_and_json_file(self):
        response = self.client.post(reverse("profile_wizard"), {
            "name": "University",
            "purpose": "school",
            "primary_label": "Year",
            "secondary_label": "Semester",
            "root_path": str(self.profile_root),
            "primary_value": "Year 2",
            "secondary_value": "Semester 1",
            "groups": "CSC2100, BSE2105",
        })

        self.assertRedirects(response, reverse("dashboard"))

        profile = Profile.objects.get(name="University")
        self.assertTrue(profile.is_active)
        self.assertEqual(profile.setup_path, "manual")
        self.assertEqual(profile.root_path, str(self.profile_root))

        config = CourseConfig.objects.get(profile=profile)
        self.assertEqual(config.groups, ["CSC2100", "BSE2105"])

        written = json.loads(paths.config_path(profile.root_path).read_text())
        self.assertEqual(written["groups"], ["CSC2100", "BSE2105"])

    def test_creating_a_second_profile_deactivates_the_first(self):
        first = self.make_profile(name="School")

        self.client.post(reverse("profile_wizard"), {
            "name": "Online",
            "purpose": "online",
            "primary_label": "Year",
            "secondary_label": "Course",
            "root_path": str(self.profile_root) + "-online",
            "primary_value": "2026",
            "secondary_value": "Python Bootcamp",
            "groups": "",
        })

        first.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(Profile.objects.get(name="Online").is_active)

    def test_missing_required_fields_reshows_the_form(self):
        response = self.client.post(reverse("profile_wizard"), {"name": "", "root_path": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Profile.objects.count(), 0)


class ProfileEditViewTests(SandboxedPathsTestCase):
    def test_updates_profile_and_config(self):
        profile = self.make_profile()

        response = self.client.post(reverse("profile_edit", args=[profile.pk]), {
            "name": "University (updated)",
            "primary_label": "Year",
            "secondary_label": "Semester",
            "root_path": str(self.profile_root),
            "primary_value": "Year 3",
            "secondary_value": "Semester 2",
            "groups": "CSC3100",
        })

        self.assertRedirects(response, reverse("profile_edit", args=[profile.pk]))

        profile.refresh_from_db()
        self.assertEqual(profile.name, "University (updated)")
        self.assertFalse(profile.ai_fallback_enabled)

        config = CourseConfig.objects.get(profile=profile)
        self.assertEqual(config.primary_value, "Year 3")
        self.assertEqual(config.groups, ["CSC3100"])

    def test_ai_fallback_checkbox_round_trips(self):
        profile = self.make_profile()

        self.client.post(reverse("profile_edit", args=[profile.pk]), {
            "name": profile.name,
            "primary_label": profile.primary_label,
            "secondary_label": profile.secondary_label,
            "root_path": profile.root_path,
            "primary_value": "Year 2",
            "secondary_value": "Semester 1",
            "groups": "",
            "ai_fallback_enabled": "on",
        })

        profile.refresh_from_db()
        self.assertTrue(profile.ai_fallback_enabled)


class ProfileActivateDeleteViewTests(SandboxedPathsTestCase):
    def test_activate_switches_active_profile(self):
        first = self.make_profile(name="School")
        second = self.make_profile(name="Online", root_path=str(self.profile_root) + "2", is_active=False)

        self.client.post(reverse("profile_activate", args=[second.pk]))

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)

    def test_delete_removes_the_profile(self):
        profile = self.make_profile()
        self.client.post(reverse("profile_delete", args=[profile.pk]))
        self.assertFalse(Profile.objects.filter(pk=profile.pk).exists())

    def test_get_does_not_delete(self):
        profile = self.make_profile()
        self.client.get(reverse("profile_delete", args=[profile.pk]))
        self.assertTrue(Profile.objects.filter(pk=profile.pk).exists())
