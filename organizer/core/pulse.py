"""Live-state snapshot for the dashboard's top strip and activity film.

Same contract as organizer.core.status_bar.get_snapshot(): a plain dict of
small, indexed queries, safe to call on every request and every poll, and
it never raises -- any failure degrades to a calm resting state, never a
stack trace on the page.

Every value here is backed by a real database row or filesystem fact. When
nothing is happening, badges render their resting state (or hide); nothing
animates or counts to imply activity that is not there.
"""

from datetime import datetime, timedelta
import logging

from django.urls import reverse
from django.utils import timezone

from .status_bar import _short_timesince

logger = logging.getLogger(__name__)

# Show the "Deadline" badge only for something genuinely near.
DEADLINE_HORIZON_DAYS = 14
# A recent MoveEvent burst is what "Sorting now" reacts to when there is no
# large_folder_sort task running.
RECENT_MOVE_WINDOW = timedelta(minutes=5)
# TimetableEntry.end_time is nullable; treat a dateless lecture as this long.
DEFAULT_LECTURE_MINUTES = 60
ACTIVITY_CARD_LIMIT = 10


def _idle():
    return {
        "active": False,
        "sorting": {"state": "resting", "label": "Watcher idle", "url": reverse("first_run")},
        "in_flight": None,
        "lecture": None,
        "needs_you": None,
        "deadline": None,
    }


def _sorting_badge(profile, now):
    from ..models import AppSettings, BackgroundTask, MoveEvent
    from pathlib import Path

    task = (
        BackgroundTask.objects.filter(
            profile=profile, kind="large_folder_sort", status__in=["queued", "running"]
        )
        .order_by("-created_at")
        .first()
    )
    if task:
        return {
            "state": "active",
            "label": "Sorting a folder now",
            "detail": _progress_text(task),
            "url": reverse("timeline"),
        }, True

    recent_moves = MoveEvent.objects.filter(
        profile=profile, timestamp__gte=now - RECENT_MOVE_WINDOW
    ).count()
    if recent_moves:
        return {
            "state": "active",
            "label": f"Sorted {recent_moves} file{'s' if recent_moves != 1 else ''} just now",
            "url": reverse("timeline"),
        }, True

    settings = AppSettings.get_solo()
    folder = Path(settings.downloads_path).name if settings.downloads_path else None
    return {
        "state": "resting",
        "label": f"Watching {folder}" if folder else "No folder watched yet",
        "url": reverse("first_run"),
    }, False


def _progress_text(task):
    if task.progress_total:
        return f"{task.progress_current} of {task.progress_total}"
    # progress_total is null/0 -> indeterminate; the template renders a
    # spinner rather than a 0% bar (see the BackgroundTask docstring).
    return None


def _in_flight_badge(profile):
    from ..models import BackgroundTask

    tasks = list(
        BackgroundTask.objects.filter(
            profile=profile, status__in=["queued", "running"]
        ).order_by("-created_at")[:4]
    )
    if not tasks:
        return None, False
    lead = tasks[0]
    return {
        "state": "active",
        "label": lead.get_kind_display(),
        "detail": _progress_text(lead),
        "indeterminate": not lead.progress_total,
        "count": len(tasks),
        "url": reverse("notifications"),
    }, True


def _lecture_badge(profile, now):
    """A TimetableEntry with kind='teaching' running right now, else the next
    one still to come today. Mirrors views.dashboard._next_class's matching
    (weekday recomputed against today, or an explicit specific_date)."""
    from django.db.models import Q

    from ..models import TimetableEntry

    today = now.date()
    current_time = now.time()
    teaching_today = TimetableEntry.objects.filter(profile=profile, kind="teaching").filter(
        Q(specific_date=today) | Q(specific_date__isnull=True, weekday=today.weekday())
    )

    for entry in teaching_today.filter(start_time__lte=current_time).order_by("-start_time"):
        if entry.end_time is not None:
            running = entry.end_time >= current_time
        else:
            ends = (
                datetime.combine(today, entry.start_time)
                + timedelta(minutes=DEFAULT_LECTURE_MINUTES)
            ).time()
            running = ends >= current_time
        if running:
            where = entry.room or entry.course_name
            return {
                "state": "active",
                "label": f"{entry.course_code or 'Lecture'} now",
                "detail": where or None,
                "url": reverse("timetable_view"),
            }

    nxt = (
        teaching_today.filter(start_time__gt=current_time)
        .order_by("start_time")
        .first()
    )
    if nxt:
        minutes_until = max(
            0,
            int(
                (
                    datetime.combine(today, nxt.start_time)
                    - now.replace(tzinfo=None)
                ).total_seconds()
                / 60
            ),
        )
        return {
            "state": "resting",
            "label": f"Next: {nxt.course_code or 'lecture'} at {nxt.start_time.strftime('%H:%M')}",
            "minutes_until": minutes_until,
            "url": reverse("timetable_view"),
        }
    return None


def _needs_you_badge(profile):
    from ..models import ReviewItem

    count = ReviewItem.objects.filter(profile=profile, status="queued").count()
    if not count:
        return None
    return {
        "state": "active",
        "label": f"{count} waiting for you",
        "count": count,
        "url": reverse("review_queue"),
    }


def _deadline_badge(profile, now):
    from ..models import AssignmentItem

    horizon = now + timedelta(days=DEADLINE_HORIZON_DAYS)
    item = (
        AssignmentItem.objects.filter(
            profile=profile, status="open", due_at__isnull=False,
            due_at__gte=now, due_at__lte=horizon,
        )
        .order_by("due_at")
        .first()
    )
    if not item:
        return None
    return {
        "state": "resting",
        "label": f"{item.title} due {_short_timesince_future(item.due_at, now)}",
        "title": item.title,
        "subject_code": item.subject_code,
        "due_at": item.due_at.isoformat(),
        "url": reverse("assignment_tracker"),
    }


def _short_timesince_future(value, now):
    seconds = max(0, int((value - now).total_seconds()))
    if seconds < 3600:
        return f"in {max(1, seconds // 60)} min"
    if seconds < 86400:
        return f"in {seconds // 3600} hr"
    days = seconds // 86400
    return f"in {days} day{'s' if days != 1 else ''}"


def get_snapshot(profile):
    """Plain-dict snapshot for GET /api/pulse/ and the dashboard's initial
    server render. Never raises."""
    if not profile:
        return {"has_profile": False, **_idle()}

    try:
        now = timezone.now()
        sorting, sorting_active = _sorting_badge(profile, now)
        in_flight, in_flight_active = _in_flight_badge(profile)
        return {
            "has_profile": True,
            "pulse_error": False,
            "active": bool(sorting_active or in_flight_active),
            "sorting": sorting,
            "in_flight": in_flight,
            # Lecture matching is against naive local wall-clock TimeFields,
            # so it needs local now, not UTC now -- same as
            # views.dashboard._next_class. TIME_ZONE is set from tzlocal.
            "lecture": _lecture_badge(profile, timezone.localtime(now)),
            "needs_you": _needs_you_badge(profile),
            "deadline": _deadline_badge(profile, now),
        }
    except Exception:
        logger.exception("Could not build dashboard pulse for profile %s.", profile.pk)
        return {"has_profile": True, **_idle(), "pulse_error": True}


# --- Activity film -----------------------------------------------------------

_ICONS = {
    "move": "file",
    "move_failed": "alert",
    "notification": "bell",
    "task_done": "check",
    "task_failed": "alert",
    "review_done": "check",
    "review_queued": "clock",
}


def get_activity_stream(profile, limit=ACTIVITY_CARD_LIMIT):
    """Reverse-chronological union of what Orch has been doing: MoveEvent,
    Notification, finished BackgroundTask, and ReviewItem state changes.
    select_related() on the profile/course FKs -- four related models in one
    view is exactly where an N+1 bites.
    """
    return get_activity_stream_with_status(profile, limit)[0]


def get_activity_stream_with_status(profile, limit=ACTIVITY_CARD_LIMIT):
    """Return activity rows and whether the data query failed.

    The dashboard can keep rendering if this query fails, but it must not
    describe a failed read as an honestly empty activity history.
    """
    if not profile:
        return [], False
    try:
        return _build_activity_stream(profile, limit), False
    except Exception:
        logger.exception("Could not load dashboard activity for profile %s.", profile.pk)
        return [], True


def _build_activity_stream(profile, limit):
    from ..models import BackgroundTask, MoveEvent, Notification, ReviewItem

    limit = max(0, min(limit, ACTIVITY_CARD_LIMIT))
    rows = []

    for ev in (
        MoveEvent.objects.filter(profile=profile)
        .select_related("profile")
        .order_by("-timestamp")[:limit]
    ):
        rows.append({
            "kind": "move" if ev.success else "move_failed",
            "icon": _ICONS["move" if ev.success else "move_failed"],
            "text": (
                f"Sorted {ev.filename}" if ev.success else f"Could not sort {ev.filename}"
            ),
            "url": f"{reverse('dashboard')}?q={ev.filename}",
            "when": ev.timestamp,
        })

    for note in (
        Notification.objects.filter(profile=profile)
        .select_related("profile")
        .order_by("-created_at")[:limit]
    ):
        rows.append({
            "kind": "notification",
            "icon": _ICONS["notification"],
            "text": note.title,
            "url": reverse("notifications"),
            "when": note.created_at,
        })

    for task in (
        BackgroundTask.objects.filter(profile=profile, status__in=["done", "failed"])
        .select_related("profile")
        .order_by("-finished_at", "-created_at")[:limit]
    ):
        done = task.status == "done"
        rows.append({
            "kind": "task_done" if done else "task_failed",
            "icon": _ICONS["task_done" if done else "task_failed"],
            "text": (
                f"{task.get_kind_display()} finished"
                if done else f"{task.get_kind_display()} failed"
            ),
            "url": reverse("notifications"),
            "when": task.finished_at or task.created_at,
        })

    for item in (
        ReviewItem.objects.filter(profile=profile)
        .select_related("profile", "move_event")
        .order_by("-created_at")[:limit]
    ):
        if item.status == "done":
            rows.append({
                "kind": "review_done",
                "icon": _ICONS["review_done"],
                "text": f"Reviewed {item.subject_code or item.title}",
                "url": reverse("review_queue"),
                "when": item.completed_at or item.created_at,
            })
        else:
            rows.append({
                "kind": "review_queued",
                "icon": _ICONS["review_queued"],
                "text": f"Review scheduled: {item.subject_code or item.title}",
                "url": reverse("review_queue"),
                "when": item.created_at,
            })

    rows = [r for r in rows if r["when"] is not None]
    rows.sort(key=lambda r: r["when"], reverse=True)
    rows = rows[:limit]
    for r in rows:
        r["when_short"] = _short_timesince(r["when"])
        r["when_iso"] = r["when"].isoformat()
        # Stable-enough identity for the film's client-side dedupe: an event
        # keeps the same key across polls, a new one gets a fresh key.
        r["key"] = f"{r['kind']}:{r['when_iso']}:{r['text'][:60]}"
    return rows


def get_activity_stream_json(profile, limit=ACTIVITY_CARD_LIMIT):
    """get_activity_stream() with the datetime dropped, for the pulse
    endpoint's JSON payload."""
    return get_activity_stream_json_with_status(profile, limit)[0]


def get_activity_stream_json_with_status(profile, limit=ACTIVITY_CARD_LIMIT):
    rows, failed = get_activity_stream_with_status(profile, limit)
    return [
        {k: v for k, v in row.items() if k != "when"}
        for row in rows
    ], failed
