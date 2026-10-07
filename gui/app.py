"""Desktop entry point. Sets up Django (ORM + migrations) before touching
the GUI, then runs the dashboard server and the file watcher as background
threads under one system tray icon -- no console window, no separate
`manage.py runserver`/`manage.py migrate` steps for the packaged exe.
"""

import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path


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
            response = urllib.request.urlopen(url, timeout=0.5)
            response.close()
            return True
        except urllib.error.HTTPError as response:
            response.close()
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(interval)
    return False


def _report_startup_failure(exc):
    local_app_data = os.environ.get("LOCALAPPDATA")
    log_dir = (
        Path(local_app_data) / "Orch" / "logs"
        if local_app_data else Path.home() / ".orch" / "logs"
    )
    log_path = log_dir / "startup.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- Orch startup failure ---\n{traceback.format_exc()}\n")
    except OSError:
        log_path = None

    message = f"Orch couldn't start: {exc}"
    if log_path:
        message += f"\n\nDetails were saved to:\n{log_path}"

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "Orch couldn't start", 0x10)
            return
        except (AttributeError, OSError):
            pass
    print(message, file=sys.stderr)


def _run_application():
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
    if not _wait_for_server(dashboard_url()):
        raise RuntimeError(
            "Orch's local dashboard did not start. Restart the app and, if it "
            "happens again, share the startup log with Orch support."
        )

    tray = OrganizerTray()

    import webview

    from .assets import ORCH_ICON_PATH

    # Without this, pywebview falls back to extracting an icon from
    # sys.executable -- python.exe's own generic icon in dev mode, since
    # _claim_windows_app_identity() above only fixes taskbar grouping, not
    # which bitmap actually gets shown for the window/taskbar button.
    try:
        tray.run()
        webview.start(icon=str(ORCH_ICON_PATH))
    except Exception:
        tray.watcher.stop()
        tray.icon.stop()
        raise


def main():
    _silence_none_streams()
    try:
        _enforce_single_instance()
        _claim_windows_app_identity()
        _run_application()
    except Exception as exc:
        _report_startup_failure(exc)


if __name__ == "__main__":
    main()
