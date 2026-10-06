"""Native desktop window for Orch.

The real Django dashboard is embedded in a taskbar-visible pywebview window.
The window stays frameless so Orch can keep its slim in-app titlebar, but all
window actions are backed by native Win32 fallbacks so resize/minimize behave
like normal desktop controls.
"""

import sys
import threading
import time

import webview

from .server import dashboard_url

# Matches base.html's light-theme --bg-deep (the app's default theme). A
# mismatched window background would flash visibly for an instant on load
# since there's a brief gap between the window appearing and the page's
# own CSS painting.
PAGE_BACKGROUND = "#F0F2F5"


class _JSApi:
    """Exposed to the page as window.pywebview.api.<name>(). Only ever
    called from the custom titlebar's own buttons and keyboard shortcuts
    (see base.html), never by arbitrary page content."""

    def __init__(self, window_holder):
        self._window_holder = window_holder

    def minimize(self):
        self._window_holder.minimize()

    def toggle_maximize(self):
        # Queries the real OS window state (IsZoomed) instead of trusting a
        # separately-maintained is_maximized flag -- a shadow copy of state
        # that lives outside the one place (Windows itself) actually
        # tracking it. Any path that changes WindowState without going
        # through this exact flag (the resize border's own drag-to-maximize
        # via Windows' native double-click-titlebar handling, Aero Snap, a
        # future feature) would desync a shadow flag silently; querying
        # reality directly can't drift.
        holder = self._window_holder
        if holder.is_maximized():
            holder.restore()
        else:
            holder.maximize()
        return holder.is_maximized()

    def toggle_fullscreen(self):
        self._window_holder.window.toggle_fullscreen()
        self._window_holder.is_fullscreen = not self._window_holder.is_fullscreen
        self._window_holder.schedule_repaint_nudge()
        return self._window_holder.is_fullscreen

    def close_to_tray(self):
        # Same behavior as clicking the OS close button on the old framed
        # window: hide, don't quit (see _on_closing below).
        self._window_holder.window.hide()


class OrchMainWindow:
    """Taskbar-visible window showing the live dashboard. Closing it hides
    the window rather than quitting the app -- the tray icon and the
    background watcher keep running either way (see gui/tray.py)."""

    @staticmethod
    def _sized_and_centered_for_screen(preferred_width, preferred_height):
        # A fixed 1280x820 window (no x/y) left it up to the OS default
        # placement, which on a display too small to fit that size (a
        # smaller/older laptop panel, or a scaled-down secondary monitor)
        # neither centers nor shrinks it -- it just plants the window near
        # the top-left with its right/bottom edges hanging off the visible
        # screen. Those edges are exactly where the custom titlebar's
        # minimize/maximize/close buttons live, so that's the real cause
        # behind "no shrink window, launches in the corner, cannot click
        # close or minimize": confirmed by measuring the actual window
        # rect on a 1280x720 display, which came back as (25,25)-(1290,725)
        # for a requested 1280x820 window -- clipped on both edges, not
        # centered. Clamping to the real primary screen size and centering
        # explicitly keeps the whole window (and its controls) on-screen
        # regardless of the display it launches on.
        try:
            screen = webview.screens[0]
            screen_width = int(screen.width)
            screen_height = int(screen.height)
        except Exception:
            return preferred_width, preferred_height, None, None

        margin_x, margin_y = 40, 60  # leaves room for the taskbar/DPI rounding
        width = max(640, min(preferred_width, screen_width - margin_x))
        height = max(460, min(preferred_height, screen_height - margin_y))
        x = max(0, (screen_width - width) // 2)
        y = max(0, (screen_height - height) // 2)
        return width, height, x, y

    def __init__(self, watcher_controller=None):
        self.watcher = watcher_controller
        self.is_fullscreen = False
        self._show_after_load = False
        # No initial url: the dashboard server may not have finished binding
        # its socket yet when this window is created (see gui/app.py's
        # startup sequence), and loading before it's ready would show a
        # connection-refused page for an instant. app.py loads the real URL
        # once it has confirmed the server actually answers.
        width, height, x, y = self._sized_and_centered_for_screen(1280, 820)
        self.window = webview.create_window(
            "Orch Loading",
            width=width,
            height=height,
            x=x,
            y=y,
            min_size=(640, 460),
            background_color=PAGE_BACKGROUND,
            hidden=True,
            frameless=True,
            resizable=True,
            easy_drag=False,
            js_api=_JSApi(self),
        )
        self.window.events.closing += self._on_closing
        self.window.events.shown += self._on_shown
        self.window.events.loaded += self._on_loaded
        self._resize_border_installed = False

    def _on_shown(self):
        # frameless=True drops Windows' own resize grips entirely (see
        # gui/resize_border.py), so they're added back here once the real
        # native window exists. events.shown can in principle fire more
        # than once for the same window (e.g. some backends refire it after
        # a restore), so this is guarded to install at most once.
        if self._resize_border_installed or sys.platform != "win32":
            return
        self._resize_border_installed = True
        try:
            from . import resize_border

            hwnd = self._native_hwnd()
            if hwnd:
                resize_border.install(hwnd)
        except Exception:
            pass

    def _on_loaded(self):
        # The same WebView2 compositor-desync glitch schedule_repaint_nudge()
        # already works around for minimize/maximize/restore/fullscreen (see
        # its own docstring) also hits the very first paint: the window is
        # created hidden, gui/app.py navigates it to desktop-shell/ while
        # it's still hidden, and only shows it afterward -- so the swap
        # chain's first real content lands while there's no visible frame
        # to have already resynced against. Confirmed by direct testing
        # (CDP-inspecting the live page: the DOM and computed CSS are
        # already correct -- data-theme, colors, button positions all
        # match the intended light theme -- so this is a stale composited
        # frame, not a template/CSS bug). The custom titlebar renders as a
        # flat, dark, button-less strip until something forces a resize,
        # and this event -- pywebview's own "the page finished loading"
        # signal -- is the first reliable point after real content exists
        # to do that.
        self.schedule_repaint_nudge()
        if self._show_after_load:
            self._show_after_load = False
            self.window.title = "Orch"
            self.show()

    def show(self):
        # A freshly launched WebView can otherwise expose its empty native
        # surface while the dashboard is still loading. Defer startup and
        # tray show requests until the first real page has finished loading.
        if self._show_after_load:
            return
        self._clamp_to_screen()
        self.window.show()
        try:
            self.restore()
        except Exception:
            pass

    def _clamp_to_screen(self):
        # webview.screens (queried in _sized_and_centered_for_screen, before
        # the native window exists) turned out not to be reliable enough on
        # its own: even after clamping to it, a real rebuilt onedir exe still
        # landed at a rect like (101,101)-(1366,801) on a 1280x720 screen --
        # still hanging off both the right and bottom edges. WinForms only
        # finalizes real DPI-aware bounds once the form is actually
        # associated with a monitor, which happens later than window
        # creation -- so this re-checks against the real Win32 monitor work
        # area (taskbar-aware, unlike raw screen resolution) right before
        # the window is actually shown, while it's still hidden, so any
        # correction here never produces a visible flash/jump.
        if sys.platform != "win32":
            return
        hwnd = self._native_hwnd()
        if not hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            MONITOR_DEFAULTTONEAREST = 2
            monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if not ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                return
            work = info.rcWork
            work_width = work.right - work.left
            work_height = work.bottom - work.top

            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top

            margin = 16
            new_width = max(640, min(width, work_width - margin))
            new_height = max(460, min(height, work_height - margin))
            new_x = work.left + max(0, (work_width - new_width) // 2)
            new_y = work.top + max(0, (work_height - new_height) // 2)

            if (new_width, new_height, new_x, new_y) != (width, height, rect.left, rect.top):
                ctypes.windll.user32.MoveWindow(hwnd, new_x, new_y, new_width, new_height, True)
        except Exception:
            pass

    def minimize(self):
        # pywebview's own minimize()/maximize()/restore() set the WinForms
        # Form's WindowState property through a proper Invoke() marshal to
        # the UI thread -- that's the SAME property toggle_fullscreen()
        # reads and writes for its own bookkeeping (old_state, is_fullscreen
        # etc). Calling raw ShowWindow() instead changes the real OS window
        # state without updating that property, so the two mechanisms drift
        # out of sync with each other -- this was the actual cause behind
        # minimize/maximize/fullscreen intermittently "doing nothing": not
        # a broken click, but a stale WindowState left over from whichever
        # of the two mechanisms touched the window last. Raw ShowWindow is
        # now only a last-resort fallback, not the primary path.
        try:
            self.window.minimize()
            return
        except Exception:
            pass
        self._show_window(6)  # SW_MINIMIZE

    def maximize(self):
        try:
            self.window.maximize()
        except Exception:
            self._show_window(3)  # SW_MAXIMIZE
        self.schedule_repaint_nudge()

    def restore(self):
        try:
            self.window.restore()
        except Exception:
            self._show_window(9)  # SW_RESTORE
        self.schedule_repaint_nudge()

    def is_maximized(self):
        if sys.platform != "win32":
            return False
        hwnd = self._native_hwnd()
        if not hwnd:
            return False
        try:
            import ctypes

            return bool(ctypes.windll.user32.IsZoomed(hwnd))
        except Exception:
            return False

    def schedule_repaint_nudge(self):
        """WebView2's compositor can lose sync with the window's actual
        size after a WindowState change (minimize/maximize/restore/
        fullscreen), leaving the page rendered as a blank frame -- this is
        a real, reproducible glitch confirmed by direct testing, not rare
        or exotic, and it takes only a handful of ordinary toggles to hit.
        A plain repaint request (InvalidateRect/UpdateWindow) does NOT
        recover it; only an actual size change does, which forces the
        swap chain to reallocate. So: wait for the state change to finish
        settling, then grow the window by 1px and immediately shrink it
        back -- imperceptible to the user, but enough to force WebView2 to
        resync, instead of leaving them staring at a blank window."""
        if sys.platform != "win32":
            return

        def _nudge():
            time.sleep(0.2)
            hwnd = self._native_hwnd()
            if not hwnd:
                return
            try:
                import ctypes
                from ctypes import wintypes

                rect = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                if width <= 0 or height <= 0:
                    return  # minimized -- nothing visible to repaint yet
                ctypes.windll.user32.MoveWindow(hwnd, rect.left, rect.top, width + 1, height, True)
                ctypes.windll.user32.MoveWindow(hwnd, rect.left, rect.top, width, height, True)
            except Exception:
                pass

        threading.Thread(target=_nudge, daemon=True).start()

    def _show_window(self, command):
        if sys.platform != "win32":
            return
        hwnd = self._native_hwnd()
        if not hwnd:
            return
        try:
            import ctypes

            ctypes.windll.user32.ShowWindow(hwnd, command)
        except Exception:
            pass

    def _native_hwnd(self):
        """Return the real Win32 HWND for pywebview's native form when present."""
        native = getattr(self.window, "native", None)
        if native is None:
            return None

        for name in ("Handle", "handle", "hwnd"):
            value = getattr(native, name, None)
            if value is None:
                continue
            if callable(value):
                try:
                    value = value()
                except Exception:
                    continue
            for converter in ("ToInt64", "ToInt32"):
                method = getattr(value, converter, None)
                if method:
                    try:
                        return int(method())
                    except Exception:
                        continue
            try:
                return int(value)
            except Exception:
                continue
        return None

    def open_path(self, path="", show_after_load=False):
        """Navigate the embedded view to a specific dashboard path, e.g.
        "study/" or "profiles/new/" -- used by the tray menu. Set
        show_after_load for the initial desktop launch so the native window
        stays hidden until the dashboard has rendered."""
        if show_after_load:
            self._show_after_load = True
        try:
            self.window.load_url(dashboard_url() + path)
        except Exception:
            if show_after_load:
                self._show_after_load = False
            raise

    def _on_closing(self):
        # Returning False cancels the close; hide instead so the tray icon
        # and watcher keep running, matching the old closeEvent-ignore
        # behavior.
        self.window.hide()
        return False
