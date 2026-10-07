import os
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import runtime


class PackagedRuntimeDataIsolationTests(TestCase):
    def test_frozen_app_data_is_separate_for_each_windows_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            user_a = Path(temp_dir) / "user-a"
            user_b = Path(temp_dir) / "user-b"

            with patch.object(runtime.sys, "frozen", True, create=True):
                with patch.dict(os.environ, {"LOCALAPPDATA": str(user_a)}):
                    app_a = runtime.app_dir()

                with patch.dict(os.environ, {"LOCALAPPDATA": str(user_b)}):
                    app_b = runtime.app_dir()

        self.assertEqual(app_a, user_a / "Orch")
        self.assertEqual(app_b, user_b / "Orch")
        self.assertNotEqual(app_a, app_b)
