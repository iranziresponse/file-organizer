import json
import logging
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
from django.db import DatabaseError, connection
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

logger = logging.getLogger(__name__)


def move_history(request):
    """Show a fixed-size page of the active profile's file action history."""
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    from ..core.contexts import get_context_for_profile

    events = (
        MoveEvent.objects.filter(profile=profile)
        .select_related("sort_decision", "summary")
        .order_by("-timestamp", "-pk")
    )
    page_obj = Paginator(events, 50).get_page(request.GET.get("page", 1))
    return render(request, "organizer/move_history.html", {
        "profile": profile,
        "study_context": get_context_for_profile(profile),
        "page_obj": page_obj,
    })


def move_history_compact(request):
    """Reclaim free SQLite pages after history has been removed."""
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    if not Profile.get_active():
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    if connection.vendor != "sqlite":
        messages.error(request, "Database compaction is only available for SQLite.")
        return redirect("move_history")
    if connection.in_atomic_block:
        messages.error(
            request,
            "Database compaction could not start inside an active transaction. "
            "Please try again from the history page.",
        )
        return redirect("move_history")

    try:
        with connection.cursor() as cursor:
            cursor.execute("VACUUM")
    except DatabaseError:
        logger.exception("Could not compact the Orch database.")
        messages.error(
            request,
            "Orch could not compact the database. Your history is unchanged; "
            "close other Orch windows and try again.",
        )
    else:
        messages.success(request, "Database compacted. Free space is available again.")
    return redirect("move_history")


def export_bundles(request):
    """View and manage portable knowledge packs."""
    import os

    from django.http import FileResponse

    from ..core.contexts import get_context_for_profile
    from ..core import export as export_core

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "create":
            scope = request.POST.get("scope", "profile")
            subject_code = request.POST.get("subject_code", "").strip()
            title = request.POST.get("title", "").strip()
            result = export_core.create_knowledge_pack(
                profile,
                scope=scope,
                subject_code=subject_code if scope == "subject" else None,
                title=title or None,
            )
            if result["status"] == "ready":
                messages.success(request, f"Knowledge pack ready: {result['title']}")
            elif result["status"] == "failed":
                messages.error(request, f"Export failed: {result['manifest'].get('error', 'Unknown error')}")
            return redirect("export_bundles")

    bundles = ExportBundle.objects.filter(profile=profile).order_by("-created_at")
    subject_memories = SubjectMemory.objects.filter(profile=profile)

    return render(request, "organizer/export_bundles.html", {
        "profile": profile,
        "study_context": ctx,
        "bundles": bundles,
        "subject_memories": subject_memories,
    })


def _sanitize_filename(name: str) -> str:
    """Make a string safe for use as a filename."""
    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    return safe.strip() or "untitled"


def export_download(request, pk):
    """Download a knowledge pack ZIP file."""
    import os

    from django.http import FileResponse, Http404

    profile = Profile.get_active()
    bundle = get_object_or_404(ExportBundle, pk=pk, profile=profile)

    if bundle.status != "ready" or not bundle.output_path:
        messages.error(request, "This knowledge pack is not ready yet.")
        return redirect("export_bundles")

    zip_path = Path(bundle.output_path)
    if not zip_path.exists():
        messages.error(request, "The file no longer exists on disk.")
        return redirect("export_bundles")

    response = FileResponse(
        open(str(zip_path), "rb"),
        content_type="application/zip",
        as_attachment=True,
        filename=f"{_sanitize_filename(bundle.title)}.zip",
    )
    return response


def export_delete(request, pk):
    """Delete a knowledge pack."""
    import os

    profile = Profile.get_active()
    bundle = get_object_or_404(ExportBundle, pk=pk, profile=profile)
    if request.method == "POST":
        # Delete the ZIP file if it exists
        if bundle.output_path:
            try:
                Path(bundle.output_path).unlink(missing_ok=True)
            except OSError:
                pass
        bundle.delete()
        messages.success(request, f"Deleted '{bundle.title}'.")
    return redirect("export_bundles")




def move_relocate(request, pk):
    """Manually move an already-sorted file to a different folder, picked
    via the folder browser, from the dashboard's Recent activity table."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    profile = Profile.get_active()
    event = get_object_or_404(MoveEvent, Q(profile=profile) | Q(profile__isnull=True), pk=pk)
    new_destination = request.POST.get("new_destination", "").strip()
    if not new_destination:
        return JsonResponse({"ok": False, "error": "Pick a destination folder first."}, status=400)

    from ..core import destination_safety, sorting

    confirmed = request.POST.get("confirm_external") == "1"
    if not confirmed and not destination_safety.is_within_trusted_roots(new_destination, profile):
        return JsonResponse({
            "ok": False,
            "error": "That folder is outside your usual Orch folders. Check 'move it anyway' below and try again.",
            "needs_confirmation": True,
        }, status=400)

    moved = sorting.relocate_move_event(event, new_destination, log=write_log)
    if moved:
        return JsonResponse({"ok": True, "destination_path": event.destination_path})
    return JsonResponse({"ok": False, "error": "Could not move that file. Check the log for details."}, status=400)


def move_undo(request, pk):
    """Revert a single move back to its original location, from the
    dashboard's Recent Moves row actions. Same restore_move() the
    dedicated Undo page uses, just reachable per-row without navigating
    away."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    from ..core import undo

    profile = Profile.get_active()
    event = get_object_or_404(MoveEvent, Q(profile=profile) | Q(profile__isnull=True), pk=pk)
    restored = undo.restore_move(event, log=write_log)
    if restored:
        return JsonResponse({"ok": True})
    return JsonResponse({"ok": False, "error": "Could not undo that move. The file may no longer be where Orch left it."}, status=400)


def move_clear_history(request):
    """Delete the active profile's Recent Moves log. Undo stops being
    possible for anything cleared this way -- the confirm dialog in
    dashboard.html says so before this is ever reached."""
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    events = MoveEvent.objects.filter(profile=profile)
    event_count = events.count()
    events.delete()
    messages.success(
        request,
        f"Cleared {event_count} move record(s). Files were not changed; undo "
        "information and summaries for these moves were removed.",
    )
    destination = "move_history" if request.POST.get("return_to") == "history" else "dashboard"
    return redirect(destination)


def move_clear_one(request, pk):
    """Remove one Recent Moves row without touching the file itself."""
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    event = get_object_or_404(MoveEvent, profile=profile, pk=pk)
    filename = event.filename
    event.delete()
    messages.success(request, f"Cleared from Recent Moves: {filename}")
    return redirect("dashboard")


def move_summarize(request, pk):
    if request.method != "POST":
        return JsonResponse({"error": "POST required."}, status=405)

    profile = Profile.get_active()
    event = get_object_or_404(MoveEvent, Q(profile=profile) | Q(profile__isnull=True), pk=pk)
    content, error = summarize_core.generate_summary(event.destination_path, log=write_log)
    if error:
        return JsonResponse({"error": error}, status=400)

    FileSummary.objects.update_or_create(move_event=event, defaults={"content": content})
    return JsonResponse({"ok": True})


def move_summary_view(request, pk):
    profile = Profile.get_active()
    event = get_object_or_404(MoveEvent, Q(profile=profile) | Q(profile__isnull=True), pk=pk)
    summary = getattr(event, "summary", None)
    if summary is None:
        return JsonResponse({"error": "No summary yet. Generate one first."}, status=404)

    return JsonResponse({
        "filename": event.filename,
        "html": summarize_core.render_html(summary.content),
        "created_at": summary.created_at.strftime("%Y-%m-%d %H:%M"),
    })


def move_summary_pdf(request, pk):
    profile = Profile.get_active()
    event = get_object_or_404(MoveEvent, Q(profile=profile) | Q(profile__isnull=True), pk=pk)
    summary = getattr(event, "summary", None)
    if summary is None:
        return HttpResponse("No summary yet.", status=404)

    pdf_bytes = summarize_core.render_pdf(event.filename, summary.content)
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    safe_name = re.sub(r"[^\w\-. ]", "_", Path(event.filename).stem) or "summary"
    response["Content-Disposition"] = f'attachment; filename="{safe_name} summary.pdf"'
    return response




def sorting_inbox(request):
    """Decision inbox for approving/rerouting/ignoring files."""
    from ..core.contexts import get_context_for_profile
    from ..core import sorting

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)
    stats = sorting.get_inbox_stats(profile)
    pending_items = sorting.get_pending_inbox(profile)

    return render(request, "organizer/sorting_inbox.html", {
        "profile": profile,
        "study_context": ctx,
        "stats": stats,
        "pending_items": pending_items,
    })


def inbox_approve(request, pk):
    """Approve a pending inbox item. `remember=1` also teaches Orch an
    OrganizationMemoryRule from this approval ("Always do this")."""
    from ..core import sorting

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    item = get_object_or_404(SortDecision, pk=pk, profile=profile)
    if request.method == "POST":
        remember = request.POST.get("remember") == "1"
        if sorting.approve_inbox_item(item, remember=remember):
            if remember:
                messages.success(request, f"Approved and remembered: {item.filename}")
            else:
                messages.success(request, f"Approved: {item.filename}")
        else:
            messages.error(request, f"Could not approve {item.filename}")
    return redirect("sorting_inbox")


def inbox_reroute(request, pk):
    """Reroute a pending inbox item to a different destination the user
    picked, then approve it there."""
    from ..core import destination_safety, sorting

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    item = get_object_or_404(SortDecision, pk=pk, profile=profile)
    if request.method == "POST":
        new_dest = request.POST.get("new_destination", "").strip()
        confirmed = request.POST.get("confirm_external") == "1"
        if new_dest:
            if not confirmed and not destination_safety.is_within_trusted_roots(new_dest, profile):
                messages.error(
                    request,
                    "That folder is outside your usual Orch folders. "
                    "Check 'move it anyway' if you're sure, then try again.",
                )
                return redirect("sorting_inbox")
            if sorting.reroute_inbox_item(item, new_dest):
                messages.success(request, f"Moved to: {item.filename}")
            else:
                messages.error(request, f"Could not move {item.filename}")
    return redirect("sorting_inbox")


def inbox_ignore(request, pk):
    """Reject a pending inbox item, leaving the file in place. `never_again=1`
    also teaches Orch to stop suggesting this pattern."""
    from ..core import sorting

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    item = get_object_or_404(SortDecision, pk=pk, profile=profile)
    if request.method == "POST":
        never_again = request.POST.get("never_again") == "1"
        sorting.reject_inbox_item(item, never_again=never_again)
        if never_again:
            messages.info(request, f"Cleared and won't suggest again: {item.filename}")
        else:
            messages.info(request, f"Cleared: {item.filename}")
    return redirect("sorting_inbox")


def inbox_clear_all(request):
    """Clear every pending inbox item by marking it rejected in one step.
    Files stay exactly where they are."""
    if request.method != "POST":
        return HttpResponse("POST required.", status=405)

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    updated = SortDecision.objects.filter(profile=profile, status="pending").update(
        status="rejected",
        resolved_at=timezone.now(),
    )
    messages.info(request, f"Cleared {updated} pending item(s). Files stayed where they are.")
    return redirect("sorting_inbox")


def folder_rules(request):
    """Visual folder rule builder."""
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "create":
            FolderRule.objects.create(
                profile=profile,
                name=request.POST.get("name", "New rule")[:120],
                priority=int(request.POST.get("priority", 100)),
                match_field=request.POST.get("match_field", "filename"),
                operator=request.POST.get("operator", "contains"),
                pattern=request.POST.get("pattern", ""),
                file_extensions=request.POST.getlist("file_extensions"),
                subject_code=request.POST.get("subject_code", ""),
                category=request.POST.get("category", ""),
                action=request.POST.get("action_type", "route"),
                enabled=request.POST.get("enabled") == "on",
            )
            messages.success(request, "Rule created.")
            return redirect("folder_rules")
        elif action == "toggle":
            rule_pk = request.POST.get("rule_pk")
            rule = get_object_or_404(FolderRule, pk=rule_pk, profile=profile)
            rule.enabled = not rule.enabled
            rule.save()
            return redirect("folder_rules")
        elif action == "delete":
            rule_pk = request.POST.get("rule_pk")
            rule = get_object_or_404(FolderRule, pk=rule_pk, profile=profile)
            rule.delete()
            messages.success(request, "Rule deleted.")
            return redirect("folder_rules")

    rules_list = FolderRule.objects.filter(profile=profile).order_by("priority")

    return render(request, "organizer/folder_rules.html", {
        "profile": profile,
        "study_context": ctx,
        "rules": rules_list,
    })


def organization_memory(request):
    """Organization Memory: rules Orch learned from repeated user decisions
    -- fully visible, editable, and disableable, so the learning loop is
    never a black box."""
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)
    rules_qs = OrganizationMemoryRule.objects.filter(profile=profile)

    stats = {
        "total": rules_qs.count(),
        "enabled": rules_qs.filter(enabled=True).count(),
        "approved_patterns": rules_qs.filter(times_approved__gt=0).count(),
        "rejected_patterns": rules_qs.filter(times_rejected__gt=0).count(),
    }

    return render(request, "organizer/organization_memory.html", {
        "profile": profile,
        "study_context": ctx,
        "rules": rules_qs,
        "stats": stats,
    })


def organization_memory_rule_update(request, pk):
    """Edit, toggle, or reset the learning on one memory rule."""
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    rule = get_object_or_404(OrganizationMemoryRule, pk=pk, profile=profile)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "toggle":
            rule.enabled = not rule.enabled
            rule.save()
        elif action == "reset":
            rule.times_approved = 0
            rule.times_rejected = 0
            rule.save()
            messages.success(request, f"Learning reset for '{rule.name}'.")
        elif action == "update":
            destination = request.POST.get("destination_path", "").strip()
            if destination:
                rule.destination_path = destination
            name = request.POST.get("name", "").strip()
            if name:
                rule.name = name[:120]
            rule.save()
            messages.success(request, f"Updated '{rule.name}'.")
    return redirect("organization_memory")


def organization_memory_rule_delete(request, pk):
    """Forget a learned rule entirely."""
    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    rule = get_object_or_404(OrganizationMemoryRule, pk=pk, profile=profile)
    if request.method == "POST":
        name = rule.name
        rule.delete()
        messages.success(request, f"Deleted '{name}'.")
    return redirect("organization_memory")


def organization_dna(request):
    """Organization DNA: a read-only report built entirely from local
    database aggregation. Most active folders, common
    file types, subjects with the most movement, files waiting for review,
    and sorting accuracy."""
    from ..core.contexts import get_context_for_profile

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)
    events = MoveEvent.objects.filter(profile=profile, success=True)

    folder_counts = Counter()
    ext_counts = Counter()
    for destination_path in events.exclude(destination_path="").values_list("destination_path", flat=True):
        p = Path(destination_path)
        folder_counts[str(p.parent)] += 1
        if p.suffix:
            ext_counts[p.suffix.lstrip(".").lower()] += 1

    top_folders = [{"path": path, "count": count} for path, count in folder_counts.most_common(8)]
    top_extensions = [{"extension": ext, "count": count} for ext, count in ext_counts.most_common(8)]

    subject_rows = list(
        events.exclude(course_code="").exclude(course_code__isnull=True)
        .values("course_code").annotate(total=Count("id")).order_by("-total")[:8]
    )

    decisions = SortDecision.objects.filter(profile=profile)
    approved = decisions.filter(status="approved").count()
    rejected = decisions.filter(status="rejected").count()
    resolved = approved + rejected
    accuracy = int((approved / resolved) * 100) if resolved else 0
    total_moves = events.count()
    undone = events.filter(undo_available=False).count()
    undo_rate = int((undone / total_moves) * 100) if total_moves else 0

    stats = {
        "pending_review": decisions.filter(status="pending").count(),
        "held_sensitive": decisions.filter(decision_type="held_sensitive", status="pending").count(),
        "auto_moved": decisions.filter(status="moved").count(),
        "approved": approved,
        "rejected": rejected,
        "accuracy": accuracy,
        "undo_rate": undo_rate,
        "total_moves": total_moves,
    }

    return render(request, "organizer/organization_dna.html", {
        "profile": profile,
        "study_context": ctx,
        "stats": stats,
        "top_folders": top_folders,
        "top_extensions": top_extensions,
        "subject_rows": subject_rows,
    })


def category_test(request):
    """Preview what a global sort category would catch, without touching
    the database -- the Settings page's "Preview what this would catch"
    button."""
    from ..core import decision as decision_core

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    category_key = request.POST.get("category", "")
    test_filename = request.POST.get("test_filename", "").strip()
    if not test_filename:
        return JsonResponse({"error": "Enter a filename to test"}, status=400)

    test_path = Path(test_filename)
    ext = test_path.suffix.lstrip(".").lower()
    matched_key = decision_core.classify_global_category(test_path.name, ext)
    sensitive = decision_core.detect_sensitive(test_path.name, ext)

    if sensitive:
        action = "This file looks sensitive -- Orch will always hold it for review, regardless of category settings."
        matched = False
    elif matched_key and matched_key == category_key:
        action = f"This file would be caught by the '{category_key}' category."
        matched = True
    elif matched_key:
        action = f"This file matches the '{matched_key}' category instead, not '{category_key}'."
        matched = False
    else:
        action = "This file doesn't match any global category."
        matched = False

    return JsonResponse({"filename": test_filename, "category": category_key, "matched": matched, "action": action})




def import_plans(request):
    """Import from existing folders — approve/apply/reject workflow."""
    from ..core.contexts import get_context_for_profile
    from ..core import sorting, study

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        action = request.POST.get("action", "")
        plan_pk = request.POST.get("plan_pk")
        plan = get_object_or_404(FolderImportPlan, pk=plan_pk, profile=profile) if plan_pk else None

        if action == "scan":
            root_path = request.POST.get("root_path", "").strip()
            if not root_path:
                messages.error(request, "Enter a folder path to scan.")
            else:
                plan = study.create_import_plan(root_path, profile=profile)
                messages.success(request, f"Scanned: found {len(plan.proposed_subjects)} subjects, {len(plan.proposed_rules)} proposed rules.")
            return redirect("import_plans")

        elif action == "approve" and plan:
            if sorting.approve_import_plan(plan, profile):
                messages.success(request, f"Plan approved: subjects and rules adopted.")
            return redirect("import_plans")

        elif action == "apply" and plan:
            if sorting.apply_import_plan(plan, profile):
                messages.success(request, f"Plan applied: folder structure created.")
            return redirect("import_plans")

        elif action == "reject" and plan:
            sorting.reject_import_plan(plan)
            messages.info(request, "Plan rejected.")
            return redirect("import_plans")

        elif action == "reject_all_scanned":
            count = FolderImportPlan.objects.filter(profile=profile, status="scanned").update(status="rejected")
            messages.info(request, f"Rejected {count} scanned plan(s).")
            return redirect("import_plans")

    plans = FolderImportPlan.objects.filter(profile=profile).order_by("-updated_at")

    from ..models import BackgroundTask

    folder_sorts = BackgroundTask.objects.filter(profile=profile, kind="large_folder_sort")[:5]

    return render(request, "organizer/import_plans.html", {
        "profile": profile,
        "study_context": ctx,
        "plans": plans,
        "folder_sorts": folder_sorts,
    })


def folder_sort_start(request):
    """Kicks off Large Folder Scan Mode: runs every file in an arbitrary
    folder through the same trust-layer pipeline the watcher uses (see
    organizer.core.sorting.sort_folder), as a background job so a folder
    with thousands of files doesn't block the request for however long
    that takes."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    root_path = request.POST.get("root_path", "").strip()
    if not root_path:
        return JsonResponse({"ok": False, "error": "Enter a folder path first."}, status=400)
    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        return JsonResponse({"ok": False, "error": "That folder does not exist or cannot be opened."}, status=400)

    from ..core import jobs, sorting

    def _do_sort_folder(task=None):
        return sorting.sort_folder(str(root), profile, task=task, log=write_log)

    task = jobs.enqueue("large_folder_sort", _do_sort_folder, profile=profile)
    return JsonResponse({"ok": True, "task_id": task.pk})


def folder_sort_cancel(request, pk):
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required."}, status=405)

    from ..core import jobs
    from ..models import BackgroundTask

    profile = Profile.get_active()
    task = get_object_or_404(BackgroundTask, pk=pk, profile=profile)
    jobs.request_cancel(task.pk)
    return JsonResponse({"ok": True})


def undo_recent(request):
    """View and restore recently moved files."""
    from ..core.contexts import get_context_for_profile
    from ..core import undo

    profile = Profile.get_active()
    if not profile:
        messages.error(request, "Activate a profile first.")
        return redirect("dashboard")

    ctx = get_context_for_profile(profile)

    if request.method == "POST":
        action = request.POST.get("action", "")
        move_pk = request.POST.get("move_pk")
        event = get_object_or_404(MoveEvent, Q(profile=profile) | Q(profile__isnull=True), pk=move_pk) if move_pk else None

        if action == "restore" and event:
            if undo.restore_move(event):
                messages.success(request, f"Restored: {event.filename}")
            else:
                messages.error(request, f"Could not restore {event.filename}")
            return redirect("undo_recent")

        elif action == "restore_all":
            count = undo.restore_recent(profile, minutes=60)
            if count:
                messages.success(request, f"Restored {count} file(s) from the last hour.")
            else:
                messages.info(request, "No files to restore.")
            return redirect("undo_recent")

    stats = undo.get_undo_stats(profile)
    restorable = undo.get_restorable_moves(profile)

    return render(request, "organizer/undo_recent.html", {
        "profile": profile,
        "study_context": ctx,
        "stats": stats,
        "restorable": restorable,
    })


def rule_test(request):
    """Test a folder rule against a filename before saving."""
    from ..core import sorting

    profile = Profile.get_active()
    if not profile:
        return JsonResponse({"error": "No active profile"}, status=400)

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    rule_data = {
        "name": request.POST.get("name", "Test rule"),
        "match_field": request.POST.get("match_field", "filename"),
        "operator": request.POST.get("operator", "contains"),
        "pattern": request.POST.get("pattern", ""),
        "file_extensions": request.POST.getlist("file_extensions"),
        "subject_code": request.POST.get("subject_code", ""),
        "category": request.POST.get("category", ""),
        "action": request.POST.get("action_type", "route"),
    }

    test_filename = request.POST.get("test_filename", "").strip()
    if not test_filename:
        return JsonResponse({"error": "Enter a filename to test"}, status=400)

    # Create a temporary rule object to evaluate
    from django.db.models import Model
    from ..models import FolderRule

    # Build a mock rule-like dict
    from types import SimpleNamespace
    mock_rule = SimpleNamespace(**rule_data)

    from pathlib import Path
    test_path = Path(test_filename)
    matched, dest = sorting.evaluate_rule(mock_rule, test_path.name, test_path)

    result = {
        "matched": matched,
        "destination": dest,
        "rule": rule_data["name"],
        "filename": test_filename,
    }

    if dest == "__IGNORE__":
        result["action"] = "File will be ignored"
    elif dest == "__INBOX__":
        result["action"] = "File will be sent to inbox for review"
    elif dest:
        result["action"] = f"File will be routed to: {dest}"
    else:
        result["action"] = "File will NOT match this rule"

    return JsonResponse(result)


def preview_scan(request):
    """Preview what Orch would do with files in an existing folder."""
    from ..core import sorting, rules as routing_rules

    profile = Profile.get_active()
    if not profile:
        return JsonResponse({"error": "No active profile"}, status=400)

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    folder_path = request.POST.get("folder_path", "").strip()
    if not folder_path:
        return JsonResponse({"error": "Enter a folder path"}, status=400)

    path = Path(folder_path)
    if not path.exists() or not path.is_dir():
        return JsonResponse({"error": "Folder does not exist"}, status=400)

    # Scan the folder
    from organizer.models import FolderRule

    rules = FolderRule.objects.filter(profile=profile, enabled=True).order_by("priority")
    files_found = []
    matched_count = 0
    unmatched_count = 0

    for f in sorted(path.iterdir()):
        if not f.is_file():
            continue

        file_info = {"filename": f.name, "matched": False, "rule": None, "destination": None}

        # Check against rules
        for rule in rules:
            matched, dest = sorting.evaluate_rule(rule, f.name, f)
            if matched:
                file_info["matched"] = True
                file_info["rule"] = rule.name
                if dest == "__IGNORE__":
                    file_info["destination"] = "Ignored"
                elif dest == "__INBOX__":
                    file_info["destination"] = "Inbox"
                elif dest:
                    file_info["destination"] = dest
                matched_count += 1
                break

        # If no rule matched, show what the default routing would do
        if not file_info["matched"]:
            default_dest = routing_rules.get_destination(f, profile_root=profile.root_path)
            if default_dest:
                file_info["destination"] = str(default_dest.path)
                file_info["rule"] = "Default routing"
            unmatched_count += 1

        files_found.append(file_info)

    return JsonResponse({
        "folder": str(path),
        "total_files": len(files_found),
        "matched": matched_count,
        "unmatched": unmatched_count,
        "files": files_found[:100],  # Limit to 100 for preview
    })
