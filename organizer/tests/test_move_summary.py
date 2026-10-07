import json
from pathlib import Path
from unittest import mock

from django.urls import reverse

from organizer.core import paths
from organizer.models import FileSummary, MoveEvent

from .helpers import SandboxedPathsTestCase


def _write_pdf(path, text):
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path))
    c.drawString(72, 700, text)
    c.save()


class IsSummarizableTests(SandboxedPathsTestCase):
    def test_true_for_an_existing_pdf(self):
        target = self.profile_root / "notes.pdf"
        _write_pdf(target, "content")
        event = MoveEvent.objects.create(filename="notes.pdf", destination_path=str(target), method="course_code")
        self.assertTrue(event.is_summarizable())

    def test_false_when_file_no_longer_exists(self):
        event = MoveEvent.objects.create(
            filename="notes.pdf", destination_path=str(self.profile_root / "gone.pdf"), method="course_code"
        )
        self.assertFalse(event.is_summarizable())

    def test_false_for_unsupported_extension(self):
        target = self.profile_root / "photo.png"
        target.write_bytes(b"x")
        event = MoveEvent.objects.create(filename="photo.png", destination_path=str(target), method="media")
        self.assertFalse(event.is_summarizable())

    def test_false_when_destination_is_blank(self):
        event = MoveEvent.objects.create(filename="notes.pdf", destination_path="", method="course_code")
        self.assertFalse(event.is_summarizable())


class MoveSummarizeViewTests(SandboxedPathsTestCase):
    def setUp(self):
        super().setUp()
        self.ai_config_path = self.profile_root.parent / "ai_config.json"
        self.enterContext(mock.patch.object(paths, "AI_CONFIG_PATH", self.ai_config_path))

    def _write_ai_config(self):
        self.ai_config_path.write_text(json.dumps({
            "enabled": True,
            "api_key": "test-key",
            "model": "llama-3.1-8b-instant",
            "base_url": "https://api.groq.com/openai/v1",
        }))

    def test_get_is_not_allowed(self):
        event = MoveEvent.objects.create(filename="notes.pdf", destination_path="x", method="course_code")
        response = self.client.get(reverse("move_summarize", args=[event.pk]))
        self.assertEqual(response.status_code, 405)

    def test_no_ai_config_returns_a_clear_error(self):
        target = self.profile_root / "notes.pdf"
        _write_pdf(target, "content " * 40)
        event = MoveEvent.objects.create(filename="notes.pdf", destination_path=str(target), method="course_code")

        response = self.client.post(reverse("move_summarize", args=[event.pk]))

        self.assertEqual(response.status_code, 400)
        self.assertIn("Smart Orch isn't turned on", response.json()["error"])
        self.assertFalse(FileSummary.objects.exists())

    @mock.patch("organizer.core.summarize.requests.post")
    def test_success_creates_and_can_regenerate_the_summary(self, mock_post):
        self._write_ai_config()
        target = self.profile_root / "notes.pdf"
        _write_pdf(target, "Data Structures lecture content " * 20)
        event = MoveEvent.objects.create(filename="notes.pdf", destination_path=str(target), method="course_code")

        mock_post.return_value.json.return_value = {
            "choices": [{"message": {"content": "# Data Structures\n\nAn overview paragraph.\n"}}]
        }

        response = self.client.post(reverse("move_summarize", args=[event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(FileSummary.objects.count(), 1)

        # Regenerating overwrites rather than creating a second row.
        mock_post.return_value.json.return_value = {
            "choices": [{"message": {"content": "# Data Structures v2\n\nA new overview.\n"}}]
        }
        self.client.post(reverse("move_summarize", args=[event.pk]))

        self.assertEqual(FileSummary.objects.count(), 1)
        self.assertIn("v2", FileSummary.objects.get().content)


class MoveSummaryViewTests(SandboxedPathsTestCase):
    def test_404_json_when_no_summary_exists_yet(self):
        event = MoveEvent.objects.create(filename="notes.pdf", destination_path="x", method="course_code")
        response = self.client.get(reverse("move_summary_view", args=[event.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json())

    def test_renders_headings_and_paragraphs_as_escaped_html(self):
        event = MoveEvent.objects.create(filename="notes.pdf", destination_path="x", method="course_code")
        FileSummary.objects.create(move_event=event, content="# Title\n\nBody with <script>alert(1)</script>.\n")

        response = self.client.get(reverse("move_summary_view", args=[event.pk]))
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("<h1>Title</h1>", data["html"])
        self.assertIn("&lt;script&gt;", data["html"])
        self.assertNotIn("<script>", data["html"])


class MoveSummaryPdfViewTests(SandboxedPathsTestCase):
    def test_404_when_no_summary_exists_yet(self):
        event = MoveEvent.objects.create(filename="notes.pdf", destination_path="x", method="course_code")
        response = self.client.get(reverse("move_summary_pdf", args=[event.pk]))
        self.assertEqual(response.status_code, 404)

    def test_returns_a_pdf_with_a_sane_filename(self):
        event = MoveEvent.objects.create(filename="Weird/Name?.pdf", destination_path="x", method="course_code")
        FileSummary.objects.create(move_event=event, content="# Title\n\nBody.\n")

        response = self.client.get(reverse("move_summary_pdf", args=[event.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn("attachment", response["Content-Disposition"])


class MoveClearHistoryViewTests(SandboxedPathsTestCase):
    def test_get_is_not_allowed(self):
        response = self.client.get(reverse("move_clear_history"))
        self.assertEqual(response.status_code, 405)

    def test_without_an_active_profile_redirects_without_deleting(self):
        MoveEvent.objects.create(filename="notes.pdf", destination_path="x", method="course_code")

        response = self.client.post(reverse("move_clear_history"))

        self.assertRedirects(response, reverse("dashboard"))
        self.assertEqual(MoveEvent.objects.count(), 1)

    def test_deletes_only_the_active_profiles_events(self):
        profile = self.make_profile()
        other_root = Path(self._tmp.name) / "Other"
        other_root.mkdir()
        other_profile = self.make_profile(name="Other", root_path=str(other_root), is_active=False)

        MoveEvent.objects.create(profile=profile, filename="mine.pdf", destination_path="x", method="course_code")
        MoveEvent.objects.create(profile=other_profile, filename="theirs.pdf", destination_path="x", method="course_code")
        MoveEvent.objects.create(filename="orphaned.pdf", destination_path="x", method="course_code")

        response = self.client.post(reverse("move_clear_history"))

        self.assertRedirects(response, reverse("dashboard"))
        self.assertFalse(MoveEvent.objects.filter(profile=profile).exists())
        self.assertTrue(MoveEvent.objects.filter(profile=other_profile).exists())
        self.assertTrue(MoveEvent.objects.filter(profile__isnull=True).exists())

    def test_history_clear_returns_to_the_paginated_history(self):
        profile = self.make_profile()
        MoveEvent.objects.create(
            profile=profile, filename="old.pdf", destination_path="x", method="course_code"
        )

        response = self.client.post(
            reverse("move_clear_history"), {"return_to": "history"}
        )

        self.assertRedirects(response, reverse("move_history"))
        self.assertFalse(MoveEvent.objects.filter(profile=profile).exists())
