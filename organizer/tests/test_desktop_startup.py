import socket
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from gui import app
from gui.main_window import OrchMainWindow, STARTUP_HTML
from gui.server import _select_dashboard_port


class DesktopStartupTests(SimpleTestCase):
    def make_window(self):
        window = OrchMainWindow.__new__(OrchMainWindow)
        window.window = Mock()
        window._startup_splash_pending = True
        window._clamp_to_screen = Mock()
        window.schedule_repaint_nudge = Mock()
        return window

    @patch("gui.main_window.dashboard_url", return_value="http://127.0.0.1:8765/")
    def test_splash_load_navigates_to_dashboard_then_nudges_paint(self, _dashboard_url):
        window = self.make_window()

        window._on_loaded()

        window.window.load_url.assert_called_once_with(
            "http://127.0.0.1:8765/desktop-shell/"
        )
        self.assertEqual(window.window.title, "Orch")
        self.assertFalse(window._startup_splash_pending)
        window.schedule_repaint_nudge.assert_not_called()

        window._on_loaded()

        window.schedule_repaint_nudge.assert_called_once_with()

    @patch("gui.main_window.webview.create_window")
    @patch(
        "gui.main_window.OrchMainWindow._sized_and_centered_for_screen",
        return_value=(1280, 820, 0, 0),
    )
    def test_native_window_starts_with_visible_splash(self, _sized, create_window):
        OrchMainWindow()

        options = create_window.call_args.kwargs
        self.assertEqual(options["html"], STARTUP_HTML)
        self.assertNotIn("hidden", options)

    def test_open_path_navigates_to_dashboard_path(self):
        window = self.make_window()

        window.open_path("study/")

        window.window.load_url.assert_called_once_with(
            "http://127.0.0.1:8765/study/"
        )

    def test_port_in_use_selects_an_available_local_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            preferred_port = occupied.getsockname()[1]

            selected_port = _select_dashboard_port("127.0.0.1", preferred_port)

        self.assertNotEqual(selected_port, preferred_port)
        self.assertGreater(selected_port, 0)

    def test_http_error_confirms_the_server_is_reachable(self):
        error = app.urllib.error.HTTPError(
            "http://127.0.0.1:8765/", 500, "Internal Server Error", {}, None
        )
        with patch.object(error, "close") as close, patch(
            "gui.app.urllib.request.urlopen", side_effect=error
        ):
            self.assertTrue(app._wait_for_server("http://127.0.0.1:8765/"))

        close.assert_called_once()

    @patch("gui.app.urllib.request.urlopen")
    def test_readiness_check_closes_successful_response(self, urlopen):
        response = urlopen.return_value

        self.assertTrue(app._wait_for_server("http://127.0.0.1:8765/"))

        response.close.assert_called_once()

    @patch("gui.app.sys.platform", "linux")
    def test_startup_failure_is_saved_to_a_user_log(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             patch.dict("gui.app.os.environ", {"LOCALAPPDATA": temp_dir}):
            try:
                raise RuntimeError("dashboard failed")
            except RuntimeError as exc:
                app._report_startup_failure(exc)

            log_path = Path(temp_dir) / "Orch" / "logs" / "startup.log"
            self.assertTrue(log_path.exists())
            self.assertIn("RuntimeError: dashboard failed", log_path.read_text(encoding="utf-8"))
