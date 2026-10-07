"""Notification bus for Orch. Sends desktop notifications via the
system tray icon when events happen (new files sorted, deadlines
approaching, MUELE sync complete, etc.).

Uses pystray's Icon.notify() for desktop notifications (see gui/tray.py's
notification-polling thread). All notification functions are safe to call
from any thread.
"""

import logging
from datetime import datetime, timedelta
from typing import Callable

from django.utils import timezone

logger = logging.getLogger("organizer.notifications")

# Queue of pending notifications. Producers only ever append(), and
# pop_pending() clears it by rebinding the name to a new empty list rather
# than mutating in place, so a concurrent append can't be lost mid-clear --
# relies on CPython's GIL making both operations atomic, not an explicit
# lock. Polled from a background thread in gui/tray.py.
_pending_notifications: list[dict] = []


# ---------------------------------------------------------------------------
# Notification data
# ---------------------------------------------------------------------------


class Notification:
    """A notification to be shown via the system tray."""

    def __init__(self, title: str, message: str, icon: int = 0, urgency: str = "normal"):
        self.title = title
        self.message = message
        self.icon = icon  # QSystemTrayIcon.MessageIcon value
        self.urgency = urgency  # "low", "normal", "critical"
        self.timestamp = datetime.now()

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "message": self.message,
            "urgency": self.urgency,
            "timestamp": self.timestamp.isoformat(),
        }


# ---------------------------------------------------------------------------
# Enqueue notifications
# ---------------------------------------------------------------------------


def notify(title: str, message: str, urgency: str = "normal", profile=None) -> None:
    """Queue a live tray toast, and persist it so it's still visible in the
    web UI later even if the toast was missed or the tray wasn't running."""
    _pending_notifications.append({
        "title": title,
        "message": message,
        "urgency": urgency,
        "timestamp": datetime.now().isoformat(),
    })

    from organizer.models import Notification

    Notification.objects.create(profile=profile, title=title, message=message, urgency=urgency)


def pop_pending() -> list[dict]:
    """Retrieve and clear all pending notifications. Called by the tray."""
    global _pending_notifications
    batch = _pending_notifications
    _pending_notifications = []
    return batch


def has_pending() -> bool:
    return len(_pending_notifications) > 0


# ---------------------------------------------------------------------------
# Notification generators
# ---------------------------------------------------------------------------


def check_deadlines(profile, log: Callable | None = None) -> int:
    """Check for upcoming/missed assignment deadlines and notify.

    Cheap enough to call on every dashboard/study page load: each
    assignment only ever triggers one notification per stage
    (warning/urgent/missed), tracked on deadline_notified_stage, so
    repeated page loads never re-notify about the same deadline.

    Returns the number of notifications sent.
    """
    from organizer.models import AssignmentItem

    now = timezone.now()
    count = 0

    # Urgent: due within 24 hours
    urgent = AssignmentItem.objects.filter(
        profile=profile,
        status="open",
        due_at__gte=now,
        due_at__lte=now + timedelta(hours=24),
    ).exclude(deadline_notified_stage="urgent")
    for assign in urgent:
        hours_left = int((assign.due_at - now).total_seconds() / 3600)
        notify(
            f"Due soon: {assign.title}",
            f"{hours_left}h remaining, {assign.subject_code or 'No subject'}",
            urgency="critical",
            profile=profile,
        )
        assign.deadline_notified_stage = "urgent"
        assign.save(update_fields=["deadline_notified_stage"])
        count += 1

    # Warning: due within 48 hours
    warning = AssignmentItem.objects.filter(
        profile=profile,
        status="open",
        due_at__gte=now + timedelta(hours=24),
        due_at__lte=now + timedelta(hours=48),
    ).exclude(deadline_notified_stage__in=["warning", "urgent"])
    for assign in warning:
        notify(
            f"Due tomorrow: {assign.title}",
            f"Due in {(assign.due_at - now).total_seconds() / 3600:.0f}h",
            urgency="normal",
            profile=profile,
        )
        assign.deadline_notified_stage = "warning"
        assign.save(update_fields=["deadline_notified_stage"])
        count += 1

    # Missed
    missed = AssignmentItem.objects.filter(
        profile=profile,
        status="open",
        due_at__lt=now,
    ).exclude(deadline_notified_stage="missed")
    for assign in missed:
        notify(
            f"Missed: {assign.title}",
            f"Was due {assign.due_at.strftime('%Y-%m-%d %H:%M') if assign.due_at else 'unknown'}",
            urgency="critical",
            profile=profile,
        )
        assign.deadline_notified_stage = "missed"
        assign.save(update_fields=["deadline_notified_stage"])
        count += 1

    return count


def check_upcoming_classes(profile, minutes_before: int = 15, log: Callable | None = None) -> int:
    """Check for lectures, tests, and exams starting soon and notify.

    Cheap enough to call on every dashboard/study page load, same as
    check_deadlines(): each entry only ever notifies once for the
    occurrence it's currently pointing at, tracked on
    last_notified_on, so repeated page loads never double-notify.

    Uses the machine's own local clock (not Django's UTC "now"), since a
    lecture reminder needs to fire against the wall-clock time the user
    actually sees, not a timezone-shifted one -- this only makes sense
    running on the student's own PC, set to their own local time.

    Returns the number of notifications sent.
    """
    from organizer.models import TimetableEntry

    now = datetime.now()
    today = now.date()
    weekday = now.weekday()  # Monday=0 ... matches TimetableEntry.WEEKDAY_CHOICES
    window_end = (now + timedelta(minutes=minutes_before)).time()
    count = 0

    # Recurring weekly lectures (Teaching/Recess): match today's weekday,
    # not a stored date, since the site never publishes a semester start
    # date to anchor one against.
    upcoming_classes = TimetableEntry.objects.filter(
        profile=profile,
        kind__in=["teaching", "recess"],
        weekday=weekday,
        start_time__gte=now.time(),
        start_time__lte=window_end,
    ).exclude(last_notified_on=today)
    for entry in upcoming_classes:
        label = f"{entry.course_code} {entry.course_name}".strip()
        detail = f"Starts {entry.start_time.strftime('%H:%M')}"
        if entry.room:
            detail += f", {entry.room}"
        notify(f"Upcoming lecture: {label}", detail, urgency="normal", profile=profile)
        entry.last_notified_on = today
        entry.save(update_fields=["last_notified_on"])
        count += 1

    # Tests/Examinations: only entries with a confidently-parsed date --
    # never guess a reminder time from a date Orch couldn't fully resolve.
    upcoming_exams = TimetableEntry.objects.filter(
        profile=profile,
        kind__in=["test", "examination"],
        specific_date=today,
        start_time__gte=now.time(),
        start_time__lte=window_end,
    ).exclude(last_notified_on=today)
    for entry in upcoming_exams:
        label = f"{entry.course_code} {entry.course_name}".strip()
        notify(
            f"Upcoming {entry.get_kind_display().lower()}: {label}",
            f"Starts {entry.start_time.strftime('%H:%M')} today",
            urgency="critical",
            profile=profile,
        )
        entry.last_notified_on = today
        entry.save(update_fields=["last_notified_on"])
        count += 1

    return count


def notify_muele_sync(result: dict, profile=None) -> None:
    """Send a notification after a MUELE sync completes."""
    if result.get("downloaded", 0) > 0:
        notify(
            "MUELE sync complete",
            f"{result['downloaded']} new files downloaded, "
            f"{result['skipped']} skipped, "
            f"{result['errors']} errors",
            profile=profile,
        )


def notify_file_sorted(filename: str, method: str, destination: str, profile=None) -> None:
    """Notify that a file was sorted."""
    method_labels = {
        "course_code": "Subject match",
        "topic": "Topic match",
        "muele_sync": "MUELE download",
        "ebook": "Ebook",
        "sensitive": "Sensitive file",
        "media": "Media",
        "installer": "Installer",
        "unsorted": "Unsorted",
        "needs_sorting": "Needs sorting",
    }
    label = method_labels.get(method, method)
    dest_path = Path(destination)
    # Include the drive letter, not just the folder name -- Windows systems
    # commonly route different profiles/downloads across multiple drives
    # (C:, D:, ...), so "BIO101" alone doesn't say which one this landed on.
    drive = dest_path.drive or ""
    notify(label, f"{filename} → {drive}\\...\\{dest_path.parent.name}", urgency="low", profile=profile)


def notify_new_items(title: str, source: str, count: int, profile=None) -> None:
    """Send one native toast for a batch of newly synced items, not one per row."""
    if count < 1:
        return
    noun = "item" if count == 1 else "items"
    verb = "is" if count == 1 else "are"
    notify(
        title,
        f"{count} new {source} {noun} {verb} ready in Orch.",
        urgency="normal",
        profile=profile,
    )


def notify_drive_backup_attention(profile) -> None:
    """Rate-limit backup failure alerts while preserving each failure in move history."""
    from datetime import timedelta

    from django.utils import timezone

    from organizer.models import Notification

    title = "Google Drive backup needs attention"
    if Notification.objects.filter(
        profile=profile,
        title=title,
        created_at__gte=timezone.now() - timedelta(minutes=30),
    ).exists():
        return
    notify(
        title,
        "An Orch backup did not reach Drive. Check your connection or available space, then retry.",
        urgency="critical",
        profile=profile,
    )


# Import Path here to avoid circular imports at module level
from pathlib import Path  # noqa: E402