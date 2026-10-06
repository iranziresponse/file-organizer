from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from gui.main_window import OrchMainWindow


class DesktopStartupTests(SimpleTestCase):
    def make_window(self):
        window = OrchMainWindow.__new__(OrchMainWindow)
        window.window = Mock()
        window._show_after_load = False
        window._clamp_to_screen = Mock()
        window.schedule_repaint_nudge = Mock()
        return window

    @patch("gui.main_window.dashboard_url", return_value="http://127.0.0.1:8765/")
    def test_initial_window_stays_hidden_until_dashboard_loads(self, _dashboard_url):
        window = self.make_window()

        window.open_path("desktop-shell/", show_after_load=True)
        window.window.load_url.assert_called_once_with(
            "http://127.0.0.1:8765/desktop-shell/"
        )

        window.show()
        window.window.show.assert_not_called()

        window._on_loaded()

        window.window.show.assert_called_once_with()
        self.assertEqual(window.window.title, "Orch")
        window._clamp_to_screen.assert_called_once_with()
        self.assertFalse(window._show_after_load)

    @patch("gui.main_window.dashboard_url", return_value="http://127.0.0.1:8765/")
    def test_failed_initial_navigation_clears_deferred_show(self, _dashboard_url):
        window = self.make_window()
        window.window.load_url.side_effect = RuntimeError("navigation failed")

        with self.assertRaisesRegex(RuntimeError, "navigation failed"):
            window.open_path("desktop-shell/", show_after_load=True)

        self.assertFalse(window._show_after_load)
