"""System tray icon and menu, built on pystray rather than PyQt6's
QSystemTrayIcon (see gui/main_window.py for why: WebView2 instead of a
Qt-bundled Chromium build). pystray's own event loop is designed to run
alongside another library's main loop via run_detached(), which is exactly
how this is used here: the tray starts detached, then gui/app.py's main()
calls webview.start() as the actual blocking call.
"""

import logging
import os
import threading
import time
import webbrowser

import pystray
from PIL import Image

from organizer.core import owner_access, paths, updater

from . import autostart
from .assets import ORCH_ICON_PATH
from .main_window import OrchMainWindow
from .server import dashboard_url
from .watcher_controller import WatcherController

_BUILD_DATE = "2026-07-19"
logger = logging.getLogger("orch.tray")

try:
    from organizer import __version__ as VERSION
except (ImportError, AttributeError):
    VERSION = "1.0.0"


def _ask_to_install_update(version):
    message = (
        f"Orch version {version} is ready to install.\n\n"
        "Choose Yes to download and install it now. Orch will restart "
        "automatically when the update is ready. Choose No to do this later."
    )
    if os.name == "nt":
        import ctypes

        # MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND
        return ctypes.windll.user32.MessageBoxW(
            None, message, "Orch update available", 0x00000004 | 0x00000020 | 0x00010000
        ) == 6

    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        return messagebox.askyesno("Orch update available", message, parent=root)
    finally:
        root.destroy()


class OrganizerTray:
    def __init__(self):
        self.watcher = WatcherController()
        # Created up front, hidden, rather than lazily on first open:
        # pywebview requires at least one window to exist before
        # webview.start() is called, so this can't wait for a background
        # "is the server ready yet" thread the way the old PyQt6 version's
        # first-open-creates-the-window pattern could.
        self.main_window = OrchMainWindow(watcher_controller=self.watcher)
        self._paused_until_timer = None
        self._update_announced = False
        self._update_download_error_shown = None

        image = Image.open(str(ORCH_ICON_PATH))
        self.icon = pystray.Icon("Orch", image, "Orch", menu=self._build_menu())

        self._notification_thread = threading.Thread(
            target=self._notification_loop, daemon=True, name="tray-notifications"
        )
        self._update_thread = threading.Thread(
            target=self._update_check_loop, daemon=True, name="tray-update-check"
        )

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: "Stop watching" if self.watcher.running else "Start watching",
                self._toggle_watcher,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Orch", self._open_main_window, default=True),
            pystray.MenuItem("Open web view", self._open_dashboard),
            pystray.MenuItem(
                "Owner tools", self._open_owner_console,
                visible=lambda item: owner_access.owner_mode_enabled(),
            ),
            pystray.MenuItem("Open log", self._open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Pause for 1 hour", lambda: self._pause_for(60), visible=lambda item: not self._is_paused()),
            pystray.MenuItem("Pause for 30 minutes", lambda: self._pause_for(30), visible=lambda item: not self._is_paused()),
            pystray.MenuItem("Pause for 15 minutes", lambda: self._pause_for(15), visible=lambda item: not self._is_paused()),
            pystray.MenuItem("Resume watching", self._resume_watcher, visible=lambda item: self._is_paused()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start with Windows", self._toggle_autostart,
                checked=lambda item: autostart.is_enabled(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda item: f"Update to v{self._latest_version()}",
                self._start_update,
                visible=lambda item: self._update_available(),
            ),
            pystray.MenuItem("Check for updates", self._check_for_updates_now),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("About Orch", self._show_about),
            pystray.MenuItem("Quit", self._quit),
        )

    def run(self):
        self.watcher.start()
        self.icon.run_detached()
        self._notification_thread.start()
        self._update_thread.start()
        self._check_for_updates_now()

    # ---------------------------------------------------------------- window

    def _open_main_window(self):
        self.main_window.show()

    def _open_dashboard(self):
        webbrowser.open(dashboard_url())

    def _open_owner_console(self):
        webbrowser.open(dashboard_url() + "owner/")

    def _open_log(self):
        if not paths.LOG_PATH.exists():
            self.icon.notify("No log file yet. Nothing has been sorted.", "Orch")
            return
        os.startfile(paths.LOG_PATH)

    # ------------------------------------------------------------- watcher

    def _toggle_watcher(self):
        if self.watcher.running:
            self.watcher.stop()
        else:
            self.watcher.start()
        self.icon.update_menu()

    def _is_paused(self):
        return self._paused_until_timer is not None

    def _pause_for(self, minutes):
        if not self.watcher.running:
            return
        self.watcher.stop()
        if self._paused_until_timer:
            self._paused_until_timer.cancel()
        timer = threading.Timer(minutes * 60, self._resume_watcher)
        timer.daemon = True
        timer.start()
        self._paused_until_timer = timer
        self.icon.update_menu()

    def _resume_watcher(self):
        if self._paused_until_timer:
            self._paused_until_timer.cancel()
            self._paused_until_timer = None
        if not self.watcher.running:
            self.watcher.start()
        self.icon.update_menu()

    # ------------------------------------------------------------ autostart

    def _toggle_autostart(self):
        autostart.set_enabled(not autostart.is_enabled())
        self.icon.update_menu()

    # ---------------------------------------------------------------- about

    def _show_about(self):
        import platform
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "About Orch",
            f"Orch\n\n"
            f"Version: {VERSION}\n"
            f"Build date: {_BUILD_DATE}\n"
            f"Python: {platform.python_version()}\n"
            f"Platform: {platform.platform()}\n\n"
            "A Windows app that keeps your files in the right folders,\n"
            "helps you review what changed, and keeps running quietly.\n\n"
            "Copyright (c) 2026 Iranzi. All rights reserved.",
        )
        root.destroy()

    # ------------------------------------------------------- notifications

    def _notification_loop(self):
        from organizer.core.notifications import has_pending, pop_pending

        while True:
            time.sleep(2)
            if has_pending():
                for note in pop_pending():
                    try:
                        self.icon.notify(note.get("message", ""), note.get("title", "Orch"))
                    except Exception:
                        logger.exception("Could not deliver a queued Orch desktop notification.")

    # -------------------------------------------------------------- update

    def _check_for_updates_now(self):
        threading.Thread(target=self._run_update_check, daemon=True).start()

    def _run_update_check(self):
        updater.remember_check_result(updater.check_for_update())

    def _update_available(self):
        result = updater.get_last_check()
        return bool(result and result.get("available"))

    def _latest_version(self):
        result = updater.get_last_check()
        return (result or {}).get("latest_version", "")

    def _update_check_loop(self):
        while True:
            time.sleep(4)
            result = updater.get_last_check()
            if not result:
                continue

            if result.get("available") and not self._update_announced:
                self._update_announced = True
                version = result.get("latest_version", "")
                self.icon.update_menu()
                threading.Thread(
                    target=self._prompt_for_update,
                    args=(version,),
                    daemon=True,
                    name="orch-update-prompt",
                ).start()

            error = result.get("error")
            if error and result.get("available") and error != self._update_download_error_shown:
                self._update_download_error_shown = error
                self.icon.update_menu()
                self.icon.notify(f"Couldn't finish updating: {error}", "Orch update failed")

    def _prompt_for_update(self, version):
        try:
            accepted = _ask_to_install_update(version)
        except Exception:
            logger.exception("Could not show the Orch update confirmation dialog.")
            self.icon.notify(
                f'Version {version} is ready. Choose "Update to v{version}" from the Orch tray menu.',
                "Orch update available",
            )
            return

        if accepted:
            self._start_update()

    def _start_update(self):
        result = updater.get_last_check()
        if not result or not result.get("available"):
            return
        version = result.get("latest_version", "")
        self.icon.notify(
            f"Downloading v{version} in the background. Orch will restart automatically when it's ready.",
            "Orch update",
        )
        threading.Thread(target=self._run_download_and_apply, args=(result,), daemon=True).start()

    def _run_download_and_apply(self, result):
        # On success, download_and_apply spawns the relaunch script and
        # exits this whole process -- execution never returns here. Only
        # the failure path needs to report anything back.
        success, error = updater.download_and_apply(result)
        if not success:
            updater.remember_check_result({**result, "error": error})

    # ----------------------------------------------------------------- quit

    def _quit(self):
        self.watcher.stop()
        self.icon.stop()
        os._exit(0)
