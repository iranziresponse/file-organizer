import re
from pathlib import Path

from django.test import SimpleTestCase
from django.urls import reverse

from organizer.models import SubjectMemory

from .helpers import SandboxedPathsTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PROJECT_ROOT / "organizer" / "templates"


def _template_files():
    return sorted(TEMPLATE_ROOT.rglob("*.html"))


class UserFacingCopyContractTests(SimpleTestCase):
    emoji_pattern = re.compile(
        "["
        "\U0001F1E6-\U0001F1FF"
        "\U0001F300-\U0001F5FF"
        "\U0001F600-\U0001F64F"
        "\U0001F680-\U0001F6FF"
        "\U0001F700-\U0001FAFF"
        "\u2600-\u27BF"
        "]"
    )
    long_dash_pattern = re.compile("[\u2013\u2014]")
    # A bare double-hyphen used as an em-dash substitute in prose (whitespace
    # on both sides). Deliberately does NOT match CSS custom-property syntax
    # (--name: ... or var(--name)), which has no whitespace before the dashes.
    double_hyphen_pattern = re.compile(r"\s--\s")
    mojibake_pattern = re.compile("[\u00e2\u00f0]|\u00ef\u00b8")

    def test_templates_do_not_use_emoji_or_symbol_badges(self):
        for path in _template_files():
            with self.subTest(template=str(path.relative_to(PROJECT_ROOT))):
                content = path.read_text(encoding="utf-8")
                self.assertIsNone(self.emoji_pattern.search(content))

    def test_templates_do_not_use_long_dash_characters(self):
        for path in _template_files():
            with self.subTest(template=str(path.relative_to(PROJECT_ROOT))):
                content = path.read_text(encoding="utf-8")
                self.assertIsNone(self.long_dash_pattern.search(content))

    def test_templates_do_not_use_double_hyphen_as_a_dash(self):
        for path in _template_files():
            with self.subTest(template=str(path.relative_to(PROJECT_ROOT))):
                content = path.read_text(encoding="utf-8")
                self.assertIsNone(self.double_hyphen_pattern.search(content))

    def test_templates_do_not_contain_mojibake(self):
        for path in _template_files():
            with self.subTest(template=str(path.relative_to(PROJECT_ROOT))):
                content = path.read_text(encoding="utf-8")
                self.assertIsNone(self.mojibake_pattern.search(content))

    def test_user_templates_do_not_link_to_admin(self):
        for path in sorted((TEMPLATE_ROOT / "organizer").rglob("*.html")):
            with self.subTest(template=str(path.relative_to(PROJECT_ROOT))):
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("/admin/", content)
                self.assertNotIn("admin:index", content)

    def test_resource_copy_does_not_claim_unverified_best_rankings(self):
        resource_template = TEMPLATE_ROOT / "organizer" / "resource_radar.html"
        content = resource_template.read_text(encoding="utf-8").lower()

        self.assertNotIn("best youtube", content)
        self.assertNotIn("best book", content)
        self.assertNotIn("top ranked", content)
        self.assertIn("discovery", content)
        self.assertIn("without inventing", content)


class ThemeAndControlContractTests(SimpleTestCase):
    def test_dark_mode_is_default_without_overriding_saved_preferences(self):
        base_template = (TEMPLATE_ROOT / "organizer" / "base.html").read_text(encoding="utf-8")
        theme_script = (
            PROJECT_ROOT / "organizer" / "static" / "organizer" / "js" / "topbar-behavior.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "saved === 'light' || saved === 'dark' ? saved : 'dark'",
            base_template,
        )
        self.assertIn(
            "apply(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');",
            theme_script,
        )

    def test_ghost_buttons_keep_visible_hover_feedback_in_both_themes(self):
        stylesheet = (
            PROJECT_ROOT / "organizer" / "static" / "organizer" / "css" / "orch.css"
        ).read_text(encoding="utf-8")

        self.assertIn(".app-shell .button-ghost:hover", stylesheet)
        self.assertIn("background: var(--green-soft) !important;", stylesheet)
        self.assertIn("color: var(--orch-ink) !important;", stylesheet)
        self.assertIn("--orch-link-hover: #23683a;", stylesheet)
        self.assertIn("color: var(--orch-link-hover);", stylesheet)


class StudyNavigationContractTests(SandboxedPathsTestCase):
    def test_study_page_links_to_resource_radar_and_learning_routes(self):
        self.make_profile()

        response = self.client.get(reverse("study_home"))

        self.assertContains(response, reverse("resource_radar"))
        self.assertContains(response, reverse("learning_routes"))

    def test_resource_radar_uses_real_external_discovery_links(self):
        profile = self.make_profile()
        SubjectMemory.objects.create(profile=profile, code="BIO101", weak_areas=["cells"])
        self.client.post(reverse("resource_radar"), {"action": "generate"})

        response = self.client.get(reverse("resource_radar"))

        self.assertContains(response, "youtube.com/results")
        self.assertContains(response, "openlibrary.org/search")
        self.assertContains(response, 'rel="noopener"')
