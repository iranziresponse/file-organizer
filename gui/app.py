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


def _find_existing_window():
    """Find this install's top-level window even after the page changes its title."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    executable = os.path.normcase(os.path.realpath(sys.executable))
    windows = []

    def collect(hwnd, _):
        process_id = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)):
            return True

        process = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not process:
            return True
        try:
            image_path = ctypes.create_unicode_buffer(32768)
            image_length = wintypes.DWORD(len(image_path))
            if not kernel32.QueryFullProcessImageNameW(
                process, 0, image_path, ctypes.byref(image_length)
            ):
                return True
            if os.path.normcase(os.path.realpath(image_path.value)) != executable:
                return True

            title_length = user32.GetWindowTextLengthW(hwnd)
            title_buffer = ctypes.create_unicode_buffer(title_length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
            title = title_buffer.value.strip()
            orch_title = "orch" in title.casefold()
            if not getattr(sys, "frozen", False) and not orch_title:
                return True

            # Prefer the app window's normal title; retain a same-process
            # fallback for frozen builds if a backend temporarily clears it.
            priority = 0 if title.casefold() == "orch" else 1 if orch_title else 2
            windows.append((priority, hwnd, process_id.value))
            return True
        finally:
            kernel32.CloseHandle(process)

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    callback = callback_type(collect)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows(callback, 0)

    if not windows:
        return None
    _, hwnd, process_id = min(windows, key=lambda item: item[0])
    return hwnd, process_id


def _focus_existing_instance():
    """Restore the running window when a user launches Orch a second time."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        existing_window = _find_existing_window()
        if not existing_window:
            return False
        hwnd, process_id = existing_window
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AllowSetForegroundWindow(process_id)
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE also unhides a hidden window.
        user32.BringWindowToTop(hwnd)
        if not user32.SetForegroundWindow(hwnd):
            user32.FlashWindow(hwnd, True)
        return bool(user32.IsWindowVisible(hwnd))
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _report_existing_instance_not_found():
    message = (
        "Orch is already running, but Windows could not restore its window. "
        "Open the Orch icon in the notification area and choose “Open Orch”."
    )
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Orch is already running", 0x40)
    except (AttributeError, OSError):
        print(message, file=sys.stderr)


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
        if not _focus_existing_instance():
            _report_existing_instance_not_found()
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
