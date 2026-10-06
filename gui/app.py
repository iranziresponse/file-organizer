"""Desktop entry point. Sets up Django (ORM + migrations) before touching
the GUI, then runs the dashboard server and the file watcher as background
threads under one system tray icon -- no console window, no separate
`manage.py runserver`/`manage.py migrate` steps for the packaged exe.
"""

import os
import sys
import threading
import time
import urllib.error
import urllib.request


def _silence_none_streams():
    # PyInstaller's --windowed build gives frozen apps sys.stdout/stderr of
    # None. Django's runserver command writes startup banners to self.stdout,
    # which crashes on None -- redirect to a null sink instead of patching
    # every call site.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")


_single_instance_mutex = None


def _focus_existing_instance():
    """Bring the ready Orch window to the front. The main window is titled
    "Orch" only after its first dashboard page loads, so a second launch
    cannot reveal its blank startup surface."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "Orch")
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _dashboard_already_serving():
    """True if something is already answering on the dashboard port -- an
    Orch instance that got fully up. A cheap backstop for the mutex check
    below, since a missed detection there means a second process builds its
    own tray, window and server, the server fails to bind this port and
    os._exit(1)s from a daemon thread mid-startup, and the half-built
    window is left behind black -- exactly the "second window, closing it
    kills the app" report."""
    import socket

    # Literals rather than importing gui.server, which pulls in Django
    # modules at import time and this runs before django.setup(). Keep in
    # sync with gui/server.py's DASHBOARD_HOST / DASHBOARD_PORT.
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.4):
            return True
    except OSError:
        return False


def _enforce_single_instance():
    # Without this, launching Orch a second time (e.g. double-clicking the
    # desktop/Start-menu shortcut while it's already running in the tray --
    # closing the window hides it rather than quitting, see
    # OrchMainWindow._on_closing, so this is the normal state most of the
    # time) starts a whole second process: its own tray icon, its own
    # hidden window, and its own dashboard server thread that fails to
    # bind the already-used port -- exactly the second, blank dark window
    # users were seeing. A named mutex lets a second launch detect the
    # first instance, bring its window to the front, and exit immediately,
    # before Django, the tray, or any window gets created.
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    global _single_instance_mutex

    # use_last_error=True is required here. With the bare ctypes.windll
    # proxy, the thread's last-error value can be overwritten between the
    # CreateMutexW call and a follow-up kernel32.GetLastError() (ctypes'
    # own marshalling / restype handling / allocation in between), so that
    # old check read 0 instead of ERROR_ALREADY_EXISTS often enough that a
    # second launch sailed straight past this guard. Reading it via
    # ctypes.get_last_error(), captured immediately, is reliable.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, "Iranzi.Orch.Desktop.SingleInstance")
    ERROR_ALREADY_EXISTS = 183
    already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS

    # Independent second signal: a prior instance whose server is already
    # up. Covers any path where the mutex signal is missed or the handle
    # was inherited.
    if not already_running and _dashboard_already_serving():
        already_running = True

    if already_running:
        _focus_existing_instance()
        os._exit(0)

    # Kept alive for the life of the process -- letting it get garbage
    # collected would release the mutex, defeating the whole point.
    _single_instance_mutex = handle


def _claim_windows_app_identity():
    # Without this, Windows groups the taskbar button under whatever exe
    # actually launched the process -- python.exe's own icon in dev mode,
    # since setting a window icon only controls the window/title-bar icon,
    # not the taskbar identity Windows uses for icon + grouping. This tells
    # Windows "this process is its own distinct app," so the taskbar uses
    # Orch's own icon instead of the host exe's icon.
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Iranzi.Orch.Desktop.1")
    except Exception:
        pass


def _wait_for_server(url, timeout=10, interval=0.05):
    """Polls the dashboard until it actually answers instead of guessing a
    fixed delay -- the previous fixed 1.5s wait was slower than necessary
    whenever the server bound its socket quickly, and could in principle
    still be too short under real load. Returns as soon as the server
    responds, or after `timeout` seconds regardless."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(interval)
    return False


def main():
    _silence_none_streams()
    _enforce_single_instance()
    _claim_windows_app_identity()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    import django

    django.setup()

    from django.core.management import call_command

    call_command("migrate", run_syncdb=True, verbosity=0)

    from . import autostart, start_menu
    from .server import dashboard_url, start_dashboard_server
    from .tray import OrganizerTray

    autostart.enable_on_first_run()
    start_menu.ensure_shortcut()
    start_dashboard_server()

    tray = OrganizerTray()
    tray.run()

    def _open_at_startup():
        _wait_for_server(dashboard_url())
        # desktop-shell/ marks the session as running inside Orch's own
        # window (see organizer.context_processors.desktop_shell) and
        # itself redirects to the setup checklist or the dashboard,
        # whichever is right for this profile -- one URL load covers both
        # cases instead of a second navigation call here.
        tray.main_window.open_path("desktop-shell/", show_after_load=True)

    threading.Thread(target=_open_at_startup, daemon=True).start()

    import webview

    from .assets import ORCH_ICON_PATH

    # Without this, pywebview falls back to extracting an icon from
    # sys.executable -- python.exe's own generic icon in dev mode, since
    # _claim_windows_app_identity() above only fixes taskbar grouping, not
    # which bitmap actually gets shown for the window/taskbar button.
    webview.start(icon=str(ORCH_ICON_PATH))


if __name__ == "__main__":
    main()
