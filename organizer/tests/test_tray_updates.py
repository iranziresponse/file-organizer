from unittest import TestCase
from unittest.mock import Mock, patch

from gui.tray import OrganizerTray


class UpdatePromptTests(TestCase):
    def make_tray(self):
        tray = OrganizerTray.__new__(OrganizerTray)
        tray.icon = Mock()
        tray._start_update = Mock()
        return tray

    @patch("gui.tray._ask_to_install_update", return_value=True)
    def test_accepting_update_starts_installation(self, _ask):
        tray = self.make_tray()

        tray._prompt_for_update("2.0.0")

        tray._start_update.assert_called_once_with()
        tray.icon.notify.assert_not_called()

    @patch("gui.tray._ask_to_install_update", return_value=False)
    def test_declining_update_leaves_it_available_without_installing(self, _ask):
        tray = self.make_tray()

        tray._prompt_for_update("2.0.0")

        tray._start_update.assert_not_called()
        tray.icon.notify.assert_not_called()

    @patch("gui.tray._ask_to_install_update", side_effect=RuntimeError("dialog unavailable"))
    def test_dialog_failure_reports_tray_menu_fallback(self, _ask):
        tray = self.make_tray()

        with self.assertLogs("orch.tray", level="ERROR"):
            tray._prompt_for_update("2.0.0")

        tray._start_update.assert_not_called()
        tray.icon.notify.assert_called_once()
        self.assertIn("Update to v2.0.0", tray.icon.notify.call_args.args[0])
