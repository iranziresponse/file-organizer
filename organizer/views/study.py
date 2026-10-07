import json
import re
import string
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

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

from .dashboard import _cockpit_context, _create_focus_session


def digest_overview(request):
    """Dedicated page showing all digests with detail view and generation."""
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        action = request.POST.get("action", "")
        period = request.POST.get("period", "weekly")
        if action == "generate":
            if period == "daily":
                digest = digest_core.generate_daily_digest(profile)
            else:
                digest = digest_core.generate_weekly_digest(profile)
            if digest:
                messages.success(request, f"{period.title()} digest created.")
            else:
                messages.info(request, "No activity to report in this period.")
            return redirect("digest_overview")

    digests = digest_core.get_recent_digests(profile, limit=20)
    schedule_info = digest_core.get_digest_schedule_info(profile)

    return render(request, "organizer/digest_overview.html", {
        "profile": profile,
        "study_context": ctx,
        "digests": digests,
        "schedule_info": schedule_info,
    })


def digest_detail(request, pk):
    """View a single digest with full content."""
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    digest = get_object_or_404(LearningDigest, pk=pk, profile=profile)
    ctx = get_context_for_profile(profile)

    return render(request, "organizer/digest_detail.html", {
        "profile": profile,
        "study_context": ctx,
        "digest": digest,
    })




@perf.measure_view
def resource_radar(request):
    """Recommend transparent video and book discovery links from subject
    memory, themes, weak areas, and recent files."""
    from ..core import resources as resource_core
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)
    subject_code = request.GET.get("subject", "").strip()

    if request.method == "POST":
        action = request.POST.get("action", "")
        recommendation_pk = request.POST.get("recommendation_pk")

        if action == "generate":
            selected_subject = request.POST.get("subject_code", "").strip() or None
            created = resource_core.sync_recommendations(profile, subject_code=selected_subject, limit=24)
            messages.success(request, f"Resource Radar refreshed {len(created)} recommendation(s).")
            if selected_subject:
                return redirect(f"{reverse('resource_radar')}?subject={selected_subject}")
            return redirect("resource_radar")

        if recommendation_pk:
            item = get_object_or_404(ResourceRecommendation, pk=recommendation_pk, profile=profile)
            if action in {"saved", "suggested", "dismissed", "opened"}:
                resource_core.set_recommendation_status(item, action)
                return redirect(request.POST.get("next") or "resource_radar")

        if action == "dismiss_all_suggested":
            from ..core import resource_cache

            dismiss_subject = request.POST.get("subject_code", "").strip() or None
            queryset = ResourceRecommendation.objects.filter(profile=profile, status="suggested")
            if dismiss_subject:
                queryset = queryset.filter(subject_code=dismiss_subject)
            count = queryset.update(status="dismissed")
            # A plain queryset .update() skips model .save(), so the
            # post_save signal that normally invalidates this cache never
            # fires -- invalidate explicitly so the dismissed items don't
            # keep showing from a stale cached list.
            resource_cache.invalidate_resource_recommendations(profile.pk)
            messages.success(request, f"Dismissed {count} suggested recommendation(s).")
            if dismiss_subject:
                return redirect(f"{reverse('resource_radar')}?subject={dismiss_subject}")
            return redirect("resource_radar")

    from ..core import resource_cache

    recommendations = resource_cache.get_or_set_resource_recommendations(
        profile.pk, subject_code,
        lambda: list(resource_core.recommendations_for_profile(
            profile,
            subject_code=subject_code or None,
            limit=40,
        )),
    )
    subject_memories = SubjectMemory.objects.filter(profile=profile).order_by("code")
    saved_count = ResourceRecommendation.objects.filter(profile=profile, status="saved").count()

    return render(request, "organizer/resource_radar.html", {
        "profile": profile,
        "study_context": ctx,
        "subject_memories": subject_memories,
        "recommendations": recommendations,
        "current_subject": subject_code,
        "saved_count": saved_count,
    })


@perf.measure_view
def study_home(request):
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if profile:
        notifications.check_deadlines(profile, log=write_log)
        notifications.check_upcoming_classes(profile, log=write_log)
    ctx = get_context_for_profile(profile)

    if profile and request.method == "POST" and request.POST.get("action") == "create_digest":
        result = digest_core.generate_weekly_digest(profile)
        if result:
            messages.success(request, "Weekly digest created.")
        else:
            messages.info(request, "No activity to report this week.")
        return redirect("study_home")

    if profile and request.method == "POST" and request.POST.get("action") == "start_focus":
        session = _create_focus_session(request, profile)
        messages.success(request, f"Focus session started for {session.target_minutes} minutes.")
        return redirect("study_home")

    foundation = study.ensure_learning_foundation(profile) if profile else {}
    events = MoveEvent.objects.filter(profile=profile) if profile else MoveEvent.objects.none()
    last_move = events.filter(success=True).order_by("-timestamp").first()
    context = {
        "profile": profile,
        "study_context": ctx,
        "foundation": foundation,
        "subject_memories": SubjectMemory.objects.filter(profile=profile) if profile and ctx.show_subject_memory else [],
        "subject_themes": SubjectTheme.objects.filter(profile=profile)[:12] if profile and ctx.show_subject_memory else [],
        "review_items": ReviewItem.objects.filter(profile=profile, status="queued")[:8] if profile and ctx.show_review_queue else [],
        "digests": LearningDigest.objects.filter(profile=profile)[:4] if profile else [],
        "activities": LearningActivity.objects.filter(profile=profile)[:12] if profile else [],
        "inbox_items": SortDecision.objects.filter(profile=profile, status="pending")[:8] if profile else [],
        "connections": IntegrationConnection.objects.filter(profile=profile) if profile and ctx.show_integrations else [],
        "export_bundles": ExportBundle.objects.filter(profile=profile)[:4] if profile else [],
        "folder_rules": FolderRule.objects.filter(profile=profile)[:8] if profile else [],
        "import_plans": FolderImportPlan.objects.filter(profile=profile)[:4] if profile else [],
        "resource_recommendations": ResourceRecommendation.objects.filter(profile=profile).exclude(status="dismissed")[:6] if profile else [],
        "learning_routes": LearningRoute.objects.filter(profile=profile).exclude(status="done")[:4] if profile else [],
        "study_goals": StudyGoal.objects.filter(profile=profile)[:4] if profile and ctx.show_goal_tracking else [],
        "unread_notification_count": Notification.objects.filter(profile=profile, read_at__isnull=True).count() if profile else 0,
        "muele_connected": IntegrationConnection.objects.filter(profile=profile, provider="muele", status="connected").exists() if profile else False,
        "timetable_connected": IntegrationConnection.objects.filter(profile=profile, provider="mak_timetable").exists() if profile else False,
    }
    context.update(_cockpit_context(request, profile, events, last_move))
    return render(request, "organizer/study.html", context)


def muele_connection(request):
    return redirect("muele_connect")




def course_guide_generate(request, profile_pk, code):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)

    active = Profile.get_active()
    if not active or active.pk != profile_pk:
        return JsonResponse({"error": "Activate this profile first."}, status=404)

    profile = active
    config = getattr(profile, "config", None)
    level = f"{config.primary_value} {config.secondary_value}".strip() if config else ""

    content, error = summarize_core.generate_course_guide(code, program=profile.name, level=level, log=write_log)
    if error:
        return JsonResponse({"error": error}, status=400)

    CourseGuide.objects.update_or_create(profile=profile, course_code=code, defaults={"content": content})
    return JsonResponse({"ok": True})


def course_guide_view(request, profile_pk, code):
    active = Profile.get_active()
    if not active or active.pk != profile_pk:
        return JsonResponse({"error": "Activate this profile first."}, status=404)

    profile = active
    guide = CourseGuide.objects.filter(profile=profile, course_code=code).first()
    if guide is None:
        return JsonResponse({"error": "No guide yet. Generate one first."}, status=404)

    return JsonResponse({
        "course_code": code,
        "html": summarize_core.render_html(guide.content),
        "created_at": guide.created_at.strftime("%Y-%m-%d %H:%M"),
    })


def review_queue(request):
    """Smart review queue with spaced repetition scheduling and user workflow."""
    from django.utils import timezone

    from ..core.contexts import get_context_for_profile
    from ..core import review as review_core

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "auto_schedule":
            count = review_core.auto_schedule_reviews(profile)
            if count:
                messages.success(request, f"Scheduled {count} new review item(s).")
            else:
                messages.info(request, "No new items to schedule.")
            return redirect("review_queue")
        if action == "skip_all_due":
            due_items = review_core.get_due_reviews(profile, limit=1000)
            count = len(due_items)
            for item in due_items:
                review_core.skip_review(item)
            messages.success(request, f"Skipped {count} due review item(s).")
            return redirect("review_queue")

    stats = review_core.get_stats(profile)
    due_items = review_core.get_due_reviews(profile, limit=20)
    recent_done = ReviewItem.objects.filter(profile=profile, status="done").order_by("-completed_at")[:5]
    overdue_count = review_core.reschedule_overdue(profile)

    return render(request, "organizer/review_queue.html", {
        "profile": profile,
        "study_context": ctx,
        "stats": stats,
        "due_items": due_items,
        "recent_done": recent_done,
        "now": timezone.now(),
    })


def review_mark_done(request, pk):
    """Mark a review item as completed."""
    from ..core import review as review_core

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    item = get_object_or_404(ReviewItem, pk=pk, profile=profile)
    if request.method == "POST":
        review_core.mark_review_done(item)
        messages.success(request, f"'{item.title}' marked as done.")
    return redirect("review_queue")


def review_skip(request, pk):
    """Skip a review item."""
    from ..core import review as review_core

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    item = get_object_or_404(ReviewItem, pk=pk, profile=profile)
    if request.method == "POST":
        review_core.skip_review(item)
        messages.info(request, f"'{item.title}' skipped.")
    return redirect("review_queue")


@perf.measure_view
def timeline_view(request):
    """Rich visual timeline with filtering by type and time range."""
    from datetime import timedelta

    from django.utils import timezone

    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    # Parse filters
    days = request.GET.get("days", "30")
    try:
        days = int(days)
    except (ValueError, TypeError):
        days = 30
    filter_type = request.GET.get("type", "")

    def _compute_timeline_groups():
        since = timezone.now() - timedelta(days=days)

        # Load activities and MoveEvents for this profile
        activities = LearningActivity.objects.filter(
            profile=profile, happened_at__gte=since
        ).order_by("-happened_at")[:200]

        if filter_type:
            activities = activities.filter(activity_type=filter_type)

        # Build timeline event dicts
        icon_map = {
            "file_sorted": "F",
            "summary_created": "S",
            "review_scheduled": "R",
            "digest_created": "D",
            "muele_sync": "M",
            "manual_note": "N",
        }
        marker_map = {
            "file_sorted": "file",
            "summary_created": "summary",
            "review_scheduled": "review",
            "digest_created": "digest",
            "muele_sync": "muele",
            "manual_note": "note",
        }
        type_labels = dict(LearningActivity.ACTIVITY_CHOICES)

        events = []
        for act in activities:
            events.append({
                "title": act.title,
                "icon": icon_map.get(act.activity_type, "N"),
                "marker_class": marker_map.get(act.activity_type, "note"),
                "type_label": type_labels.get(act.activity_type, act.activity_type),
                "subject_code": act.subject_code or "",
                "details": act.details[:120] if act.details else "",
                "time": act.happened_at.strftime("%Y-%m-%d %H:%M"),
                "timestamp": act.happened_at,
            })

        # Group by date
        from itertools import groupby

        def date_key(e):
            return e["timestamp"].strftime("%A, %B %d, %Y")

        events.sort(key=lambda e: e["timestamp"], reverse=True)
        groups = []
        for day_label, group in groupby(events, key=date_key):
            groups.append((day_label, list(group)))
        return groups

    from ..core import resource_cache

    timeline_groups = resource_cache.get_or_set_timeline(
        profile.pk, days, filter_type, _compute_timeline_groups,
    )

    day_options = [7, 30, 90, 9999]

    return render(request, "organizer/timeline.html", {
        "profile": profile,
        "study_context": ctx,
        "timeline_groups": timeline_groups,
        "activity_types": LearningActivity.ACTIVITY_CHOICES,
        "current_filter": filter_type,
        "days": str(days),
        "day_options": day_options,
    })


def subject_dashboard(request):
    """Semantic subject dashboard showing themes, activity, and learning
    context across all subjects."""
    from django.utils import timezone

    from ..core import topics as topics_core
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    all_memories = SubjectMemory.objects.filter(profile=profile).order_by("code")

    if request.method == "POST" and request.POST.get("action") == "refresh_topics":
        subject_code = request.POST.get("subject_code", "") or None
        results = topics_core.process_all_subjects(profile) if not subject_code else {subject_code: len(topics_core.process_subject_topics(profile, subject_code))}
        total = sum(results.values())
        messages.success(request, f"Topics refreshed: {total} themes extracted across {len(results)} subjects.")
        return redirect("subject_dashboard")

    # Build dashboard data for each subject
    subject_data = []
    for memory in all_memories:
        themes = SubjectTheme.objects.filter(
            profile=profile, subject_code=memory.code
        ).order_by("-weight")[:10]

        # Recent activity for this subject
        recent_activity = LearningActivity.objects.filter(
            profile=profile, subject_code=memory.code
        ).order_by("-happened_at")[:3]

        subject_data.append({
            "code": memory.code,
            "title": memory.title or memory.code,
            "resource_count": memory.resource_count,
            "last_touched_at": memory.last_touched_at,
            "themes": [
                {
                    "name": t.name,
                    "weight": t.weight,
                    "source": t.source,
                    "evidence_count": len(t.evidence or []),
                }
                for t in themes
            ],
            "top_theme": themes[0].name if themes else "",
            "theme_count": len(themes),
            "recent_activity": [
                {
                    "title": a.title,
                    "time": a.happened_at.strftime("%Y-%m-%d %H:%M"),
                }
                for a in recent_activity
            ],
        })

    # Global topic cloud: most prominent themes across all subjects
    global_themes = SubjectTheme.objects.filter(profile=profile).order_by("-weight")[:30]

    return render(request, "organizer/subject_dashboard.html", {
        "profile": profile,
        "study_context": ctx,
        "subject_data": subject_data,
        "global_themes": [
            {
                "name": t.name,
                "weight": t.weight,
                "subject_code": t.subject_code,
            }
            for t in global_themes
        ],
        "total_subjects": all_memories.count(),
        "total_themes": SubjectTheme.objects.filter(profile=profile).count(),
    })




def _parse_checklist_text(raw: str) -> list:
    """One checklist item per line. A leading "x " or "[x] " marks it done
    -- lets an edit round-trip through the same plain-text box the create
    form uses, no separate per-item form fields."""
    items = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        done = False
        lowered = line.lower()
        if lowered.startswith("[x]"):
            done = True
            line = line[3:].strip()
        elif lowered.startswith("x "):
            done = True
            line = line[2:].strip()
        if line:
            items.append({"text": line[:200], "done": done})
    return items


def assignment_tracker(request):
    """Every assignment for the active profile, grouped by status, with
    evidence and a checklist per item -- the "deadlines impossible to
    ignore" tracker."""
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        title = request.POST.get("title", "").strip()
        if not title:
            messages.error(request, "Enter a title for the assignment.")
            return redirect("assignment_tracker")

        due_at = None
        due_raw = request.POST.get("due_at", "").strip()
        if due_raw:
            parsed = parse_datetime(due_raw)
            due_at = parsed if parsed else None

        AssignmentItem.objects.create(
            profile=profile,
            subject_code=request.POST.get("subject_code", "").strip()[:32],
            title=title[:180],
            due_at=due_at,
            notes=request.POST.get("notes", ""),
            checklist=_parse_checklist_text(request.POST.get("checklist_text", "")),
            source="manual",
        )
        messages.success(request, f"Added: {title}")
        return redirect("assignment_tracker")

    items = AssignmentItem.objects.filter(profile=profile).order_by("status", "due_at", "-created_at")
    grouped = {"open": [], "submitted": [], "missed": [], "archived": []}
    for item in items:
        grouped.setdefault(item.status, []).append(item)

    return render(request, "organizer/assignment_tracker.html", {
        "profile": profile,
        "study_context": ctx,
        "grouped": grouped,
        "status_labels": AssignmentItem.STATUS_CHOICES,
        "total_count": items.count(),
        "open_count": len(grouped["open"]),
        "now": timezone.now(),
    })


def assignment_tracker_item_update(request, pk):
    """Edit an assignment: status, subject, due date, notes, evidence path,
    checklist (as plain text, one item per line), or toggle a single
    checklist item done/undone from the list view."""
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    item = get_object_or_404(AssignmentItem, pk=pk, profile=profile)
    if request.method != "POST":
        return redirect("assignment_tracker")

    action = request.POST.get("action", "update")

    if action == "toggle_checklist_item":
        index = int(request.POST.get("index", -1))
        checklist = item.checklist or []
        if 0 <= index < len(checklist):
            checklist[index]["done"] = not checklist[index].get("done")
            item.checklist = checklist
            item.save(update_fields=["checklist"])
        return redirect("assignment_tracker")

    if action == "mark_submitted":
        item.status = "submitted"
        item.save(update_fields=["status"])
        messages.success(request, f"Marked submitted: {item.title}")
        return redirect("assignment_tracker")

    # Full edit
    item.title = request.POST.get("title", item.title).strip()[:180] or item.title
    item.subject_code = request.POST.get("subject_code", item.subject_code).strip()[:32]
    item.status = request.POST.get("status", item.status)
    item.notes = request.POST.get("notes", item.notes)
    item.evidence_path = request.POST.get("evidence_path", item.evidence_path).strip()
    item.checklist = _parse_checklist_text(request.POST.get("checklist_text", ""))
    due_raw = request.POST.get("due_at", "").strip()
    if due_raw:
        parsed = parse_datetime(due_raw)
        if parsed:
            item.due_at = parsed
    elif "due_at" in request.POST:
        item.due_at = None
    item.save()
    messages.success(request, f"Updated: {item.title}")
    return redirect("assignment_tracker")


def assignment_tracker_item_delete(request, pk):
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    item = get_object_or_404(AssignmentItem, pk=pk, profile=profile)
    if request.method == "POST":
        title = item.title
        item.delete()
        messages.success(request, f"Deleted: {title}")
    return redirect("assignment_tracker")


def assignment_tracker_archive_missed(request):
    """Bulk-archives every assignment currently sitting in the Missed
    bucket -- keeps the history (unlike a delete) while clearing the
    tracker's most stressful-looking column in one step."""
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    count = AssignmentItem.objects.filter(profile=profile, status="missed").update(status="archived")
    messages.success(request, f"Archived {count} missed assignment(s).")
    return redirect("assignment_tracker")


def exam_countdown(request):
    """Exam Countdown Mode: days left per paper, a revision-coverage proxy
    (queued vs done ReviewItems for that subject -- not a true syllabus
    percentage, nothing here claims to know the real syllabus), and one
    "what to study today" callout. Built entirely from TimetableEntry rows
    timetable_sync.py already syncs and the local ReviewItem queue -- no
    new data source."""
    from datetime import date

    from ..core import muele_calendar
    from ..core import rules as routing_rules
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)
    today = date.today()

    exam_rows = TimetableEntry.objects.filter(
        profile=profile, kind__in=["test", "examination"],
    ).order_by("specific_date", "weekday")

    dated = []
    undated = []
    for entry in exam_rows:
        if entry.specific_date is None:
            undated.append(entry)
        elif entry.specific_date >= today:
            dated.append(entry)
        # Past exams (specific_date < today) aren't shown -- nothing left to count down to.

    exams = []
    for entry in dated:
        code = entry.course_code
        reviews_done = 0
        reviews_queued = 0
        open_assignments = 0
        past_papers_available = 0
        if code:
            reviews_done = ReviewItem.objects.filter(profile=profile, subject_code=code, status="done").count()
            reviews_queued = ReviewItem.objects.filter(profile=profile, subject_code=code, status="queued").count()
            open_assignments = AssignmentItem.objects.filter(profile=profile, subject_code=code, status="open").count()
            resource_paths = MoveEvent.objects.filter(
                profile=profile, course_code=code, success=True
            ).values_list("destination_path", flat=True)
            past_papers_available = sum(
                1 for path in resource_paths
                if routing_rules.category_from_path(path) == "03 Past Papers and Tests"
            )
        reviews_total = reviews_done + reviews_queued
        days_left = (entry.specific_date - today).days
        exams.append({
            "entry": entry,
            "days_left": days_left,
            "coverage_percent": int((reviews_done / reviews_total) * 100) if reviews_total else 0,
            "reviews_done": reviews_done,
            "reviews_total": reviews_total,
            "open_assignments": open_assignments,
            "past_papers_available": past_papers_available,
            "urgency": "critical" if days_left <= 3 else "warning" if days_left <= 14 else "ok",
        })

    next_best = None
    if exams:
        nearest = exams[0]
        code = nearest["entry"].course_code
        top_review = (
            ReviewItem.objects.filter(profile=profile, subject_code=code, status="queued").order_by("due_at").first()
            if code else None
        )
        label = code or "this subject"
        if top_review:
            next_best = f"Study {label} today: {top_review.title} -- exam in {nearest['days_left']} day(s)."
        else:
            next_best = f"{label}'s exam is in {nearest['days_left']} day(s) with no queued reviews yet -- visit Subject Memory to schedule some."

    return render(request, "organizer/exam_countdown.html", {
        "profile": profile,
        "study_context": ctx,
        "exams": exams,
        "undated": undated,
        "next_best": next_best,
        "is_exam_period": muele_calendar.is_exam_period(today),
        "remaining_weeks": muele_calendar.get_remaining_weeks(today),
        "upcoming_periods": muele_calendar.get_upcoming_periods(today),
    })




def first_run_checklist(request):
    """First-run onboarding page with setup checklist."""
    from ..core.contexts import get_context_for_profile, get_context

    profile = Profile.get_active()
    settings = AppSettings.get_solo() if AppSettings.objects.exists() else None
    ctx = get_context_for_profile(profile) if profile else get_context("custom")

    # Build checklist items
    checklist = []

    # 1. Profile configured
    if profile:
        checklist.append({
            "id": "profile",
            "label": "Profile configured",
            "detail": f"'{profile.name}', {profile.purpose} context",
            "done": True,
            "url": reverse("profile_edit", args=[profile.pk]),
        })
    else:
        checklist.append({
            "id": "profile",
            "label": "Create a profile",
            "detail": "Choose Makerere or Manual setup path",
            "done": False,
            "url": reverse("start"),
        })

    # 2. Downloads folder set
    if settings and settings.downloads_path:
        from pathlib import Path
        path = Path(settings.downloads_path)
        checklist.append({
            "id": "downloads",
            "label": "Downloads folder configured",
            "detail": str(path),
            "done": path.exists(),
            "url": reverse("settings_edit"),
            "warning": not path.exists(),
        })
    else:
        checklist.append({
            "id": "downloads",
            "label": "Set downloads folder",
            "detail": "Orch needs to know where to watch for files",
            "done": False,
            "url": reverse("settings_edit"),
        })

    # 3. Profile has subjects
    config = getattr(profile, "config", None) if profile else None
    has_subjects = bool(config and config.groups)
    checklist.append({
        "id": "subjects",
        "label": "Subjects added",
        "detail": f"{len(config.groups) if config and config.groups else 0} subjects configured" if has_subjects else "Add subjects so Orch knows where to route files",
        "done": has_subjects,
        "url": reverse("profile_edit", args=[profile.pk]) if profile else reverse("start"),
    })

    # 4. Watcher running
    checklist.append({
        "id": "watcher",
        "label": "File watcher active",
        "detail": "Watches for new files in Downloads folder",
        "done": True,
        "url": None,
        "info": "Enabled by default in system tray",
    })

    # 5. Writing help configured (optional)
    from ..core import ai_classify
    active_ai_config = ai_classify.load_ai_config() or {}
    ai_configured = bool(active_ai_config.get("enabled") and active_ai_config.get("api_key"))
    checklist.append({
        "id": "ai",
        "label": "Writing help",
        "detail": "Helps with summaries, course guides, and folder suggestions" if ai_configured else "Optional. Turn it on from Settings for summaries and folder suggestions",
        "done": ai_configured,
        "url": reverse("settings_edit"),
        "optional": True,
    })

    # 6. MUELE connected (if Makerere profile)
    if profile and profile.setup_path == "makerere":
        muele = IntegrationConnection.objects.filter(profile=profile, provider="muele").first()
        muele_ready = bool(muele and (muele.status == "connected" or muele.username or muele.last_sync_at))
        checklist.append({
            "id": "muele",
            "label": "MUELE connected",
            "detail": "MUELE details saved" if muele_ready else "Connect MUELE for auto-sync",
            "done": muele_ready,
            "url": reverse("muele_connect"),
        })

    # 7. Owner console (only relevant once owner mode has been opted into via
    # orch-owner.json or ORCH_OWNER_MODE -- most installs never enable this,
    # so it stays out of the checklist entirely rather than nagging every
    # user about a troubleshooting-only feature they'll never use).
    if owner_access.owner_mode_enabled():
        User = get_user_model()
        has_owner = User.objects.filter(is_staff=True).exists()
        checklist.append({
            "id": "owner",
            "label": "Owner account created",
            "detail": "Create the admin login at /owner/setup/" if not has_owner else "Admin console ready at /owner/",
            "done": has_owner,
            "url": reverse("owner_console"),
        })

    done_count = sum(1 for c in checklist if c.get("done"))
    total_count = sum(1 for c in checklist if not c.get("optional"))
    required_items = [c for c in checklist if not c.get("optional")]
    setup_complete = bool(required_items) and all(c["done"] for c in required_items)

    return render(request, "organizer/first_run.html", {
        "profile": profile,
        "study_context": ctx,
        "checklist": checklist,
        "done_count": done_count,
        "total_count": total_count,
        "setup_complete": setup_complete,
        "watched_folder": settings.downloads_path if settings else "",
    })


def open_watched_folder(request):
    """Opens the active Downloads watch folder in File Explorer -- the
    first-run success screen's "try it" action, so a new user can drop a
    real file in and see it get sorted for real rather than a simulated
    demo."""
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    settings = AppSettings.get_solo() if AppSettings.objects.exists() else None
    folder = settings.downloads_path if settings else ""
    if not folder or not Path(folder).is_dir():
        messages.error(request, "That folder doesn't exist yet. Set it up in Settings first.")
        return redirect("first_run")

    import os

    try:
        os.startfile(folder)
        messages.success(request, "Opened your watched folder. Drop a file in to see Orch sort it.")
    except OSError:
        messages.error(request, "Couldn't open that folder.")
    return redirect("first_run")


def notifications_view(request):
    """History of everything Orch has alerted about: deadline warnings,
    MUELE sync results, and anything else raised through
    organizer.core.notifications. Backs up the tray's toast popups, which
    are easy to miss and don't persist anywhere on their own."""
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    ctx = get_context_for_profile(profile) if profile else None

    if request.method == "POST" and request.POST.get("action") == "mark_all_read":
        from django.utils import timezone
        Notification.objects.filter(profile=profile, read_at__isnull=True).update(read_at=timezone.now())
        messages.success(request, "All notifications marked as read.")
        return redirect("notifications")

    if request.method == "POST" and request.POST.get("action") == "clear_all":
        deleted, _ = Notification.objects.filter(profile=profile).delete()
        messages.success(request, f"Cleared {deleted} notification(s).")
        return redirect("notifications")

    items = Notification.objects.filter(profile=profile).order_by("-created_at")[:100]
    unread_count = Notification.objects.filter(profile=profile, read_at__isnull=True).count()

    return render(request, "organizer/notifications.html", {
        "profile": profile,
        "study_context": ctx,
        "notifications": items,
        "unread_count": unread_count,
    })


def notification_clear(request, pk):
    """Remove one notification from the list."""
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    item = get_object_or_404(Notification, pk=pk, profile=profile)
    item.delete()
    messages.success(request, "Notification cleared.")
    return redirect("notifications")


def support_message(request):
    """Receives the "Contact support" popup's message, from any page. Saves
    it first and emails it best-effort -- see organizer.core.support."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    subject = request.POST.get("subject", "").strip()
    message = request.POST.get("message", "").strip()
    if not subject:
        return JsonResponse({"ok": False, "error": "Give it a subject before sending."}, status=400)
    if not message:
        return JsonResponse({"ok": False, "error": "Write a message before sending."}, status=400)

    from ..core import support as support_core

    app_state = None
    if request.POST.get("include_diagnostics") == "1":
        include_raw_details = request.POST.get("include_raw_details") == "1"
        app_state = support_core.build_app_state_snapshot(
            Profile.get_active(), include_raw_details=include_raw_details,
        )

    # Any SMTP/config failure lands on the saved SupportMessage row for the
    # admin to see (email_error, visible in the owner console) -- the
    # person submitting the form always just sees a clean confirmation,
    # since the message itself is never lost regardless of email delivery.
    support_core.submit_support_message(
        name=request.POST.get("name", ""),
        email=request.POST.get("email", ""),
        subject=subject,
        message=message,
        page_url=request.POST.get("page_url", ""),
        app_state=app_state,
    )
    return JsonResponse({"ok": True})




def learning_routes(request):
    """Learning Route page: turn weak areas into a sequenced study path."""
    from ..core import learning_route as route_engine
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "generate":
            subject_code = request.POST.get("subject_code", "").strip() or None
            theme = request.POST.get("theme", "").strip() or None
            route = route_engine.create_or_refresh_route(profile, subject_code=subject_code, theme=theme)
            if route:
                messages.success(request, f"Learning route ready: {route.title}")
            else:
                messages.error(request, "Add a subject with a weak area or focus topic first.")
            return redirect("learning_routes")

        if action == "step_done":
            route = get_object_or_404(LearningRoute, pk=request.POST.get("route_pk"), profile=profile)
            step_index = int(request.POST.get("step_index", 0))
            try:
                route_engine.mark_step_done(route, step_index)
            except IndexError:
                messages.error(request, "That step no longer exists on this route.")
            return redirect("learning_routes")

    subject_memories = SubjectMemory.objects.filter(profile=profile).order_by("code")
    routes = LearningRoute.objects.filter(profile=profile).order_by("status", "subject_code", "-updated_at")

    return render(request, "organizer/learning_routes.html", {
        "profile": profile,
        "study_context": ctx,
        "subject_memories": subject_memories,
        "routes": routes,
    })


def learning_route(request):
    return learning_routes(request)


# Course notes: the same five get_content_category() folder labels
# every document already lands in, relabeled for a student-facing bucket
# heading. "Other" catches anything routed outside the default document
# pipeline (a custom FolderRule destination, media, etc.) so no resource
# silently disappears from the count.
_COURSE_NOTES_BUCKET_LABELS = {
    "01 Lecture Notes and Slides": "Lecture Notes",
    "02 Assignments and Coursework": "Assignments & Coursework",
    "03 Past Papers and Tests": "Past Papers & Tests",
    "04 Reports and Projects": "Reports & Projects",
    "05 Reference and Extra Reading": "Reference",
}


def _bucket_resources_by_category(resources):
    from ..core import rules as routing_rules

    buckets = {label: [] for label in _COURSE_NOTES_BUCKET_LABELS.values()}
    buckets["Other"] = []
    for event in resources:
        category = routing_rules.category_from_path(event.destination_path)
        label = _COURSE_NOTES_BUCKET_LABELS.get(category, "Other")
        buckets[label].append(event)
    return buckets


def subject_memory_detail(request, code):
    """Full subject memory detail page with resources, themes, assignments, reviews."""
    from django.utils import timezone

    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    memory = get_object_or_404(SubjectMemory, profile=profile, code=code)
    themes = SubjectTheme.objects.filter(profile=profile, subject_code=code).order_by("-weight")[:20]
    assignments = AssignmentItem.objects.filter(profile=profile, subject_code=code).order_by("due_at")
    reviews_qs = ReviewItem.objects.filter(profile=profile, subject_code=code, status="queued").order_by("due_at")
    reviews = reviews_qs[:10]
    reviews_queued_count = reviews_qs.count()
    reviews_done_count = ReviewItem.objects.filter(profile=profile, subject_code=code, status="done").count()
    resources = MoveEvent.objects.filter(profile=profile, course_code=code, success=True).order_by("-timestamp")[:50]
    activities = LearningActivity.objects.filter(profile=profile, subject_code=code).order_by("-happened_at")[:20]
    guide = CourseGuide.objects.filter(profile=profile, course_code=code).first()

    resource_buckets = _bucket_resources_by_category(resources)

    return render(request, "organizer/subject_memory.html", {
        "profile": profile,
        "study_context": ctx,
        "memory": memory,
        "themes": themes,
        "top_themes": themes[:5],
        "assignments": assignments,
        "open_assignments_count": assignments.filter(status="open").count(),
        "reviews": reviews,
        "reviews_count": reviews_queued_count,
        "reviews_queued_count": reviews_queued_count,
        "reviews_done_count": reviews_done_count,
        "resources": resources,
        "resource_buckets": resource_buckets,
        "lecture_notes_count": len(resource_buckets["Lecture Notes"]),
        "past_papers_count": len(resource_buckets["Past Papers & Tests"]),
        "weak_topics_count": len(memory.weak_areas or []),
        "activities": activities,
        "guide": guide,
        "now": timezone.now(),
    })


def past_paper_analysis(request, code):
    """Past paper review: local, free topic-frequency analysis over
    a subject's own past papers -- no upload flow, just the files the
    sorter already routed into that subject's "03 Past Papers and Tests"
    folder."""
    from ..core import past_papers as past_papers_core
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        analysis, skipped = past_papers_core.analyze_subject(profile, code, log=write_log)
        if analysis is None:
            if skipped:
                messages.error(request, f"Couldn't read any of the {skipped} past paper(s) found for {code}.")
            else:
                messages.error(request, f"No past papers found yet for {code}. Sort some into its Past Papers folder first.")
        else:
            summary = f"Analyzed {analysis.paper_count} past paper(s) for {code}."
            if skipped:
                summary += f" Skipped {skipped} unreadable file(s)."
            messages.success(request, summary)
        return redirect("past_paper_analysis", code=code)

    analysis = PastPaperAnalysis.objects.filter(profile=profile, subject_code=code).first()
    resources = MoveEvent.objects.filter(profile=profile, course_code=code, success=True).order_by("-timestamp")
    past_paper_files = [
        event for event in resources
        if rules.category_from_path(event.destination_path) == "03 Past Papers and Tests"
    ]

    return render(request, "organizer/past_paper_analysis.html", {
        "profile": profile,
        "study_context": ctx,
        "code": code,
        "analysis": analysis,
        "past_paper_files": past_paper_files,
        "total_marks": sum((analysis.marks_by_topic or {}).values()) if analysis else 0,
    })


def weakness_radar(request):
    """Weak areas: a read-only, cross-subject view built entirely from
    patterns other features already produce (SubjectMemory.weak_areas,
    last_touched_at, and the review queue) -- no new producer here."""
    from ..core import weakness_radar as weakness_radar_core
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)
    radar = weakness_radar_core.build_radar(profile)

    return render(request, "organizer/weakness_radar.html", {
        "profile": profile,
        "study_context": ctx,
        **radar,
    })


def grade_target_planner(request):
    """Grade Target Planner: what's needed in the exam to reach a target
    overall percentage, per subject."""
    from ..core import grade_planner
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        subject_code = request.POST.get("subject_code", "").strip()[:32]
        if not subject_code:
            messages.error(request, "Enter a subject code.")
            return redirect("grade_target_planner")

        def _int_field(name, default):
            raw = request.POST.get(name, "").strip()
            try:
                return max(0, min(100, int(raw)))
            except ValueError:
                return default

        def _optional_int_field(name):
            raw = request.POST.get(name, "").strip()
            if not raw:
                return None
            try:
                return max(0, min(100, int(raw)))
            except ValueError:
                return None

        GradeTarget.objects.update_or_create(
            profile=profile,
            subject_code=subject_code,
            defaults={
                "coursework_weight": _int_field("coursework_weight", 30),
                "coursework_score": _optional_int_field("coursework_score"),
                "test_weight": _int_field("test_weight", 0),
                "test_score": _optional_int_field("test_score"),
                "exam_weight": _int_field("exam_weight", 70),
                "target_percent": _int_field("target_percent", 70),
            },
        )
        messages.success(request, f"Saved grade target for {subject_code}.")
        return redirect("grade_target_planner")

    targets = []
    for target in GradeTarget.objects.filter(profile=profile):
        result = grade_planner.required_exam_score(
            coursework_weight=target.coursework_weight,
            coursework_score=target.coursework_score,
            test_weight=target.test_weight,
            test_score=target.test_score,
            exam_weight=target.exam_weight,
            target_percent=target.target_percent,
        )
        targets.append({"target": target, "result": result})

    return render(request, "organizer/grade_target_planner.html", {
        "profile": profile,
        "study_context": ctx,
        "targets": targets,
    })


def grade_target_delete(request, pk):
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    target = get_object_or_404(GradeTarget, pk=pk, profile=profile)
    if request.method == "POST":
        subject_code = target.subject_code
        target.delete()
        messages.success(request, f"Deleted grade target for {subject_code}.")
    return redirect("grade_target_planner")


def flashcards(request):
    """Active Recall Builder: practice queue + generation + management, all
    in one view, same single-view action-dispatch pattern as
    assignment_tracker."""
    from ..core import flashcards as flashcards_core
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        action = request.POST.get("action", "")
        subject_code = request.POST.get("subject_code", "").strip()[:32]

        if action == "generate_past_papers":
            if not subject_code:
                messages.error(request, "Choose a subject.")
            else:
                created = flashcards_core.generate_from_past_papers(profile, subject_code)
                messages.success(request, f"Created {created} new card(s) from past papers for {subject_code}.")
        elif action == "generate_summaries":
            if not subject_code:
                messages.error(request, "Choose a subject.")
            else:
                created = flashcards_core.generate_from_summaries(profile, subject_code)
                messages.success(request, f"Created {created} new card(s) from notes for {subject_code}.")
        elif action == "create_manual":
            front = request.POST.get("front", "").strip()
            if not front:
                messages.error(request, "Enter a prompt for the card.")
            else:
                Flashcard.objects.create(
                    profile=profile,
                    subject_code=subject_code,
                    card_type="manual",
                    front=front[:500],
                    back=request.POST.get("back", "").strip(),
                )
                messages.success(request, "Card added.")
        elif action == "delete_subject_cards":
            # The template groups blank subject_code cards under the label
            # "Unassigned" (see cards_by_subject below); that label isn't a
            # real stored value, so map it back to the actual blank code.
            lookup_code = "" if subject_code == "Unassigned" else subject_code
            deleted, _ = Flashcard.objects.filter(profile=profile, subject_code=lookup_code).delete()
            messages.success(request, f"Deleted {deleted} card(s) for {subject_code or 'Unassigned'}.")
        return redirect("flashcards")

    due_count = Flashcard.objects.filter(profile=profile, status="active", due_at__lte=timezone.now()).count()
    next_card = flashcards_core.get_due_cards(profile).first()

    all_cards = Flashcard.objects.filter(profile=profile, status="active").order_by("subject_code", "-created_at")
    cards_by_subject: dict = {}
    for card in all_cards:
        cards_by_subject.setdefault(card.subject_code or "Unassigned", []).append(card)

    subjects_with_past_papers = set(
        PastPaperAnalysis.objects.filter(profile=profile).values_list("subject_code", flat=True)
    )
    subjects_with_summaries = set(
        FileSummary.objects.filter(move_event__profile=profile)
        .exclude(move_event__course_code="").exclude(move_event__course_code__isnull=True)
        .values_list("move_event__course_code", flat=True)
    )
    generation_sources = sorted(subjects_with_past_papers | subjects_with_summaries)

    return render(request, "organizer/flashcards.html", {
        "profile": profile,
        "study_context": ctx,
        "next_card": next_card,
        "due_count": due_count,
        "total_active": all_cards.count(),
        "cards_by_subject": cards_by_subject,
        "generation_sources": generation_sources,
        "subjects_with_past_papers": subjects_with_past_papers,
        "subjects_with_summaries": subjects_with_summaries,
    })


def flashcard_grade(request, pk):
    from ..core import flashcards as flashcards_core

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    card = get_object_or_404(Flashcard, pk=pk, profile=profile)
    if request.method == "POST":
        remembered = request.POST.get("remembered") == "1"
        flashcards_core.grade_flashcard(card, remembered)
    return redirect("flashcards")


def flashcard_delete(request, pk):
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    card = get_object_or_404(Flashcard, pk=pk, profile=profile)
    if request.method == "POST":
        card.delete()
        messages.success(request, "Card deleted.")
    return redirect("flashcards")


def war_room(request):
    """The focus plan: a single screen synthesizing every study
    pattern Orch already tracks -- pure aggregation, no new data."""
    from ..core import war_room as war_room_core
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)
    data = war_room_core.build_war_room(profile)

    return render(request, "organizer/war_room.html", {
        "profile": profile,
        "study_context": ctx,
        **data,
    })


def study_timetable(request):
    """Smart Study Timetable: a priority engine, not a calendar --
    regenerated fresh on every load from assignments, exams, classes, weak
    areas, and past-paper questions already tracked elsewhere."""
    from ..core import study_timetable as study_timetable_core
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)
    blocks = study_timetable_core.generate_study_blocks(profile)
    grouped = study_timetable_core.group_by_day(blocks)

    return render(request, "organizer/study_timetable.html", {
        "profile": profile,
        "study_context": ctx,
        "grouped": grouped,
        "total_blocks": len(blocks),
    })


# ---------------------------------------------------------------------------
# Career tab
# ---------------------------------------------------------------------------



def course_guide_pdf(request, profile_pk, code):
    active = Profile.get_active()
    if not active or active.pk != profile_pk:
        return HttpResponse("Activate this profile first.", status=404)

    profile = active
    guide = CourseGuide.objects.filter(profile=profile, course_code=code).first()
    if guide is None:
        return HttpResponse("No guide yet.", status=404)

    pdf_bytes = summarize_core.render_pdf(code, guide.content)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    safe_name = re.sub(r"[^\w\-. ]", "_", code) or "course-guide"
    response["Content-Disposition"] = f'attachment; filename="{safe_name} guide.pdf"'
    return response


# ---------------------------------------------------------------------------
# MUELE Integration Views
# ---------------------------------------------------------------------------
