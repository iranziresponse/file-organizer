from django.urls import reverse
from django.utils import timezone

from organizer.core import study
from organizer.models import (
    CourseConfig,
    FolderImportPlan,
    FolderRule,
    IntegrationConnection,
    LearningDigest,
    MoveEvent,
    ResourceRecommendation,
    ReviewItem,
    StudyFocusSession,
    SubjectMemory,
    SubjectTheme,
)

from .helpers import SandboxedPathsTestCase


class StudyFoundationTests(SandboxedPathsTestCase):
    def test_subject_memory_and_review_queue_are_seeded_from_profile_activity(self):
        profile = self.make_profile()
        CourseConfig.objects.create(
            profile=profile,
            primary_value="Year 2",
            secondary_value="Semester 1",
            groups=["CSC2100", "BSE2105"],
        )
        event = MoveEvent.objects.create(
            profile=profile,
            filename="CSC2100 arrays.pdf",
            source_path="C:/Downloads/CSC2100 arrays.pdf",
            destination_path=str(self.profile_root / "CSC2100 arrays.pdf"),
            method="course_code",
            course_code="CSC2100",
            success=True,
        )

        foundation = study.ensure_learning_foundation(profile)

        self.assertEqual({m.code for m in foundation["memories"]}, {"BSE2105", "CSC2100"})
        self.assertTrue(foundation["goal"])
        self.assertEqual({r.subject_code for r in foundation["rules"]}, {"BSE2105", "CSC2100"})
        memory = SubjectMemory.objects.get(profile=profile, code="CSC2100")
        self.assertEqual(memory.resource_count, 1)
        self.assertTrue(SubjectTheme.objects.filter(profile=profile, subject_code="CSC2100").exists())

        review = ReviewItem.objects.get(profile=profile, move_event=event)
        self.assertEqual(review.subject_code, "CSC2100")
        self.assertEqual(review.status, "queued")

    def test_weekly_digest_records_metrics(self):
        profile = self.make_profile()
        MoveEvent.objects.create(
            profile=profile,
            filename="notes.pdf",
            method="course_code",
            course_code="CSC2100",
            success=True,
        )

        digest = study.create_weekly_digest(profile)

        self.assertEqual(digest.metrics["files_sorted"], 1)
        self.assertIn("files sorted", digest.content)

    def test_makerere_profiles_get_muele_connection_foundation(self):
        profile = self.make_profile(name="Computer Science - Makerere University")

        connection = study.ensure_makerere_connection(profile)

        self.assertIsNotNone(connection)
        self.assertEqual(connection.provider, "muele")
        self.assertEqual(connection.base_url, study.MUELE_BASE_URL)
        self.assertEqual(connection.status, "planned")

    def test_subject_memory_title_includes_the_real_course_name_when_known(self):
        profile = self.make_profile()
        CourseConfig.objects.create(
            profile=profile,
            primary_value="Year 2",
            secondary_value="Semester 1",
            groups=["CSC2100", "made-up-code"],
        )

        study.ensure_learning_foundation(profile)

        known = SubjectMemory.objects.get(profile=profile, code="CSC2100")
        self.assertEqual(known.title, "CSC2100 - Data Structures and Algorithms")
        unknown = SubjectMemory.objects.get(profile=profile, code="made-up-code")
        self.assertEqual(unknown.title, "made-up-code")

    def test_manual_profile_does_not_get_muele_connection(self):
        profile = self.make_profile(setup_path="manual")

        foundation = study.ensure_learning_foundation(profile)

        self.assertIsNone(foundation["muele_connection"])
        self.assertFalse(IntegrationConnection.objects.filter(profile=profile, provider="muele").exists())

    def test_folder_import_plan_scans_without_changing_files(self):
        root = self.profile_root / "Messy"
        (root / "BIO101").mkdir(parents=True)
        (root / "BIO101" / "cells.pdf").write_text("notes", encoding="utf-8")

        plan = study.create_import_plan(root, profile=self.make_profile())

        self.assertEqual(plan.status, "scanned")
        self.assertIn("BIO101", plan.proposed_subjects)
        self.assertTrue(FolderImportPlan.objects.filter(pk=plan.pk).exists())

    def test_rescanning_an_unchanged_folder_reports_files_as_already_indexed(self):
        root = self.profile_root / "Messy"
        (root / "BIO101").mkdir(parents=True)
        (root / "BIO101" / "cells.pdf").write_text("notes", encoding="utf-8")
        profile = self.make_profile()

        first = study.create_import_plan(root, profile=profile)
        self.assertEqual(first.notes, "")

        second = study.create_import_plan(root, profile=profile)

        self.assertIn("1 unchanged since a previous scan", second.notes)

    def test_rescanning_after_adding_a_file_reports_it_as_new(self):
        root = self.profile_root / "Messy"
        (root / "BIO101").mkdir(parents=True)
        (root / "BIO101" / "cells.pdf").write_text("notes", encoding="utf-8")
        profile = self.make_profile()

        study.create_import_plan(root, profile=profile)
        (root / "BIO101" / "genetics.pdf").write_text("more notes", encoding="utf-8")
        second = study.create_import_plan(root, profile=profile)

        self.assertIn("1 new or changed file(s), 1 unchanged", second.notes)

    def test_rule_builder_foundation_records_visual_rule(self):
        profile = self.make_profile()

        rule = study.create_rule_from_subject(profile, "BIO101", extensions=["pdf"])

        self.assertEqual(rule.match_field, "filename")
        self.assertEqual(rule.operator, "contains")
        self.assertEqual(rule.pattern, "BIO101")
        self.assertEqual(rule.file_extensions, ["pdf"])
        self.assertEqual(FolderRule.objects.get(pk=rule.pk).subject_code, "BIO101")


class StudyViewsTests(SandboxedPathsTestCase):
    def test_study_page_requires_active_profile_foundation(self):
        response = self.client.get(reverse("study_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No active profile")

    def test_study_page_renders_active_profile_foundation(self):
        profile = self.make_profile()
        CourseConfig.objects.create(
            profile=profile,
            primary_value="Year 2",
            secondary_value="Semester 1",
            groups=["CSC2100"],
        )

        response = self.client.get(reverse("study_home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Subject memory")
        self.assertContains(response, "Folder rules")
        self.assertContains(response, "Folder imports")
        self.assertContains(response, "CSC2100 - Data Structures and Algorithms")

    def test_study_page_renders_live_cockpit_surfaces(self):
        profile = self.make_profile()
        CourseConfig.objects.create(
            profile=profile,
            primary_value="Year 2",
            secondary_value="Semester 1",
            groups=["CSC2100"],
        )
        SubjectMemory.objects.create(profile=profile, code="CSC2100", weak_areas=["recursion"])
        ReviewItem.objects.create(profile=profile, subject_code="CSC2100", title="Review recursion", due_at=timezone.now())
        ResourceRecommendation.objects.create(
            profile=profile,
            subject_code="CSC2100",
            theme="recursion",
            source_type="youtube",
            title="Find strong video lessons for CSC2100: recursion",
            query="CSC2100 recursion lecture tutorial",
            url="https://www.youtube.com/results?search_query=CSC2100+recursion",
            reason="Based on weak area: recursion",
        )

        response = self.client.get(reverse("study_home"))

        self.assertContains(response, "Watcher paused")
        self.assertContains(response, "Today")
        self.assertContains(response, "Next best action")
        self.assertContains(response, "Live activity feed")
        self.assertContains(response, "App status")
        self.assertContains(response, "Downloads folder")
        self.assertContains(response, "Focus Mode")
        self.assertContains(response, "Why:")
        self.assertContains(response, "Search Orch")

    def test_focus_mode_post_creates_real_session(self):
        profile = self.make_profile()
        CourseConfig.objects.create(
            profile=profile,
            primary_value="Year 2",
            secondary_value="Semester 1",
            groups=["CSC2100"],
        )
        review = ReviewItem.objects.create(
            profile=profile,
            subject_code="CSC2100",
            title="Review arrays",
            due_at=timezone.now(),
        )
        resource = ResourceRecommendation.objects.create(
            profile=profile,
            subject_code="CSC2100",
            theme="arrays",
            source_type="book",
            title="Find books and study guides for CSC2100: arrays",
            query="CSC2100 arrays textbook study guide",
            url="https://openlibrary.org/search?q=CSC2100+arrays",
            reason="Based on recent files: arrays",
        )

        response = self.client.post(reverse("study_home"), {
            "action": "start_focus",
            "focus_subject": "CSC2100",
            "focus_minutes": "30",
            "focus_review_items": [str(review.pk)],
            "focus_resources": [str(resource.pk)],
            "focus_weak_areas": ["CSC2100: arrays"],
            "focus_notes": "Work through examples.",
        })

        self.assertRedirects(response, reverse("study_home"))
        session = StudyFocusSession.objects.get(profile=profile)
        self.assertEqual(session.subject_code, "CSC2100")
        self.assertEqual(session.target_minutes, 30)
        self.assertEqual(session.review_item_ids, [review.pk])
        self.assertEqual(session.resource_ids, [resource.pk])
        self.assertEqual(session.weak_areas, ["CSC2100: arrays"])
        self.assertTrue(session.notes)

    def test_create_digest_from_study_page(self):
        profile = self.make_profile()
        MoveEvent.objects.create(profile=profile, filename="notes.pdf", method="media", success=True)

        response = self.client.post(reverse("study_home"), {"action": "create_digest"})

        self.assertRedirects(response, reverse("study_home"))
        self.assertEqual(LearningDigest.objects.filter(profile=profile).count(), 1)

    def test_makerere_wizard_creates_muele_connection(self):
        response = self.client.post(reverse("makerere_wizard"), {
            "college": "College of Computing and Information Sciences",
            "school": "School of Computing and Informatics Technology",
            "program": "Bachelor of Science in Computer Science",
            "year_value": "2",
            "semester_value": "1",
            "root_path": str(self.profile_root),
            "groups": "CSC2100",
        })

        self.assertRedirects(response, reverse("dashboard"))
        connection = IntegrationConnection.objects.get()
        self.assertEqual(connection.provider, "muele")
        self.assertEqual(connection.base_url, study.MUELE_BASE_URL)

    def test_muele_connection_page_saves_metadata_without_secret(self):
        profile = self.make_profile(name="Computer Science - Makerere University")
        study.ensure_makerere_connection(profile)

        response = self.client.post(reverse("muele_connection"), {
            "base_url": study.MUELE_BASE_URL,
            "username": "student@mak.ac.ug",
            "college": "COCIS",
            "sync_targets": ["course_files", "assignments"],
        })

        self.assertRedirects(response, reverse("study_home"))
        connection = IntegrationConnection.objects.get(profile=profile, provider="muele")
        self.assertEqual(connection.username, "student@mak.ac.ug")
        self.assertEqual(connection.token_reference, "")
        self.assertEqual(connection.config["college"], "COCIS")
