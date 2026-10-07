import json
import re
import string
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from ..core import (
    diagnostics,
    digest as digest_core,
    makerere,
    makerere_curricula,
    muele_api,
    muele_downloader,
    notifications,
    owner_access,
    paths,
    perf,
    rules,
    study,
)
from ..core import pulse as pulse_core
from ..core import summarize as summarize_core
from ..core.watcher import write_log
from ..models import (
    AppSettings,
    AssignmentItem,
    CareerDigest,
    CareerProfile,
    ContentDraft,
    CourseConfig,
    CourseGuide,
    ExportBundle,
    FileSummary,
    Flashcard,
    FolderImportPlan,
    FolderRule,
    GlobalSortCategory,
    GradeTarget,
    IntegrationConnection,
    LearningActivity,
    LearningDigest,
    LearningRoute,
    MoveEvent,
    Notification,
    OrganizationMemoryRule,
    PastPaperAnalysis,
    Profile,
    Project,
    ProjectUpdate,
    PublishedPost,
    ResourceRecommendation,
    ReviewItem,
    SortDecision,
    StudyFocusSession,
    SubjectMemory,
    SubjectTheme,
    StudyGoal,
    SuggestedCourseUnit,
    TimetableEntry,
)


PURPOSE_LABEL_DEFAULTS = {
    "school": {"primary_label": "Year", "secondary_label": "Semester"},
    "online": {"primary_label": "Year", "secondary_label": "Course"},
    "research": {"primary_label": "Topic", "secondary_label": "Phase"},
    "work": {"primary_label": "Department", "secondary_label": "Training Cycle"},
    "custom": {"primary_label": "Year", "secondary_label": "Semester"},
}


def _owner_not_found():
    return HttpResponse("Not found", status=404)


def owner_console(request):
    if not owner_access.request_allowed(request):
        return _owner_not_found()

    User = get_user_model()
    if not User.objects.filter(is_staff=True).exists():
        return redirect("owner_setup")

    return redirect("admin:index")


def owner_setup(request):
    if not owner_access.request_allowed(request):
        return _owner_not_found()

    User = get_user_model()
    if User.objects.filter(is_staff=True).exists():
        return redirect("owner_console")

    errors = []
    values = {"username": "", "email": ""}

    if request.method == "POST":
        values["username"] = request.POST.get("username", "").strip()
        values["email"] = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        confirm_password = request.POST.get("confirm_password", "")

        if not values["username"]:
            errors.append("Choose an owner username.")
        elif User.objects.filter(username=values["username"]).exists():
            errors.append("That username is already in use.")

        if password != confirm_password:
            errors.append("The passwords do not match.")

        try:
            validate_password(password)
        except ValidationError as exc:
            errors.extend(exc.messages)

        if not errors:
            user = User.objects.create_superuser(
                username=values["username"],
                email=values["email"],
                password=password,
            )
            login(request, user)
            messages.success(request, "Owner access is ready.")
            return redirect("admin:index")

    return render(
        request,
        "organizer/owner_setup.html",
        {
            "errors": errors,
            "values": values,
        },
    )


def _write_config_json(profile, config):
    payload = {
        "_comment": (
            "Edit this from the Orch dashboard, or by hand at the start of a "
            f"new {profile.secondary_label.lower()}. primary_value/"
            f"secondary_value become folder names under {profile.root_path}."
        ),
        "primary_value": config.primary_value,
        "secondary_value": config.secondary_value,
        "groups": config.groups,
    }
    try:
        target = paths.config_path(profile.root_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return True, None
    except OSError as exc:
        return False, str(exc)


def _parse_groups(raw):
    return [g.strip() for g in raw.split(",") if g.strip()]


def _save_unverified_course_units(program, primary_value, secondary_value, codes):
    """When Orch has no verified curriculum for this program/year/semester,
    keep what the student actually typed so an admin can later check it
    against an official Makerere source and add it to makerere_curricula.py
    by hand. Never treated as verified, never shown to other students as fact."""
    if makerere_curricula.get_course_units(program, primary_value, secondary_value):
        return
    for code in codes:
        SuggestedCourseUnit.objects.get_or_create(
            program=program,
            primary_value=primary_value,
            secondary_value=secondary_value,
            code=code,
        )


_SKIP_DIR_NAMES = {"$recycle.bin", "system volume information"}


def _is_accessible_dir(path):
    try:
        return path.is_dir()
    except OSError:
        return False


def browse_folders(request):
    """Read-only directory listing of this machine's own filesystem, used by
    the folder-browser picker and the existing-subfolder suggestions in the
    profile forms. The dashboard only ever binds to 127.0.0.1 (see
    gui/server.py), so this is local-machine-only, same trust boundary as
    everything else here -- no separate auth layer, consistent with the rest
    of the app.
    """
    raw_path = request.GET.get("path", "").strip()

    if not raw_path:
        drives = []
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:/")
            if _is_accessible_dir(root):
                drives.append({"name": f"{letter}:\\", "path": str(root)})
        return JsonResponse({"path": "", "parent": None, "folders": drives})

    path = Path(raw_path)
    if not _is_accessible_dir(path):
        return JsonResponse({"error": "That folder doesn't exist or can't be opened."}, status=400)

    try:
        children = [
            p for p in path.iterdir()
            if _is_accessible_dir(p) and not p.name.startswith(".") and p.name.lower() not in _SKIP_DIR_NAMES
        ]
    except OSError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    folders = sorted(
        ({"name": p.name, "path": str(p)} for p in children),
        key=lambda f: f["name"].lower(),
    )
    parent = str(path.parent) if path.parent != path else None
    return JsonResponse({"path": str(path), "parent": parent, "folders": folders})


def activity_ping(request):
    """Tiny, side-effect-free check for whether anything new has happened
    since the caller last looked, for the embedded desktop window's
    background poll (gui/main_window.py). Touches no rendered content --
    the window shows a small "New activity" banner itself when this
    changes, rather than Orch silently patching the page for it."""
    profile = Profile.get_active()
    if not profile:
        return JsonResponse({"latest": None})

    from ..models import Notification

    last_move = MoveEvent.objects.filter(profile=profile).order_by("-timestamp").first()
    last_notification = Notification.objects.filter(profile=profile).order_by("-created_at").first()
    timestamps = [
        t for t in (
            last_move.timestamp if last_move else None,
            last_notification.created_at if last_notification else None,
        ) if t
    ]
    latest = max(timestamps).isoformat() if timestamps else None
    return JsonResponse({"latest": latest})


def status_bar_data(request):
    """Backs the persistent status bar's polling refresh (status-bar.js) --
    same snapshot shape used for its initial server-rendered state, see
    the status_bar context processor."""
    from ..core import status_bar

    profile = Profile.get_active()
    return JsonResponse(status_bar.get_snapshot(profile))


def pulse_data(request):
    """The dashboard live strip's one consolidated poll (dashboard-pulse.js):
    same dict organizer.core.pulse.get_snapshot() renders the strip from on
    first paint. Small indexed queries only, never raises. The client polls
    this at 5s while a task is active, 30s when idle, and not at all while
    the window is hidden -- so it stays cheaper than the third fixed-rate
    timer it replaces. The same poll also carries the activity film's rows,
    so the page never grows a separate timer for that."""
    profile = Profile.get_active()
    payload = pulse_core.get_snapshot(profile)
    payload["activity"], payload["activity_error"] = pulse_core.get_activity_stream_json_with_status(profile)
    payload["controller_unread_count"] = Notification.objects.filter(
        Q(profile=profile) | Q(profile__isnull=True), read_at__isnull=True
    ).count() if profile else 0
    payload["controller"] = _workspace_controller(
        profile,
        pulse_snapshot=payload,
        unread_count=payload["controller_unread_count"],
    )
    return JsonResponse(payload)


def command_palette_search(request):
    """Backs the Ctrl/Cmd+K command palette's file/summary results (the
    static nav destinations it also shows are matched client-side, see
    command-palette.js -- no server round trip needed for a fixed list).

    Reuses the same FTS5 index and safe-fallback shape as the dashboard's
    own Recent Moves search (see dashboard()'s search_query handling
    above): both move_event and file_summary rows resolve back to a
    MoveEvent (FileSummary.record_id is its own MoveEvent's pk, see
    organizer/signals.py), so results always link to a working, existing
    page -- the dashboard filtered to that one file -- rather than a
    detail page that may not exist for every result.
    """
    query = request.GET.get("q", "").strip()
    profile = Profile.get_active()
    if not query or not profile:
        return JsonResponse({"results": []})

    from ..core import search_index

    try:
        matches = search_index.search(
            query, profile.pk, record_types=["move_event", "file_summary"], limit=8,
        )
    except Exception:
        return JsonResponse({"results": []})

    event_ids = list(dict.fromkeys(record_id for _, record_id in matches))[:8]
    events_by_id = MoveEvent.objects.in_bulk(event_ids)

    results = []
    for event_id in event_ids:
        event = events_by_id.get(event_id)
        if not event:
            continue
        results.append({
            "title": event.filename,
            "subtitle": event.course_code or event.destination_path,
            "url": f"{reverse('dashboard')}?q={quote(event.filename)}",
        })
    return JsonResponse({"results": results})


def _today_window():
    now = timezone.localtime()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return now, start, end


def _short_timesince(value):
    if not value:
        return "Never"
    current = timezone.now()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    seconds = max(0, int((current - value).total_seconds()))
    if seconds < 60:
        return "Just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def _next_class(profile, now):
    if not profile:
        return None
    today = now.date()
    current_time = now.time()
    classes = TimetableEntry.objects.filter(profile=profile).filter(
        Q(specific_date=today) | Q(specific_date__isnull=True, weekday=today.weekday())
    )
    return classes.filter(start_time__gte=current_time).order_by("start_time").first()


def _profile_uses_learning_tools(profile):
    return bool(profile and (profile.purpose in {"school", "online"} or profile.setup_path == "makerere"))


def _app_status(profile, last_move):
    watcher = diagnostics.get_watcher_status()
    folders = diagnostics.check_all_watched_folders()
    folders_ok = bool(folders) and all(
        item.get("exists") and item.get("is_dir") and item.get("readable") and item.get("writable")
        for item in folders
    )
    # A configured path is not proof that the watcher is alive. The heartbeat
    # is the source of truth for the live/paused state; folder accessibility
    # is checked separately below.
    primary_folder = next((item for item in folders if item.get("label") == "Primary downloads"), None)
    watched_folder_name = Path(primary_folder["path"]).name if primary_folder and primary_folder.get("path") else None
    watcher_running = bool(watcher.get("running"))
    connected_sources = (
        IntegrationConnection.objects.filter(Q(profile=profile) | Q(profile__isnull=True), status="connected")
        if profile else []
    )
    connected_count = connected_sources.count() if profile else 0
    connected_labels = [connection.get_provider_display() for connection in connected_sources[:2]] if profile else []

    return [
        {
            "label": "Downloads folder",
            "value": f"Watching {watched_folder_name}" if watcher_running and watched_folder_name else "Watcher paused" if watched_folder_name else "Not configured",
            "detail": _short_timesince(datetime.fromisoformat(watcher["last_activity"])) if watcher.get("last_activity") else "No recent check yet",
            "state": "live" if watcher_running and watched_folder_name and folders_ok else "warning",
        },
        {
            "label": "Folders",
            "value": "Ready" if folders_ok else "Needs setup",
            "detail": "Downloads and profile folders are reachable" if folders_ok else "Check folder paths in Settings",
            "state": "live" if folders_ok else "warning",
        },
        {
            "label": "Notifications",
            "value": "Ready" if profile else "Needs profile",
            "detail": "Orch can show reminders while this profile is active" if profile else "Activate a profile first",
            "state": "live" if profile else "warning",
        },
        {
            "label": "Sync",
            "value": "Connected" if connected_count else "Optional",
            "detail": ", ".join(connected_labels) if connected_labels else "Connect optional services when you need them",
            "state": "live" if connected_count else "muted",
        },
        {
            "label": "Last activity",
            "value": _short_timesince(last_move.timestamp) if last_move else "None yet",
            "detail": last_move.filename if last_move else "Sorted files will appear here",
            "state": "live" if last_move else "muted",
        },
    ]


def _today_panel(profile, events, now, start, end):
    if not profile:
        return []
    files_sorted_today = events.filter(success=True, timestamp__gte=start, timestamp__lt=end).count()
    classes_today = TimetableEntry.objects.filter(profile=profile).filter(
        Q(specific_date=now.date()) | Q(specific_date__isnull=True, weekday=now.date().weekday())
    ).count()
    reviews_due_today = ReviewItem.objects.filter(profile=profile, status="queued", due_at__gte=start, due_at__lt=end).count()
    muele_items_today = (
        AssignmentItem.objects.filter(profile=profile, source="muele", created_at__gte=start, created_at__lt=end).count()
        + events.filter(method="muele_sync", timestamp__gte=start, timestamp__lt=end).count()
    )
    unsorted_waiting = SortDecision.objects.filter(profile=profile, status="pending").count()

    return [
        {
            "label": "Files moved today",
            "value": files_sorted_today,
            "detail": "Moved into place",
            "state": "live" if files_sorted_today else "muted",
        },
        {
            "label": "Timetable items",
            "value": classes_today,
            "detail": "From your timetable",
            "state": "live" if classes_today else "muted",
        },
        {
            "label": "Follow-ups due",
            "value": reviews_due_today,
            "detail": "Saved review items",
            "state": "warning" if reviews_due_today else "muted",
        },
        {
            "label": "New MUELE files",
            "value": muele_items_today,
            "detail": "Found today",
            "state": "live" if muele_items_today else "muted",
        },
        {
            "label": "Unsorted waiting",
            "value": unsorted_waiting,
            "detail": "Files to check",
            "state": "warning" if unsorted_waiting else "muted",
        },
    ]


def _next_best_action(profile, now, start, end):
    if not profile:
        return {
            "title": "Create your first profile",
            "detail": "Tell Orch which folder you want organized and what labels you use there.",
            "url": reverse("start"),
            "label": "Start setup",
            "state": "warning",
        }

    pending = SortDecision.objects.filter(profile=profile, status="pending").count()
    if pending:
        return {
            "title": f"Sort {pending} uncertain file{'s' if pending != 1 else ''}",
            "detail": "Approve, reroute, or ignore the files waiting for your decision.",
            "url": reverse("sorting_inbox"),
            "label": "Open inbox",
            "state": "warning",
        }

    review = ReviewItem.objects.filter(profile=profile, status="queued", due_at__lte=end).order_by("due_at").first()
    if review:
        subject = review.subject_code or "General"
        return {
            "title": f"Review {subject}",
            "detail": review.title,
            "url": reverse("review_queue"),
            "label": "Start review",
            "state": "live",
        }

    # A fresh summary is worth reading while it is still useful --
    # a 48h window (vs. the weekly digest check below) because summaries
    # land per-file, far more often than once a week, and nudging days
    # later would just be noise once the reader's already moved on.
    fresh_summary = FileSummary.objects.filter(
        move_event__profile=profile, created_at__gte=now - timedelta(hours=48),
    ).order_by("-created_at").first()
    if fresh_summary:
        return {
            "title": f"Read: {fresh_summary.move_event.filename}",
            "detail": "A new summary is ready for this file.",
            # Opens the same #summary-overlay modal as every "View summary"
            # row action (see summary.js) instead of linking straight to
            # move_summary_view, which is a JSON endpoint the modal fetches
            # from, not a page meant to be navigated to on its own.
            "summary_move_id": fresh_summary.move_event_id,
            "label": "Read summary",
            "state": "live",
        }

    # Same idea for the strongest still-unwatched video Resource Radar has
    # found -- only a real, specific video (a "watch?v=" URL from a
    # configured YouTube key), never the plain search-results fallback
    # link, since there's nothing to "watch" about a search page.
    top_video = ResourceRecommendation.objects.filter(
        profile=profile, source_type="youtube", status="suggested", url__contains="watch?v=",
    ).order_by("-score").first()
    if top_video:
        return {
            "title": f"Watch: {top_video.title}",
            "detail": top_video.reason or "A video pick based on your saved topics.",
            "url": reverse("resource_radar"),
            "label": "Watch it",
            "state": "live",
        }

    timetable_connected = IntegrationConnection.objects.filter(profile=profile, provider="mak_timetable").exists()
    if _profile_uses_learning_tools(profile) and not timetable_connected:
        return {
            "title": "Connect timetable",
            "detail": "Add a timetable if this profile follows classes, sessions, or training times.",
            "url": reverse("timetable_connect"),
            "label": "Connect",
            "state": "warning",
        }

    this_week_digest = LearningDigest.objects.filter(profile=profile, created_at__gte=now - timedelta(days=7)).exists()
    if not this_week_digest:
        return {
            "title": "Make weekly file summary",
            "detail": "Make a simple summary of files moved, topics seen, and things left to do.",
            "post_action": "create_digest",
            "url": reverse("study_home"),
            "label": "Make summary",
            "state": "live",
        }

    route = LearningRoute.objects.filter(profile=profile).exclude(status="done").order_by("status", "-updated_at").first()
    if route:
        return {
            "title": route.title,
            "detail": f"Continue {route.subject_code}: {route.theme}",
            "url": reverse("learning_routes"),
            "label": "Open route",
            "state": "live" if route.status == "active" else "warning",
        }

    return {
        "title": "Open timeline",
        "detail": "See what Orch moved recently.",
        "url": reverse("timeline"),
        "label": "View timeline",
        "state": "muted",
    }


def _live_activity_feed(profile, events, limit=10):
    if not profile:
        return []

    feed = []
    for event in events.order_by("-timestamp")[:limit]:
        feed.append({
            "title": "File sorted" if event.success else "File needs attention",
            "detail": event.filename,
            "meta": event.get_method_display(),
            "when": event.timestamp,
            "state": "live" if event.success else "warning",
        })

    for item in ReviewItem.objects.filter(profile=profile).order_by("-created_at")[:limit]:
        feed.append({
            "title": "Review scheduled",
            "detail": item.title,
            "meta": item.subject_code or "General",
            "when": item.created_at,
            "state": "warning" if item.due_at <= timezone.now() else "live",
        })

    for item in AssignmentItem.objects.filter(profile=profile, source="muele").order_by("-created_at")[:limit]:
        feed.append({
            "title": "MUELE item found",
            "detail": item.title,
            "meta": item.subject_code or "MUELE",
            "when": item.created_at,
            "state": "warning" if item.status == "open" else "live",
        })

    for digest in LearningDigest.objects.filter(profile=profile).order_by("-created_at")[:limit]:
        feed.append({
            "title": "Summary created",
            "detail": digest.title,
            "meta": "Weekly summary",
            "when": digest.created_at,
            "state": "live",
        })

    for session in StudyFocusSession.objects.filter(profile=profile).order_by("-started_at")[:limit]:
        feed.append({
            "title": "Focus session started",
            "detail": session.title,
            "meta": f"{session.target_minutes} min",
            "when": session.started_at,
            "state": "live" if session.status == "active" else "muted",
        })

    return sorted(feed, key=lambda item: item["when"], reverse=True)[:limit]


def _focus_context(profile):
    if not profile:
        return {}

    memories = list(SubjectMemory.objects.filter(profile=profile).order_by("code"))
    config = getattr(profile, "config", None)
    codes = []
    if config:
        codes.extend(config.groups or [])
    codes.extend(memory.code for memory in memories)
    seen = set()
    subjects = []
    for code in codes:
        clean = str(code).strip()
        if clean and clean.lower() not in seen:
            subjects.append(clean)
            seen.add(clean.lower())

    review_files = ReviewItem.objects.filter(profile=profile, status="queued").order_by("due_at")[:8]
    resources = ResourceRecommendation.objects.filter(profile=profile).exclude(status="dismissed").order_by("status", "-score")[:8]
    weak_areas = []
    for memory in memories:
        for area in memory.weak_areas or []:
            weak_areas.append({"subject_code": memory.code, "name": area})

    return {
        "focus_subjects": subjects,
        "focus_review_items": review_files,
        "focus_resources": resources,
        "focus_weak_areas": weak_areas[:10],
        "active_focus_session": StudyFocusSession.objects.filter(profile=profile, status="active").order_by("-started_at").first(),
    }


def _command_items(profile, next_action):
    items = [
        {"label": "Open inbox", "detail": "Sort uncertain files", "url": reverse("sorting_inbox")},
        {"label": "Add folder rule", "detail": "Create a routing rule", "url": reverse("folder_rules")},
        {"label": "Make summary", "detail": "Create a weekly file summary", "post_action": "create_digest"},
        {"label": "Start focus block", "detail": "Use a timer for focused work", "anchor": "focus-mode"},
        {"label": "Open Resource Radar", "detail": "Find videos and books for a topic", "url": reverse("resource_radar")},
        {"label": "Open system setup", "detail": "Review app readiness", "url": reverse("first_run")},
    ]
    if next_action:
        items.insert(0, {
            "label": next_action["title"],
            "detail": next_action["detail"],
            "url": next_action.get("url"),
            "post_action": next_action.get("post_action"),
        })
    if profile:
        config = getattr(profile, "config", None)
        for code in (config.groups if config else [])[:10]:
            items.append({
                "label": f"Go to {code}",
                "detail": "Open profile memory",
                "url": reverse("subject_memory_detail", args=[code]),
            })
    return items


def _service_item(name, state, detail, insight, url=None, action_label="Open"):
    status_labels = {
        "live": "Ready",
        "saved": "Saved",
        "warning": "Needs setup",
        "muted": "Optional",
    }
    status_classes = {
        "live": "",
        "saved": "is-muted",
        "warning": "is-warning",
        "muted": "is-muted",
    }
    return {
        "key": slugify(name),
        "name": name,
        "state": state,
        "status": status_labels.get(state, "Available"),
        "status_class": status_classes.get(state, "is-muted"),
        "detail": detail,
        "insight": insight,
        "url": url,
        "action_label": action_label,
        # Only genuinely-optional ("muted") items can be hidden -- a
        # "Needs setup" warning is a real gap, not a preference, so it
        # always stays visible.
        "dismissible": state == "muted",
    }


def _service_mesh_context(profile, app_status_items, pending_decisions=0):
    from ..core import ai_classify, diagnostics, drive_api, muele_api, youtube_api

    muele_connection = (
        IntegrationConnection.objects.filter(profile=profile, provider="muele").first()
        if profile else None
    )
    timetable_connection = (
        IntegrationConnection.objects.filter(profile=profile, provider="mak_timetable").first()
        if profile else None
    )
    timetable_entries = TimetableEntry.objects.filter(profile=profile).count() if profile else 0
    muele_token_available = bool(muele_api.load_connection_token(muele_connection)) if muele_connection else False
    muele_needs_attention = bool(
        muele_connection and (
            muele_connection.status == "error"
            or (muele_connection.status == "connected" and not muele_token_available)
        )
    )
    muele_connected = bool(
        muele_connection and muele_connection.status == "connected" and muele_token_available
    )
    timetable_connected = bool(timetable_connection and timetable_connection.status == "connected")
    timetable_configured = bool(timetable_connection and (timetable_connection.config or {}).get("group"))

    ai_config = ai_classify.load_ai_config() or {}
    youtube_config = youtube_api.load_youtube_config() or {}
    drive_config = drive_api.load_drive_config() or {}
    smart_orch_ready = bool(ai_config.get("enabled") and ai_config.get("api_key"))
    smart_orch_saved = bool(ai_config.get("api_key"))
    youtube_ready = bool(youtube_config.get("enabled") and youtube_config.get("api_key"))
    youtube_saved = bool(youtube_config.get("api_key"))
    drive_configured = bool(drive_config.get("enabled") and drive_config.get("client_id") and drive_config.get("client_secret"))
    drive_linked = bool(drive_configured and drive_api.is_connected())
    watcher_running = diagnostics.get_watcher_status().get("running", False)
    learning_profile = _profile_uses_learning_tools(profile)

    GlobalSortCategory.ensure_defaults()
    enabled_global_categories = GlobalSortCategory.objects.exclude(key="sensitive").filter(enabled=True)
    enabled_global_count = enabled_global_categories.count()
    learned_rules = OrganizationMemoryRule.objects.filter(profile=profile, enabled=True).count() if profile else 0
    folder_rule_count = FolderRule.objects.filter(profile=profile, enabled=True).count() if profile else 0

    career_profile = CareerProfile.objects.filter(profile=profile).first() if profile else None
    project_count = Project.objects.filter(profile=profile).count() if profile else 0
    draft_count = ContentDraft.objects.filter(profile=profile).exclude(status="posted").count() if profile else 0
    latest_digest = CareerDigest.objects.filter(profile=profile).first() if profile else None

    lanes = [
        {
            "title": "Profile sorting",
            "detail": "Watched folders, rules, and files waiting for your say.",
            "items": [
                _service_item(
                    "Downloads watcher",
                    "live" if watcher_running and app_status_items[0]["state"] == "live" else "warning",
                    app_status_items[0]["value"],
                    app_status_items[0]["detail"],
                    reverse("first_run"),
                    "Check",
                ),
                _service_item(
                    "Folder rules",
                    "live" if folder_rule_count else "muted",
                    f"{folder_rule_count} rule{'s' if folder_rule_count != 1 else ''} turned on" if folder_rule_count else "Add rules for names, types, or folders",
                    "Rules tell Orch where repeat files should go",
                    reverse("folder_rules"),
                    "Open",
                ),
                _service_item(
                    "Files to check",
                    "warning" if pending_decisions else "live",
                    f"{pending_decisions} waiting" if pending_decisions else "Clear",
                    f"{learned_rules} saved rule{'s' if learned_rules != 1 else ''}",
                    reverse("sorting_inbox"),
                    "Review",
                ),
                _service_item(
                    "Extra sorting",
                    "live" if enabled_global_count else "muted",
                    f"{enabled_global_count} extra categor{'ies' if enabled_global_count != 1 else 'y'} enabled",
                    "Media, ebooks, archives, installers, code, and sensitive files are separate opt-ins",
                    reverse("settings_edit"),
                    "Tune",
                ),
            ],
        },
        {
            "title": "Learning tools",
            "detail": "Optional course, timetable, review, and resource helpers.",
            "items": [
                _service_item(
                    "Makerere MUELE",
                    "live" if muele_connected else "warning" if learning_profile and muele_needs_attention else "warning" if learning_profile else "muted",
                    "Course files and assignment dates" if muele_connected else "MUELE needs attention; reconnect or retry" if muele_needs_attention else "Connect to bring in course files and assignment dates" if learning_profile else "Optional Makerere course file sync",
                    f"Last successful sync {_short_timesince(muele_connection.last_sync_at)}" if muele_connection and muele_connection.last_sync_at else "No successful MUELE sync yet",
                    reverse("muele_courses") if muele_connected else reverse("muele_connect"),
                    "Manage" if muele_connected else "Connect",
                ),
                _service_item(
                    "Makerere Timetable",
                    "live" if timetable_connected else "warning" if learning_profile and timetable_connection and timetable_connection.status == "error" else "warning" if learning_profile else "muted",
                    f"{timetable_entries} timetable entries synced" if timetable_connected and timetable_entries else f"Sync succeeded; no timetable entries are currently listed" if timetable_connected else f"Saved for {(timetable_connection.config or {}).get('group')}; sync not verified" if timetable_configured else "Add your timetable" if learning_profile else "Optional timetable reminders",
                    "Useful for class, session, or training reminders",
                    reverse("timetable_view") if timetable_connected else reverse("timetable_connect"),
                    "View" if timetable_connected else "Connect",
                ),
                _service_item(
                    "Resource Radar",
                    "saved" if youtube_ready or youtube_saved else "muted",
                    "YouTube key enabled; checked on use" if youtube_ready else "YouTube key saved" if youtube_saved else "Works with search links; a YouTube key improves video picks",
                    "Finds videos and books for saved topics",
                    reverse("resource_radar"),
                    "Open",
                ),
            ],
        },
        {
            "title": "Projects and backup",
            "detail": "Projects, drafts, summaries, writing help, and Drive backup.",
            "items": [
                _service_item(
                    "Career page",
                    "live" if career_profile else "muted",
                    career_profile.get_career_track_display() if career_profile else "Optional direction and weekly goal",
                    career_profile.weekly_goal if career_profile and career_profile.weekly_goal else "Keeps your files and projects connected to your longer-term goals",
                    reverse("career_home"),
                    "Open",
                ),
                _service_item(
                    "Project Studio",
                    "live" if project_count else "muted",
                    f"{project_count} project{'s' if project_count != 1 else ''} saved" if project_count else "Add a project you are building",
                    "Keeps project files, notes, and proof in one place",
                    reverse("project_studio"),
                    "Open",
                ),
                _service_item(
                    "Drafts",
                    "live" if draft_count else "muted",
                    f"{draft_count} draft{'s' if draft_count != 1 else ''} saved" if draft_count else "Write a short update from your work",
                    "Helps turn real project work into a post you can edit yourself",
                    reverse("content_drafts"),
                    "Draft",
                ),
                _service_item(
                    "Weekly work summary",
                    "live" if latest_digest else "muted",
                    f"Latest summary {_short_timesince(latest_digest.created_at)}" if latest_digest else "No weekly project summary yet",
                    "Summarizes what you worked on, built, and may want to share",
                    reverse("career_digest"),
                    "Make",
                ),
                _service_item(
                    "Summaries and writing help",
                    "saved" if smart_orch_ready or smart_orch_saved else "muted",
                    "Writing help enabled; checked on use" if smart_orch_ready else "Access key saved" if smart_orch_saved else "Optional writing help is not set up",
                    "Orch can still sort files without this",
                    reverse("settings_edit"),
                    "Settings",
                ),
                _service_item(
                    "Google Drive Backup",
                    "saved" if drive_linked or drive_configured else "muted",
                    "Account token stored; checked on backup" if drive_linked else "Configured but not connected" if drive_configured else "Optional backup",
                    "Drive access is confirmed when a backup succeeds",
                    reverse("settings_edit"),
                    "Settings",
                ),
            ],
        },
    ]

    dismissed_keys = set(profile.dismissed_setup_items) if profile else set()
    hidden_count = 0
    for lane in lanes:
        visible_items = []
        for item in lane["items"]:
            if item["dismissible"] and item["key"] in dismissed_keys:
                hidden_count += 1
                continue
            visible_items.append(item)
        lane["items"] = visible_items

    all_items = [item for lane in lanes for item in lane["items"]]
    connected_count = sum(1 for item in all_items if item["state"] == "live")
    total_count = len(all_items)
    missing_count = total_count - connected_count
    readiness = round((connected_count / total_count) * 100) if total_count else 0
    priority_action = next((item for item in all_items if item["state"] == "warning"), None)
    quiet_items = [item for item in all_items if item["state"] == "muted"]
    next_activation = priority_action or (quiet_items[0] if quiet_items else None)

    return {
        "readiness": readiness,
        "connected_count": connected_count,
        "missing_count": missing_count,
        "total_count": total_count,
        "lanes": lanes,
        "priority_action": priority_action,
        "next_activation": next_activation,
        "hidden_count": hidden_count,
        "headline": (
            f"{connected_count} things are ready"
            if connected_count
            else "Let's get Orch set up"
        ),
        "detail": (
            f"Try {next_activation['name']} next so Orch can handle more of your files."
            if next_activation
            else "Everything important is set up."
        ),
    }


def _dashboard_priority_cards(profile, service_mesh, pending_inbox_count):
    # Keep this deck short: the top panel already shows the current file
    # watcher, active profile, recent move, and pending file count.
    return [
        {
            "label": "Projects",
            "title": "Save your project work",
            "detail": "Keep projects, work files, notes, and proof together so you can find them later.",
            "meta": f"{service_mesh['connected_count']} things set up",
            "state": "live" if profile else "warning",
            "url": reverse("career_home"),
            "action_label": "Open projects",
        },
        {
            "label": "Files to check",
            "title": "Review uncertain files",
            "detail": "If Orch is not sure where a file belongs, it waits here for you.",
            "meta": f"{pending_inbox_count} waiting" if pending_inbox_count else "Clear",
            "state": "warning" if pending_inbox_count else "live",
            "url": reverse("sorting_inbox"),
            "action_label": "Review",
        },
    ]


def _cockpit_context(request, profile, events, last_move, controller_pulse=None):
    now, start, end = _today_window()
    next_class = _next_class(profile, now)
    app_status_items = _app_status(profile, last_move)
    today_items = _today_panel(profile, events, now, start, end)
    next_action = _next_best_action(profile, now, start, end)
    pending_inbox_count = SortDecision.objects.filter(profile=profile, status="pending").count() if profile else 0
    service_mesh = _service_mesh_context(profile, app_status_items, pending_decisions=pending_inbox_count)
    dashboard_tool_items = _dashboard_tool_items(service_mesh)
    controller_unread_count = Notification.objects.filter(
        Q(profile=profile) | Q(profile__isnull=True), read_at__isnull=True
    ).count() if profile and controller_pulse is not None else 0

    now_strip = [
        {
            "label": "Downloads folder",
            "detail": f"{app_status_items[0]['value']} - {app_status_items[0]['detail']}",
            "state": app_status_items[0]["state"],
        },
        {
            "label": "Current setup",
            "detail": profile.name if profile else "No active profile",
            "state": "live" if profile else "warning",
        },
        {
            "label": "Last file moved",
            "detail": _short_timesince(last_move.timestamp) if last_move else "No files yet",
            "state": "live" if last_move else "muted",
        },
        {
            "label": "Files to check",
            "detail": f"{pending_inbox_count} waiting" if pending_inbox_count else "Clear",
            "state": "warning" if pending_inbox_count else "muted",
        },
    ]

    return {
        "now_strip": now_strip,
        "today_items": today_items,
        "next_best_action": next_action,
        "live_feed": _live_activity_feed(profile, events),
        "app_status_items": app_status_items,
        "health_items": app_status_items,
        "service_mesh": service_mesh,
        "dashboard_tool_items": dashboard_tool_items,
        "controller_items": _workspace_controller(
            profile,
            now,
            pulse_snapshot=controller_pulse,
            unread_count=controller_unread_count,
        ) if controller_pulse is not None else [],
        "controller_unread_count": controller_unread_count,
        "priority_cards": _dashboard_priority_cards(profile, service_mesh, pending_inbox_count),
        "next_class": next_class,
        "command_items": _command_items(profile, next_action),
        **_focus_context(profile),
    }


def _dashboard_tool_items(service_mesh):
    """Pick the highest-priority tool statuses for the dashboard."""
    state_priority = {"warning": 0, "live": 1, "saved": 2, "muted": 3}
    items = [
        item
        for lane in service_mesh["lanes"]
        if lane["title"] != "Profile sorting"
        for item in lane["items"]
    ]
    items.sort(key=lambda item: state_priority[item["state"]])
    return items[:4]


def _workspace_controller(profile, now=None, pulse_snapshot=None, unread_count=None):
    """Build the live, data-backed action cards for the workspace dashboard."""
    if not profile:
        return []

    from ..core import drive_api

    now = now or timezone.localtime()
    items = []

    pulse_snapshot = pulse_snapshot or pulse_core.get_snapshot(profile)
    lecture = pulse_snapshot.get("lecture")
    if lecture:
        minutes = lecture.get("minutes_until")
        if lecture["state"] == "active":
            timing = "In progress"
        elif minutes is not None:
            timing = "Starts in about an hour" if 45 <= minutes <= 75 else (
                f"Starts in {minutes} min" if minutes < 60 else "Later today"
            )
        else:
            timing = lecture.get("detail") or "In progress"
        items.append({
            "kind": "schedule",
            "title": "Next lesson" if lecture["state"] != "active" else "Lesson in progress",
            "detail": f"{lecture['label']} · {timing}",
            "url": lecture["url"],
            "action": "Open timetable",
            "state": lecture["state"],
        })

    due = pulse_snapshot.get("deadline")
    if due:
        due_at = parse_datetime(due["due_at"]) if due.get("due_at") else None
        items.append({
            "kind": "deadline",
            "title": due.get("title") or due["label"],
            "detail": (
                f"{due.get('subject_code') or 'Assignment'} · due {_short_timesince_future(due_at, now)}"
                if due_at else due["label"]
            ),
            "url": due["url"],
            "action": "Review deadline",
            "state": "warning" if due_at and due_at <= now + timedelta(hours=24) else "live",
        })

    pending_files = SortDecision.objects.filter(profile=profile, status="pending").count()
    if pending_files:
        items.append({
            "kind": "files",
            "title": f"{pending_files} file{'s' if pending_files != 1 else ''} need a decision",
            "detail": "Orch is waiting for your approval before moving these files.",
            "url": reverse("sorting_inbox"),
            "action": "Review files",
            "state": "warning",
        })

    queued_reviews = pulse_snapshot.get("needs_you")
    if queued_reviews:
        items.append({
            "kind": "review",
            "title": f"{queued_reviews['count']} review item{'s' if queued_reviews['count'] != 1 else ''} saved",
            "detail": "Your study follow-ups are ready when you are.",
            "url": queued_reviews["url"],
            "action": "Open reviews",
            "state": "live",
        })

    recommendation = ResourceRecommendation.objects.filter(
        profile=profile, source_type="youtube", status="suggested"
    ).order_by("-score", "-updated_at").first()
    if recommendation:
        items.append({
            "kind": "resource",
            "title": recommendation.title,
            "detail": f"{recommendation.subject_code or recommendation.theme} · {recommendation.reason or 'Recommended from your saved study topics'}",
            "url": recommendation.url,
            "action": "Open YouTube pick",
            "state": "live",
            "external": True,
        })

    failed_backups = MoveEvent.objects.filter(
        profile=profile, drive_backup_status="failed"
    ).count()
    quota = drive_api.storage_quota_snapshot(profile)
    if quota and quota.get("percent", 0) >= 90:
        percent = quota["percent"]
        items.append({
            "kind": "storage",
            "title": "Google Drive is full" if percent >= 100 else "Google Drive storage is low",
            "detail": (
                "Free Drive space, then retry the pending Orch backups."
                if percent >= 100
                else f"{percent}% used · {failed_backups} Orch backup{'s' if failed_backups != 1 else ''} pending"
            ),
            "url": reverse("connections_home"),
            "action": "Review Drive backups",
            "state": "warning",
        })
    elif failed_backups:
        items.append({
            "kind": "storage",
            "title": f"{failed_backups} Drive backup{'s' if failed_backups != 1 else ''} need attention",
            "detail": "The files are safe on this device. Check Drive connection or available space, then retry.",
            "url": reverse("connections_home"),
            "action": "Review and retry",
            "state": "warning",
        })

    connection_errors = list(
        IntegrationConnection.objects.filter(
            Q(profile=profile) | Q(profile__isnull=True), status="error"
        ).order_by("display_name")[:3]
    )
    if connection_errors:
        labels = ", ".join(connection.display_name for connection in connection_errors)
        items.append({
            "kind": "connection",
            "title": "A connected service needs attention",
            "detail": labels,
            "url": reverse("connections_home"),
            "action": "Check connections",
            "state": "warning",
        })

    if unread_count is None:
        unread_count = Notification.objects.filter(
            Q(profile=profile) | Q(profile__isnull=True), read_at__isnull=True
        ).count()
    unread = unread_count
    if unread:
        items.append({
            "kind": "notifications",
            "title": f"{unread} new notification{'s' if unread != 1 else ''}",
            "detail": "Updates from your connected tools and Orch are collected here.",
            "url": reverse("notifications"),
            "action": "Open updates",
            "state": "live",
        })
    return items


def _short_timesince_future(value, now):
    seconds = max(0, int((value - now).total_seconds()))
    if seconds < 3600:
        return f"in {max(1, seconds // 60)} min"
    if seconds < 86400:
        hours = seconds // 3600
        return f"in {hours} hr"
    days = seconds // 86400
    return f"in {days} day{'s' if days != 1 else ''}"


def _create_focus_session(request, profile):
    subject_code = request.POST.get("focus_subject", "").strip()
    raw_minutes = request.POST.get("focus_minutes", "25").strip()
    try:
        minutes = max(5, min(240, int(raw_minutes)))
    except ValueError:
        minutes = 25

    review_ids = [int(pk) for pk in request.POST.getlist("focus_review_items") if pk.isdigit()]
    resource_ids = [int(pk) for pk in request.POST.getlist("focus_resources") if pk.isdigit()]
    weak_areas = request.POST.getlist("focus_weak_areas")
    title_subject = subject_code or profile.name
    session = StudyFocusSession.objects.create(
        profile=profile,
        subject_code=subject_code,
        title=f"{title_subject} focus session",
        target_minutes=minutes,
        review_item_ids=review_ids,
        resource_ids=resource_ids,
        weak_areas=weak_areas,
        notes=request.POST.get("focus_notes", "").strip(),
    )
    LearningActivity.objects.create(
        profile=profile,
        activity_type="manual_note",
        subject_code=subject_code,
        title=f"Focus session started: {title_subject}",
        details=f"{minutes} minute focus block",
        metadata={"focus_session_id": session.pk},
    )
    return session


def desktop_shell_enter(request):
    """The one URL Orch's own desktop window (gui/main_window.py) loads
    first, instead of the dashboard directly -- marks this browser session
    as "inside the desktop shell" so base.html's context processor
    (organizer.context_processors.desktop_shell) can show the frameless
    window's own minimize/close titlebar on every page from here on,
    without every internal link needing to carry that state itself. A
    regular browser tab never hits this URL, so it never sees that
    titlebar. Also does the same first-run-vs-existing-profile routing
    gui/app.py used to do with a second navigation call, so the desktop
    window only ever has to load one URL to get started."""
    request.session["is_desktop_shell"] = True
    if not Profile.objects.exists():
        return redirect("start")
    return redirect("dashboard")


def _sorting_pulse_context(profile, events):
    """Route-method counts and per-subject file counts -- the numbers behind
    the dashboard's compact "Sorting" summary line and the full Sorting
    Report page. Pure move out of dashboard(); query shape is unchanged."""
    method_counts = list(events.values("method").annotate(total=Count("id")).order_by("-total"))
    method_labels = dict(MoveEvent.METHOD_CHOICES)
    for row in method_counts:
        row["label"] = method_labels.get(row["method"], row["method"])
        last_of_method = events.filter(method=row["method"]).order_by("-timestamp").first()
        row["last_event"] = last_of_method
    course_counts = list(
        events.exclude(course_code__isnull=True)
        .exclude(course_code="")
        .values("course_code")
        .annotate(total=Count("id"))
        .order_by("-total")
    )
    return {
        "method_counts": method_counts,
        "course_counts": course_counts,
        "total_moves": events.count(),
        "top_method": method_counts[0] if method_counts else None,
        "subjects_receiving": len(course_counts),
    }


def _recent_moves_context(request, profile, events):
    """Recent Moves search + pagination. The dashboard now shows only the
    first few rows with a "View all" link, but the paginator stays: the
    instant-search JS still fetches this same view with ?q= and swaps in a
    fresh slice, and the query-count budget test pins this shape."""
    # Recent Moves search: filters just the table, not the stat boxes above
    # it, so searching for one file doesn't make the overall counts look
    # like they changed. Indexed (FTS5) first, matching filenames, course
    # codes, and summary text -- falls back to the plain filename-only
    # icontains this used before if the index query fails for any reason,
    # rather than showing a broken/empty table.
    search_query = request.GET.get("q", "").strip()
    if search_query:
        try:
            from ..core import search_index

            matches = search_index.search(
                search_query, profile.pk if profile else None,
                record_types=["move_event", "file_summary"], limit=500,
            )
            table_events = events.filter(pk__in={record_id for _, record_id in matches})
        except Exception:
            table_events = events.filter(filename__icontains=search_query)
    else:
        table_events = events

    # select_related avoids one extra query per row for each: the "Why?"
    # panel's event.sort_decision.matched_rule lookup, and the Actions
    # menu's event.summary check (both reverse-OneToOne, both looped over
    # in the recent-moves partial -- an N+1 for every row otherwise).
    paginator = Paginator(table_events.select_related("sort_decision", "summary"), 25)
    page_number = request.GET.get("page", 1)
    return {
        "page_obj": paginator.get_page(page_number),
        "search_query": search_query,
    }


def _muele_panel_context(profile):
    """MUELE course/deadline/sync figures for the dashboard summary line and
    the MUELE Courses page link. Unchanged from the inline version."""
    muele_connection = None
    muele_courses_count = 0
    muele_upcoming_deadlines = []
    muele_last_synced_course = None
    if profile:
        from ..models import AssignmentItem, IntegrationConnection, MueleCourse

        muele_connection = IntegrationConnection.objects.filter(
            profile=profile, provider="muele"
        ).first()
        if muele_connection:
            muele_courses_count = MueleCourse.objects.filter(
                connection=muele_connection, auto_download=True
            ).count()
            muele_upcoming_deadlines = list(
                AssignmentItem.objects.filter(
                    profile=profile, source="muele", status="open"
                ).order_by("due_at")[:5]
            )
            muele_last_synced_course = (
                MueleCourse.objects.filter(connection=muele_connection)
                .exclude(last_sync_at__isnull=True)
                .order_by("-last_sync_at")
                .first()
            )
    return {
        "muele_connection": muele_connection,
        "muele_courses_count": muele_courses_count,
        "muele_upcoming_deadlines": muele_upcoming_deadlines,
        "muele_next_deadline": muele_upcoming_deadlines[0] if muele_upcoming_deadlines else None,
        "muele_last_synced_course": muele_last_synced_course,
    }


@perf.measure_view
def dashboard(request):
    profile = Profile.get_active()
    if profile:
        notifications.check_deadlines(profile, log=write_log)
        notifications.check_upcoming_classes(profile, log=write_log)
    events = MoveEvent.objects.filter(profile=profile) if profile else MoveEvent.objects.none()
    last_move = events.filter(success=True).order_by("-timestamp").first()
    config = getattr(profile, "config", None) if profile else None
    guided_codes = set(
        CourseGuide.objects.filter(profile=profile).values_list("course_code", flat=True)
    ) if profile else set()

    context = {
        "profile": profile,
        "has_any_profile": Profile.objects.exists(),
        "config": config,
        "guided_codes": guided_codes,
        "last_move": last_move,
    }
    context.update(_sorting_pulse_context(profile, events))
    context.update(_recent_moves_context(request, profile, events))
    context.update(_muele_panel_context(profile))
    context["pulse"] = pulse_core.get_snapshot(profile)
    context.update(_cockpit_context(
        request, profile, events, last_move, controller_pulse=context["pulse"]
    ))
    context["activity_stream"], context["activity_stream_error"] = pulse_core.get_activity_stream_with_status(profile)
    return render(request, "organizer/dashboard.html", context)


def sorting_report(request):
    """Sorting Pulse (files per route method) and Subject Distribution
    (files per subject) -- the two analytics tables that used to sit on the
    dashboard as full-width cards. Kept reachable here, linked from the
    dashboard's compact summary row."""
    profile = Profile.get_active()
    events = MoveEvent.objects.filter(profile=profile) if profile else MoveEvent.objects.none()
    context = {
        "profile": profile,
        "last_move": events.filter(success=True).order_by("-timestamp").first(),
    }
    context.update(_sorting_pulse_context(profile, events))
    return render(request, "organizer/sorting_report.html", context)


def privacy_policy(request):
    return render(request, "organizer/privacy.html", {"profile": Profile.get_active()})


def setup_item_dismiss(request):
    """Hides one optional ("muted") setup checklist item from the dashboard.
    Only ever called for items the view itself marked dismissible -- a real
    "Needs setup" warning can't be hidden this way, see _service_item."""
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")
    item_key = request.POST.get("item_key", "").strip()
    if item_key and item_key not in profile.dismissed_setup_items:
        profile.dismissed_setup_items = [*profile.dismissed_setup_items, item_key]
        profile.save(update_fields=["dismissed_setup_items"])
    return redirect("dashboard")


def setup_items_restore(request):
    """Brings back every optional setup item previously hidden via
    setup_item_dismiss for the active profile."""
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")
    profile.dismissed_setup_items = []
    profile.save(update_fields=["dismissed_setup_items"])
    return redirect("dashboard")


def profiles_list(request):
    profiles = Profile.objects.all()
    return render(request, "organizer/profiles_list.html", {"profiles": profiles})




def profile_wizard(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        purpose = request.POST.get("purpose") or "custom"
        primary_label = request.POST.get("primary_label", "").strip() or "Year"
        secondary_label = request.POST.get("secondary_label", "").strip() or "Semester"
        root_path = request.POST.get("root_path", "").strip()
        primary_value = request.POST.get("primary_value", "").strip()
        secondary_value = request.POST.get("secondary_value", "").strip()
        groups = _parse_groups(request.POST.get("groups", ""))
        ai_fallback_enabled = bool(request.POST.get("ai_fallback_enabled"))

        if not name or not root_path:
            messages.error(request, "A profile needs at least a name and a folder to organize into.")
            return render(request, "organizer/profile_wizard.html", {
                "purposes": Profile.PURPOSE_CHOICES,
                "purpose_defaults": PURPOSE_LABEL_DEFAULTS,
                "form": request.POST,
            })

        profile = Profile.objects.create(
            name=name,
            purpose=purpose,
            setup_path="manual",
            primary_label=primary_label,
            secondary_label=secondary_label,
            root_path=root_path,
            ai_fallback_enabled=ai_fallback_enabled,
            is_active=True,
        )
        config = CourseConfig.objects.create(
            profile=profile,
            primary_value=primary_value,
            secondary_value=secondary_value,
            groups=groups,
        )
        ok, error = _write_config_json(profile, config)
        study.ensure_learning_foundation(profile)
        if ok:
            messages.success(request, f"'{profile.name}' is set up and active.")
        else:
            messages.error(request, f"Profile saved, but could not write _config.json: {error}")

        return redirect("dashboard")

    return render(request, "organizer/profile_wizard.html", {
        "purposes": Profile.PURPOSE_CHOICES,
        "purpose_defaults": PURPOSE_LABEL_DEFAULTS,
    })


def start(request):
    """First stop when setting up a profile: a Makerere-specific guided
    path, or the generic wizard for everyone else."""
    return render(request, "organizer/start.html")


def makerere_wizard(request):
    colleges_json = makerere.as_json()
    curricula_json = {name: data["years"] for name, data in makerere_curricula.CURRICULA.items()}

    if request.method == "POST":
        college_name = request.POST.get("college", "").strip()
        school_name = request.POST.get("school", "").strip()
        program = request.POST.get("program", "").strip()
        year_value = request.POST.get("year_value", "").strip()
        semester_value = request.POST.get("semester_value", "").strip()
        root_path = request.POST.get("root_path", "").strip()
        groups = _parse_groups(request.POST.get("groups", ""))
        ai_fallback_enabled = bool(request.POST.get("ai_fallback_enabled"))

        college = makerere.get_college_by_name(college_name)

        if not college or not school_name or not program or not root_path or not year_value or not semester_value:
            messages.error(
                request,
                "Pick your college and school, fill in your program, year, semester, and a folder to organize into.",
            )
            return render(request, "organizer/makerere_wizard.html", {
                "colleges": makerere.COLLEGES,
                "colleges_json": colleges_json,
                "curricula_json": curricula_json,
                "default_root_hint": str(paths.PERSONAL_ROOT / "Makerere"),
                "form": request.POST,
            })

        profile_name = f"{program} ({college['code']}) - Makerere University"
        profile = Profile.objects.create(
            name=profile_name,
            purpose="school",
            setup_path="makerere",
            primary_label="Year",
            secondary_label="Semester",
            root_path=root_path,
            ai_fallback_enabled=ai_fallback_enabled,
            is_active=True,
        )
        config = CourseConfig.objects.create(
            profile=profile,
            primary_value=f"Year {year_value}",
            secondary_value=f"Semester {semester_value}",
            groups=groups,
        )
        _save_unverified_course_units(program, config.primary_value, config.secondary_value, groups)
        ok, error = _write_config_json(profile, config)
        study.ensure_learning_foundation(profile)

        from ..core import sorting
        folder_result = sorting.ensure_subject_folders(profile)

        if ok:
            extras = []
            if folder_result["created"]:
                extras.append(f"created {len(folder_result['created'])} folder(s)")
            if folder_result["renamed"]:
                extras.append(f"named {len(folder_result['renamed'])} existing folder(s)")
            if extras:
                messages.success(request, f"'{profile.name}' is set up and active. {', '.join(extras).capitalize()}.")
            else:
                messages.success(request, f"'{profile.name}' is set up and active.")
        else:
            messages.error(request, f"Profile saved, but could not write _config.json: {error}")

        return redirect("dashboard")

    return render(request, "organizer/makerere_wizard.html", {
        "colleges": makerere.COLLEGES,
        "colleges_json": colleges_json,
        "curricula_json": curricula_json,
        "default_root_hint": str(paths.PERSONAL_ROOT / "Makerere"),
    })


def profile_edit(request, pk):
    profile = get_object_or_404(Profile, pk=pk)
    config = getattr(profile, "config", None)

    if request.method == "POST":
        from ..core import sorting

        if request.POST.get("action") == "sync_folders":
            result = sorting.ensure_subject_folders(profile)
            parts = []
            if result["created"]:
                parts.append(f"created {len(result['created'])} folder(s): {', '.join(result['created'])}")
            if result["renamed"]:
                parts.append(f"named {len(result['renamed'])} existing folder(s): {', '.join(result['renamed'])}")
            if parts:
                messages.success(request, f"Done: {'; '.join(parts)}.")
            elif result["existing"]:
                messages.info(request, f"All {len(result['existing'])} subject folders already exist and are already named. Nothing to change.")
            else:
                messages.info(request, "No subjects configured yet -- add some below first.")
            return redirect("profile_edit", pk=profile.pk)

        profile.name = request.POST.get("name", "").strip() or profile.name
        profile.primary_label = request.POST.get("primary_label", "").strip() or profile.primary_label
        profile.secondary_label = request.POST.get("secondary_label", "").strip() or profile.secondary_label
        profile.root_path = request.POST.get("root_path", "").strip() or profile.root_path
        profile.ai_fallback_enabled = bool(request.POST.get("ai_fallback_enabled"))
        profile.save()

        if config is None:
            config = CourseConfig(profile=profile)
        config.primary_value = request.POST.get("primary_value", "").strip()
        config.secondary_value = request.POST.get("secondary_value", "").strip()
        config.groups = _parse_groups(request.POST.get("groups", ""))
        config.save()

        ok, error = _write_config_json(profile, config)
        folder_result = sorting.ensure_subject_folders(profile)
        if ok:
            extras = []
            if folder_result["created"]:
                extras.append(f"created {len(folder_result['created'])} new subject folder(s)")
            if folder_result["renamed"]:
                extras.append(f"named {len(folder_result['renamed'])} existing folder(s)")
            if extras:
                messages.success(request, f"Profile updated, {', '.join(extras)}.")
            else:
                messages.success(request, "Profile updated.")
        else:
            messages.error(request, f"Saved to database but could not write _config.json: {error}")

        return redirect("profile_edit", pk=profile.pk)

    return render(request, "organizer/profile_edit.html", {"profile": profile, "config": config})


def profile_activate(request, pk):
    profile = get_object_or_404(Profile, pk=pk)
    if request.method == "POST":
        profile.is_active = True
        profile.save()
        messages.success(request, f"'{profile.name}' is now active.")
    return redirect("profiles_list")


def profile_delete(request, pk):
    profile = get_object_or_404(Profile, pk=pk)
    if request.method == "POST":
        name = profile.name
        profile.delete()
        messages.success(request, f"Deleted '{name}'.")
    return redirect("profiles_list")


def settings_edit(request):
    from ..core import ai_classify, drive_api, owner_access, youtube_api

    settings = AppSettings.get_solo()
    ai_config = ai_classify.load_ai_config() or {}
    youtube_config = youtube_api.load_youtube_config() or {}
    drive_config = drive_api.load_drive_config() or {}
    GlobalSortCategory.ensure_defaults()

    if request.method == "POST":
        # Validate up front and refuse the whole save on a bad value, rather
        # than silently keeping the old path / ignoring a non-numeric day
        # count -- the client-side form blocks these too, this is the
        # backstop for a direct POST or JS-off.
        errors = []
        downloads_path = request.POST.get("downloads_path", "").strip()
        library_inbox_path = request.POST.get("library_inbox_path", "").strip()
        if not downloads_path:
            errors.append("The primary downloads folder can't be empty.")
        if not library_inbox_path:
            errors.append("The ebook inbox folder can't be empty.")

        def _positive_int(field, label):
            raw = request.POST.get(field, "").strip()
            try:
                value = int(raw)
            except (TypeError, ValueError):
                errors.append(f"{label} needs to be a whole number.")
                return None
            if value < 1:
                errors.append(f"{label} needs to be 1 or more.")
                return None
            return value

        stale_days = _positive_int("installer_stale_days", "Installer review-after days")
        delete_days = _positive_int("installer_delete_days", "Installer delete-after days")

        if errors:
            for message in errors:
                messages.error(request, message)
            return redirect("settings_edit")

        settings.downloads_path = downloads_path
        settings.secondary_downloads_path = request.POST.get("secondary_downloads_path", "").strip()
        settings.library_inbox_path = library_inbox_path
        settings.installer_stale_days = stale_days
        settings.installer_delete_days = delete_days
        global_default_mode = request.POST.get("global_default_mode", "")
        if global_default_mode in dict(AppSettings.GLOBAL_DEFAULT_MODE_CHOICES):
            settings.global_default_mode = global_default_mode
        settings.save()

        # Automation Control: one enabled/destination/mode triple per
        # opt-in category. `sensitive` is deliberately excluded -- its
        # UI is locked and it's never submitted as an editable field.
        for category in GlobalSortCategory.objects.exclude(key="sensitive"):
            category.enabled = request.POST.get(f"category_{category.key}_enabled") == "on"
            destination = request.POST.get(f"category_{category.key}_destination", "").strip()
            if destination:
                category.destination_path = destination
            mode = request.POST.get(f"category_{category.key}_mode", "")
            if mode in dict(GlobalSortCategory.MODE_CHOICES):
                category.mode = mode
            category.save()

        ai_enabled = bool(request.POST.get("ai_enabled"))
        ai_api_key = request.POST.get("ai_api_key", "").strip()
        if ai_enabled or ai_api_key or paths.AI_CONFIG_PATH.exists():
            ai_config["enabled"] = ai_enabled
            if ai_api_key:
                ai_config["api_key"] = ai_api_key
            ai_config.setdefault("model", "llama-3.1-8b-instant")
            ai_config.setdefault("base_url", "https://api.groq.com/openai/v1")
            paths.AI_CONFIG_PATH.write_text(json.dumps(ai_config, indent=2), encoding="utf-8")

        youtube_enabled = bool(request.POST.get("youtube_enabled"))
        youtube_api_key = request.POST.get("youtube_api_key", "").strip()
        if youtube_enabled or youtube_api_key or paths.YOUTUBE_CONFIG_PATH.exists():
            youtube_config["enabled"] = youtube_enabled
            if youtube_api_key:
                youtube_config["api_key"] = youtube_api_key
            paths.YOUTUBE_CONFIG_PATH.write_text(json.dumps(youtube_config, indent=2), encoding="utf-8")

        drive_enabled = bool(request.POST.get("drive_enabled"))
        drive_client_id = request.POST.get("drive_client_id", "").strip()
        drive_client_secret = request.POST.get("drive_client_secret", "").strip()
        if drive_enabled or drive_client_id or paths.DRIVE_CONFIG_PATH.exists():
            drive_config["enabled"] = drive_enabled
            if drive_client_id:
                drive_config["client_id"] = drive_client_id
            if drive_client_secret:
                drive_config["client_secret"] = drive_client_secret
            paths.DRIVE_CONFIG_PATH.write_text(json.dumps(drive_config, indent=2), encoding="utf-8")

        messages.success(request, "Settings saved.")
        return redirect("settings_edit")

    User = get_user_model()
    drive_connection = IntegrationConnection.objects.filter(provider="drive").first()
    return render(request, "organizer/settings_edit.html", {
        "settings": settings,
        "default_downloads": str(paths.DEFAULT_DOWNLOADS),
        "default_library_inbox": str(paths.DEFAULT_LIBRARY_INBOX),
        "ai_enabled": ai_config.get("enabled", False),
        "ai_key_set": bool(ai_config.get("api_key")),
        "youtube_enabled": youtube_config.get("enabled", False),
        "youtube_key_set": bool(youtube_config.get("api_key")),
        "drive_enabled": drive_config.get("enabled", False),
        "drive_client_configured": bool(drive_config.get("client_id") and drive_config.get("client_secret")),
        "drive_connected": drive_api.is_connected(),
        "drive_account_email": drive_connection.config.get("email") if drive_connection else None,
        "owner_mode": owner_access.owner_mode_enabled(),
        "owner_mode_available": owner_access.feature_available(),
        "owner_account_exists": User.objects.filter(is_staff=True).exists(),
        "sort_categories": GlobalSortCategory.objects.exclude(key="sensitive"),
        "sensitive_category": GlobalSortCategory.objects.filter(key="sensitive").first(),
    })


def owner_mode_toggle(request):
    """Its own tiny save, independent of the big Settings form -- it used
    to be one more field on that form, which meant saving ANY other
    setting (changing the downloads folder, say) with the checkbox
    visually unchecked would silently turn owner mode back off and hide
    the Admin link again, even for someone who'd already set up their
    account. A standalone toggle can't be reset by an unrelated save."""
    from ..core import owner_access

    if not owner_access.feature_available():
        return _owner_not_found()

    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    enabled = request.POST.get("enabled") == "true"
    owner_access.owner_config_path().write_text(
        json.dumps({"owner_mode": enabled}, indent=2), encoding="utf-8"
    )
    return JsonResponse({"ok": True, "enabled": enabled})
