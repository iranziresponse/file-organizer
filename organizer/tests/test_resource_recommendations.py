from unittest import mock

from django.urls import reverse
from django.db import IntegrityError, transaction

from organizer.core import resources
from organizer.models import (
    CourseConfig,
    MoveEvent,
    Notification,
    ResourceRecommendation,
    SubjectMemory,
    SubjectTheme,
)

from .helpers import SandboxedPathsTestCase


class ResourceRecommendationEngineTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_profile()
        CourseConfig.objects.create(
            profile=self.profile,
            primary_value="Year 2",
            secondary_value="Semester 1",
            groups=["CSC2100"],
        )
        self.memory = SubjectMemory.objects.create(
            profile=self.profile,
            code="CSC2100",
            title="Data Structures",
            current_focus=["trees"],
            weak_areas=["recursion"],
            resource_count=4,
        )
        SubjectTheme.objects.create(
            profile=self.profile,
            subject_memory=self.memory,
            subject_code="CSC2100",
            name="binary search trees",
            weight=12,
            source="filename",
        )
        # GitHub's search API is actually reachable from this environment
        # (unlike MUELE/YouTube), so leaving it unmocked would make every
        # test in this class a slow, flaky real network call that burns
        # into the anonymous rate limit. Default to no results; tests that
        # care about GitHub behavior override this within their own body.
        self.github_search_patcher = mock.patch("organizer.core.resources.github_api.search_repos", return_value=[])
        self.github_search_patcher.start()
        self.addCleanup(self.github_search_patcher.stop)

    def test_builds_youtube_book_and_github_discovery_links_from_subject_signal(self):
        candidates = resources.build_candidates(self.profile, limit=8)

        self.assertTrue(any(item.source_type == "youtube" for item in candidates))
        self.assertTrue(any(item.source_type == "book" for item in candidates))
        self.assertTrue(any(item.source_type == "github_repo" for item in candidates))
        self.assertTrue(any("recursion" in item.query.lower() for item in candidates))
        self.assertTrue(all(
            "youtube.com/results" in item.url or "openlibrary.org/search" in item.url or "github.com/search" in item.url
            for item in candidates
        ))
        self.assertTrue(all("Based on" in item.reason for item in candidates))

    def test_uses_a_real_repo_result_when_github_search_returns_one(self):
        with mock.patch("organizer.core.resources.github_api.search_repos") as search:
            search.return_value = [{
                "full_name": "someone/bst-visualizer",
                "description": "A binary search tree visualizer.",
                "url": "https://github.com/someone/bst-visualizer",
                "stars": 120,
                "language": "Python",
            }]

            candidates = resources.build_candidates(self.profile, limit=8)

        repo_candidates = [c for c in candidates if c.source_type == "github_repo"]
        self.assertTrue(any(c.title == "someone/bst-visualizer" for c in repo_candidates))
        self.assertTrue(any(c.url == "https://github.com/someone/bst-visualizer" for c in repo_candidates))
        self.assertTrue(any("120 stars" in c.reason for c in repo_candidates))

    def test_falls_back_to_a_search_link_when_github_returns_nothing(self):
        candidates = resources.build_candidates(self.profile, limit=8)

        repo_candidates = [c for c in candidates if c.source_type == "github_repo"]
        self.assertTrue(repo_candidates)
        self.assertTrue(all("github.com/search" in c.url for c in repo_candidates))

    def test_subject_fallback_searches_the_resolved_course_name_not_the_raw_code_label(self):
        # A subject with no theme/weak-area/recent-file signal falls back to
        # its own title as the "topic" (see _subject_topics' "subject"
        # branch) -- e.g. "CSC2100 - Data Structures and Algorithms". A
        # course code prefix isn't real search text; querying GitHub with it
        # returned zero results against the live API during manual testing.
        # Replace the CSC2100 memory that has weak/theme signal, isolating
        # this subject to only the fallback path.
        self.memory.delete()
        SubjectMemory.objects.create(profile=self.profile, code="CSC2100", title="CSC2100 - Data Structures and Algorithms")

        with mock.patch("organizer.core.resources.github_api.search_repos", return_value=[]) as search:
            resources.build_candidates(self.profile, subject_code="CSC2100", limit=8)

        query = search.call_args.args[0]
        self.assertNotIn("CSC2100", query)
        self.assertEqual(query, "Data Structures and Algorithms")

    def test_github_searches_are_capped_per_sync(self):
        codes = [f"COD{i}" for i in range(10)]
        for code in codes:
            SubjectMemory.objects.create(profile=self.profile, code=code, weak_areas=[f"topic {code}"])

        with mock.patch("organizer.core.resources.github_api.search_repos", return_value=[]) as search:
            resources.build_candidates(self.profile, limit=200)

        self.assertLessEqual(search.call_count, resources._MAX_GITHUB_SEARCHES_PER_SYNC)

    def test_uses_a_real_video_result_when_youtube_search_returns_one(self):
        with mock.patch("organizer.core.resources.youtube_api.search_videos") as search:
            search.return_value = [{
                "title": "Binary Search Trees Explained",
                "channel": "CS Dojo",
                "video_id": "abc123",
                "url": "https://www.youtube.com/watch?v=abc123",
                "thumbnail_url": "https://i.ytimg.com/vi/abc123/mqdefault.jpg",
            }]

            candidates = resources.build_candidates(self.profile, limit=8)

        youtube_candidates = [c for c in candidates if c.source_type == "youtube"]
        self.assertTrue(any(c.title == "Binary Search Trees Explained" for c in youtube_candidates))
        self.assertTrue(any(c.url == "https://www.youtube.com/watch?v=abc123" for c in youtube_candidates))
        self.assertTrue(any("CS Dojo" in c.reason for c in youtube_candidates))

    def test_falls_back_to_a_search_link_when_youtube_returns_nothing(self):
        with mock.patch("organizer.core.resources.youtube_api.search_videos", return_value=[]):
            candidates = resources.build_candidates(self.profile, limit=8)

        youtube_candidates = [c for c in candidates if c.source_type == "youtube"]
        self.assertTrue(all("youtube.com/results" in c.url for c in youtube_candidates))

    def test_multiple_real_videos_for_the_same_topic_do_not_collapse_into_one_row(self):
        with mock.patch("organizer.core.resources.youtube_api.search_videos") as search:
            search.return_value = [
                {"title": "Video A", "channel": "Chan A", "video_id": "aaa",
                 "url": "https://www.youtube.com/watch?v=aaa", "thumbnail_url": ""},
                {"title": "Video B", "channel": "Chan B", "video_id": "bbb",
                 "url": "https://www.youtube.com/watch?v=bbb", "thumbnail_url": ""},
            ]
            resources.sync_recommendations(self.profile, limit=8)
            resources.sync_recommendations(self.profile, limit=8)

        titles = set(
            ResourceRecommendation.objects.filter(profile=self.profile, source_type="youtube")
            .values_list("title", flat=True)
        )
        self.assertIn("Video A", titles)
        self.assertIn("Video B", titles)
        self.assertEqual(
            Notification.objects.filter(
                profile=self.profile, title="New study recommendations"
            ).count(),
            1,
        )

    def test_weak_areas_rank_above_general_themes(self):
        candidates = resources.build_candidates(self.profile, limit=4)

        self.assertIn("recursion", candidates[0].theme.lower())
        self.assertGreaterEqual(candidates[0].score, candidates[-1].score)

    def test_subject_filter_limits_recommendations_to_one_subject(self):
        SubjectMemory.objects.create(
            profile=self.profile,
            code="MTH100",
            weak_areas=["limits"],
            resource_count=1,
        )

        candidates = resources.build_candidates(self.profile, subject_code="MTH100", limit=8)

        self.assertTrue(candidates)
        self.assertEqual({item.subject_code for item in candidates}, {"MTH100"})

    def test_recent_files_create_signal_when_memory_has_no_themes(self):
        empty_memory = SubjectMemory.objects.create(profile=self.profile, code="PHY101")
        MoveEvent.objects.create(
            profile=self.profile,
            filename="PHY101 circular motion notes.pdf",
            source_path="C:/Downloads/PHY101 circular motion notes.pdf",
            destination_path=str(self.profile_root / "PHY101 circular motion notes.pdf"),
            method="course_code",
            course_code="PHY101",
            success=True,
        )

        candidates = resources.build_candidates(self.profile, subject_code=empty_memory.code, limit=4)

        self.assertTrue(any("circular motion" in item.query.lower() for item in candidates))

    def test_sync_persists_without_duplicating_recommendations(self):
        first = resources.sync_recommendations(self.profile, limit=8)
        second = resources.sync_recommendations(self.profile, limit=8)

        self.assertEqual(len(first), len(second))
        self.assertEqual(ResourceRecommendation.objects.filter(profile=self.profile).count(), len(first))

    def test_status_changes_are_validated(self):
        item = resources.sync_recommendations(self.profile, limit=1)[0]

        resources.set_recommendation_status(item, "saved")
        item.refresh_from_db()

        self.assertEqual(item.status, "saved")
        with self.assertRaises(ValueError):
            resources.set_recommendation_status(item, "unknown")

    def test_dismissed_recommendations_are_excluded_from_default_query(self):
        item = resources.sync_recommendations(self.profile, limit=1)[0]
        resources.set_recommendation_status(item, "dismissed")

        visible = list(resources.recommendations_for_profile(self.profile))

        self.assertEqual(visible, [])


class ResourceRecommendationDatabaseTests(SandboxedPathsTestCase):
    def test_unique_query_per_profile_subject_and_source(self):
        profile = self.make_profile()
        payload = {
            "profile": profile,
            "subject_code": "CSC2100",
            "theme": "trees",
            "source_type": "youtube",
            "title": "Find strong video lessons for CSC2100: trees",
            "query": "CSC2100 trees lecture tutorial",
            "url": "https://www.youtube.com/results?search_query=CSC2100+trees",
        }
        ResourceRecommendation.objects.create(**payload)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ResourceRecommendation.objects.create(**payload)

    def test_recommendations_cascade_when_profile_is_deleted(self):
        profile = self.make_profile()
        ResourceRecommendation.objects.create(
            profile=profile,
            subject_code="CSC2100",
            theme="trees",
            source_type="book",
            title="Find books and study guides for CSC2100: trees",
            query="CSC2100 trees textbook",
            url="https://openlibrary.org/search?q=CSC2100+trees",
        )

        profile.delete()

        self.assertEqual(ResourceRecommendation.objects.count(), 0)


class ResourceRadarViewTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.make_profile()
        SubjectMemory.objects.create(
            profile=self.profile,
            code="BIO101",
            weak_areas=["cell division"],
            resource_count=2,
        )
        github_search_patcher = mock.patch("organizer.core.resources.github_api.search_repos", return_value=[])
        github_search_patcher.start()
        self.addCleanup(github_search_patcher.stop)

    def test_page_renders_empty_state_before_generation(self):
        response = self.client.get(reverse("resource_radar"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Resource Radar")
        self.assertContains(response, "No recommendations yet")

    def test_generate_creates_recommendations(self):
        response = self.client.post(reverse("resource_radar"), {"action": "generate"})

        self.assertRedirects(response, reverse("resource_radar"))
        self.assertTrue(ResourceRecommendation.objects.filter(profile=self.profile).exists())

    def test_generate_for_one_subject_keeps_filter_in_redirect(self):
        SubjectMemory.objects.create(
            profile=self.profile,
            code="CHEM101",
            weak_areas=["bonding"],
            resource_count=1,
        )

        response = self.client.post(reverse("resource_radar"), {
            "action": "generate",
            "subject_code": "CHEM101",
        })

        self.assertRedirects(response, f"{reverse('resource_radar')}?subject=CHEM101", fetch_redirect_response=False)
        self.assertEqual(
            set(ResourceRecommendation.objects.values_list("subject_code", flat=True)),
            {"CHEM101"},
        )

    def test_user_can_save_and_dismiss_recommendation(self):
        item = resources.sync_recommendations(self.profile, limit=1)[0]

        response = self.client.post(
            reverse("resource_radar"),
            {"action": "saved", "recommendation_pk": item.pk},
        )
        self.assertRedirects(response, reverse("resource_radar"))
        item.refresh_from_db()
        self.assertEqual(item.status, "saved")

        response = self.client.post(
            reverse("resource_radar"),
            {"action": "dismissed", "recommendation_pk": item.pk},
        )
        self.assertRedirects(response, reverse("resource_radar"))
        item.refresh_from_db()
        self.assertEqual(item.status, "dismissed")

    def test_dismiss_all_suggested_clears_only_suggested_items(self):
        items = resources.sync_recommendations(self.profile, limit=3)
        saved_item = items[0]
        saved_item.status = "saved"
        saved_item.save(update_fields=["status"])

        response = self.client.post(reverse("resource_radar"), {"action": "dismiss_all_suggested"})

        self.assertRedirects(response, reverse("resource_radar"))
        saved_item.refresh_from_db()
        self.assertEqual(saved_item.status, "saved")
        self.assertEqual(
            ResourceRecommendation.objects.filter(profile=self.profile, status="suggested").count(), 0,
        )
        self.assertEqual(
            ResourceRecommendation.objects.filter(profile=self.profile, status="dismissed").count(), 2,
        )

    def test_resource_radar_requires_active_profile(self):
        self.profile.is_active = False
        self.profile.save()

        response = self.client.get(reverse("resource_radar"))

        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_recommendation_renders_with_stable_hooks_for_the_ajax_layer(self):
        item = resources.sync_recommendations(self.profile, limit=1)[0]

        response = self.client.get(reverse("resource_radar"))
        content = response.content.decode()

        # Hooks resource-radar-actions.js depends on to find a row, submit
        # its action, and patch in the server's own response.
        self.assertIn('data-pk="%d"' % item.pk, content)
        self.assertIn('id="rec-actions-%d"' % item.pk, content)
        self.assertIn('data-radar-action', content)
        self.assertIn('id="saved-count"', content)
        self.assertIn('id="visible-count"', content)

    def test_dismiss_response_no_longer_lists_the_item(self):
        # This is exactly what resource-radar-actions.js checks to decide
        # whether the dismiss actually succeeded server-side.
        item = resources.sync_recommendations(self.profile, limit=1)[0]

        response = self.client.post(
            reverse("resource_radar"),
            {"action": "dismissed", "recommendation_pk": item.pk},
            follow=True,
        )

        self.assertNotIn('data-pk="%d"' % item.pk, response.content.decode())

    def test_save_response_still_lists_the_item_with_updated_action(self):
        item = resources.sync_recommendations(self.profile, limit=1)[0]

        response = self.client.post(
            reverse("resource_radar"),
            {"action": "saved", "recommendation_pk": item.pk},
            follow=True,
        )
        content = response.content.decode()

        self.assertIn('data-pk="%d"' % item.pk, content)
        self.assertIn("Unsave", content)
