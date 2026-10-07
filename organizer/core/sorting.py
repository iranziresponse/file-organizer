"""Sorting and trust layer — rule execution, inbox approval workflow,
and import-from-existing-folders apply logic.

Completes the three unfinished features:
1. Folder Rule Builder — execute rules against incoming files
2. Inbox Mode — approve/reroute/ignore files in the decision inbox
3. Import From Existing Folders — approve and apply import plans
"""

import json
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from django.utils import timezone

from . import makerere_curricula, paths, rules
from .watcher import write_log


# ---------------------------------------------------------------------------
# 1. Folder Rule Execution
# ---------------------------------------------------------------------------

def evaluate_rule(rule, filename: str, file_path: Path) -> tuple[bool, str | None]:
    """Evaluate a FolderRule against a filename.

    Returns (matched, destination_path_or_None).
    """
    from organizer.models import Profile

    target = filename if rule.match_field == "filename" else file_path.suffix.lower()
    if rule.match_field == "extension":
        target = file_path.suffix.lstrip(".").lower()
    elif rule.match_field == "source_path":
        target = str(file_path)

    # File extension filter
    if rule.file_extensions:
        ext = file_path.suffix.lstrip(".").lower()
        if ext not in rule.file_extensions:
            return False, None

    # Match
    matched = False
    pattern = rule.pattern
    if rule.operator == "contains":
        matched = pattern.lower() in target.lower()
    elif rule.operator == "equals":
        matched = target.lower() == pattern.lower()
    elif rule.operator == "starts_with":
        matched = target.lower().startswith(pattern.lower())
    elif rule.operator == "ends_with":
        matched = target.lower().endswith(pattern.lower())
    elif rule.operator == "regex":
        try:
            matched = bool(re.search(pattern, target, re.IGNORECASE))
        except re.error:
            matched = False

    if not matched:
        return False, None

    # Action: ignore — return True with no destination
    if rule.action == "ignore":
        return True, "__IGNORE__"

    # Action: review — return True with inbox signal
    if rule.action == "review":
        return True, "__INBOX__"

    # Action: route — build destination path
    if rule.action == "route" and rule.subject_code:
        profile = rule.profile
        config = rules.load_config(profile.root_path)
        if config:
            dest = (
                Path(profile.root_path)
                / config.get("primary_value", "_Unknown")
                / config.get("secondary_value", "_Unknown")
                / rule.subject_code
                / (rule.category or "Study Material")
            )
        else:
            dest = Path(profile.root_path) / "_Routed" / rule.subject_code
        return True, str(dest)

    return True, None


def execute_rules_for_file(
    file_path: Path,
    profile,
    log: Callable | None = None,
) -> tuple[str | None, str | None]:
    """Run all enabled FolderRules against a file. Returns the first match.

    Returns (destination_path_or_signal, matched_rule_name_or_None).
    Signals: "__IGNORE__" = skip file, "__INBOX__" = send to inbox.
    """
    from organizer.models import FolderRule

    rules_qs = FolderRule.objects.filter(
        profile=profile, enabled=True
    ).order_by("priority")

    for rule in rules_qs:
        try:
            matched, dest = evaluate_rule(rule, file_path.name, file_path)
            if matched:
                if dest == "__IGNORE__":
                    if log:
                        log(f"Rule '{rule.name}' ignored '{file_path.name}'")
                    return "__IGNORE__", rule.name
                if dest == "__INBOX__":
                    if log:
                        log(f"Rule '{rule.name}' sent '{file_path.name}' to inbox")
                    return "__INBOX__", rule.name
                if dest:
                    return dest, rule.name
        except Exception as exc:
            if log:
                log(f"Rule '{rule.name} failed for '{file_path.name}': {exc}")

    return None, None


# ---------------------------------------------------------------------------
# 2. The Trust-Layer Pipeline — decide, then (maybe) act
# ---------------------------------------------------------------------------
#
# Every sorter returns a decision.DecisionResult instead of moving a file
# directly. decide_for_file() is pure decision-making (safe to call from a
# preview/dry-run context — it never touches disk or the database);
# process_file() is the only place a DecisionResult gets carried out.
#
# Pipeline order, per file:
#   1. Safety scan     — sensitive files always held for review.
#   2. Profile routing  — the trusted zone: subject code / topic match.
#   3. User rules       — explicit FolderRules, then learned OrganizationMemoryRules.
#   4. Global category  — media/ebooks/archives/installers/code, opt-in only.
#   5. Fallback          — nothing matched: leave the file untouched.

def match_memory_rule(profile, file_path: Path):
    """The best-matching enabled OrganizationMemoryRule for this file, or
    None. Checked after explicit FolderRules (hand-authored, always win)
    but before global category suggestions (generic, no memory of this
    user's own past decisions)."""
    from organizer.models import OrganizationMemoryRule

    if not profile:
        return None

    ext = file_path.suffix.lstrip(".").lower()
    name = file_path.name.lower()
    source_folder = str(file_path.parent).lower()

    for rule in OrganizationMemoryRule.objects.filter(profile=profile, enabled=True):
        value = rule.match_value.lower()
        if rule.match_type == "extension" and value == ext:
            return rule
        if rule.match_type == "filename_contains" and value in name:
            return rule
        if rule.match_type == "folder_source" and value in source_folder:
            return rule
        if rule.match_type == "subject_code" and value in name:
            return rule
    return None


def decide_for_file(file_path: Path, profile, settings=None, ai_classify: Callable | None = None):
    """Runs the five-stage pipeline described above and returns a single
    decision.DecisionResult. Never moves a file or writes to the database —
    safe to call from a preview/test context."""
    from . import decision
    from organizer.models import GlobalSortCategory

    file_path = Path(file_path)
    name = file_path.name
    ext = file_path.suffix.lstrip(".").lower()
    lname = name.lower()

    if lname.endswith((".crdownload", ".part", ".tmp")):
        return decision.DecisionResult(
            action="leave", confidence=0, destination=None,
            explanation="Still downloading.", decision_type="manual",
        )

    # 1. Safety scan — sensitive files always win, regardless of everything else.
    if decision.detect_sensitive(name, ext):
        sensitive = GlobalSortCategory.objects.filter(key="sensitive").first()
        dest = Path(sensitive.destination_path) if sensitive and sensitive.destination_path else paths.IMPORTANT_ROOT
        return decision.DecisionResult(
            action="hold", confidence=0, destination=dest,
            explanation="Held for review because this looks like a private key, password, or other sensitive file.",
            decision_type="held_sensitive", method="sensitive", category_key="sensitive",
        )

    # 2. Profile routing — the trusted zone. Documents only; the user
    #    explicitly opted into this profile's own structure.
    if profile and profile.root_path and ext in paths.DOC_EXT:
        dest = rules.route_profile_document(file_path, profile.root_path, ai_classify=ai_classify)
        if dest:
            code = dest.course_code or ""
            if dest.method == "course_code":
                reason = f"Moved to {code} because the filename contains the active profile's subject code."
            elif dest.method == "topic":
                reason = f"Moved to {code} because the filename's topic matched this subject's keywords."
            else:
                reason = f"Moved to {code} using Orch's optional AI fallback."
            return decision.DecisionResult(
                action="auto_move", confidence=100, destination=dest.path,
                explanation=reason, decision_type="profile_auto", method=dest.method,
                matched_rule_name=code,
            )

    # 3. User-created rules — explicit FolderRules first, always trusted at
    #    face value since the user wrote the destination themselves. Then
    #    OrganizationMemoryRules, which are inferred from past approvals and
    #    so are confidence-gated like anything else Orch guesses at.
    if profile:
        rule_dest, rule_name = execute_rules_for_file(file_path, profile)
        if rule_dest == "__IGNORE__":
            return decision.DecisionResult(
                action="leave", confidence=100, destination=None,
                explanation=f"Ignored by your rule '{rule_name}'.", decision_type="manual",
                matched_rule_name=rule_name,
            )
        if rule_dest == "__INBOX__":
            score = decision.score_confidence(explicit_rule_match=True)
            return decision.DecisionResult(
                action="suggest", confidence=score, destination=None,
                explanation=f"Your rule '{rule_name}' flagged this file for review.",
                decision_type="global_suggested", matched_rule_name=rule_name,
            )
        if rule_dest:
            return decision.DecisionResult(
                action="auto_move", confidence=100, destination=Path(rule_dest),
                explanation=f"Moved to this folder because it matched your rule '{rule_name}'.",
                decision_type="global_auto", method="course_code", matched_rule_name=rule_name,
            )

        memory_rule = match_memory_rule(profile, file_path)
        if memory_rule:
            dest_path = Path(memory_rule.destination_path)
            score = decision.score_confidence(
                prior_approved_boost=memory_rule.confidence_boost,
                destination_exists=dest_path.exists(),
                prior_rejected=memory_rule.times_rejected > memory_rule.times_approved,
            )
            if score >= decision.AUTO_THRESHOLD:
                action = "auto_move"
            elif score >= decision.SUGGEST_THRESHOLD:
                action = "suggest"
            else:
                action = None
            if action:
                category_key = decision.classify_global_category(name, ext)
                return decision.DecisionResult(
                    action=action, confidence=score, destination=dest_path,
                    explanation=f"Suggested this folder because you approved {memory_rule.times_approved} similar file(s) before.",
                    decision_type="global_auto" if action == "auto_move" else "global_suggested",
                    method=decision.CATEGORY_METHOD.get(category_key, "unsorted"),
                    matched_rule_name=memory_rule.name,
                )

    # 4. Global category suggestion — only acts if the category is enabled.
    category_key = decision.classify_global_category(name, ext)
    if category_key:
        category = GlobalSortCategory.objects.filter(key=category_key).first()
        if category and category.enabled:
            dest_path = Path(category.destination_path) if category.destination_path else None
            score = decision.score_confidence(
                extension_category_match=True,
                destination_exists=bool(dest_path and dest_path.exists()),
            )
            if category.mode == "auto":
                action = "auto_move"
            elif category.mode == "auto_high_confidence":
                action = "auto_move" if score >= decision.AUTO_THRESHOLD else "suggest"
            else:
                action = "suggest"
            label = category.label or dict(GlobalSortCategory.KEY_CHOICES).get(category_key, category_key)
            verb = "Moved" if action == "auto_move" else "Suggested"
            return decision.DecisionResult(
                action=action, confidence=score, destination=dest_path,
                explanation=f"{verb} to {label} because this is a .{ext} file and {label.lower()} sorting is enabled.",
                decision_type="global_auto" if action == "auto_move" else "global_suggested",
                method=decision.CATEGORY_METHOD.get(category_key, "unsorted"),
                category_key=category_key,
            )

    # 5. Fallback — nothing matched, or the category is disabled. Global
    #    sorting stays conservative: the file is left exactly where it is.
    return decision.DecisionResult(
        action="leave", confidence=0, destination=None,
        explanation="Left in place — no matching profile, rule, or enabled category.",
        decision_type="manual",
    )


def _move_into(source: Path, dest_dir: Path) -> Path:
    """Shared move-with-collision-rename logic used by both an auto-moved
    file and an approved inbox item, so the two paths can't drift apart."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / source.name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = dest_dir / f"{target.stem}_{stamp}{target.suffix}"
    shutil.move(str(source), str(target))
    return target


def process_file(
    file_path: Path,
    profile,
    settings=None,
    log: Callable | None = None,
    ai_enabled: bool | None = None,
):
    """Decides what to do with a file, then carries it out. Returns the
    decision.DecisionResult that was acted on. The watcher calls this for
    every newly-downloaded file; nothing else moves a file off the pipeline
    without going through here.

    ai_enabled: None (the default) defers to the active profile's own
    ai_fallback_enabled setting. Pass True/False explicitly to override it,
    which is what the test suite does.

    Thin timing wrapper around _process_file_impl() -- kept separate so the
    Performance Health Panel's instrumentation can't change any of that
    function's own control flow (several early returns below)."""
    from . import perf

    with perf.measure("sort_file", profile=profile, detail=Path(file_path).name):
        return _process_file_impl(file_path, profile, settings=settings, log=log, ai_enabled=ai_enabled)


def _process_file_impl(
    file_path: Path,
    profile,
    settings=None,
    log: Callable | None = None,
    ai_enabled: bool | None = None,
):
    from . import ai_classify as ai_classify_module
    from . import decision, notifications
    from organizer.models import SortDecision
    from .watcher import _record_event

    file_path = Path(file_path)
    use_ai = bool(profile and profile.ai_fallback_enabled) if ai_enabled is None else ai_enabled
    classify_fn = (
        (lambda n, curriculum: ai_classify_module.classify(n, curriculum, log=log or write_log))
        if use_ai else None
    )
    result = decide_for_file(file_path, profile, settings=settings, ai_classify=classify_fn)

    if result.action == "leave":
        if log:
            log(f"Left '{file_path.name}' in place: {result.explanation}")
        return result

    if result.action in ("hold", "suggest"):
        # A file left un-moved (held/suggested) stays put and gets re-seen
        # every poll cycle -- don't pile up a duplicate pending SortDecision
        # for the same file every few seconds while the user hasn't acted
        # on the first one yet.
        already_pending = SortDecision.objects.filter(
            profile=profile, source_path=str(file_path), status="pending"
        ).exists()
        if not already_pending:
            SortDecision.objects.create(
                profile=profile,
                filename=file_path.name,
                source_path=str(file_path),
                suggested_destination=str(result.destination) if result.destination else "",
                decision_type=result.decision_type,
                confidence=result.confidence,
                explanation=result.explanation,
                matched_rule=result.matched_rule_name[:120],
                status="pending",
            )
            if log:
                log(f"Sent '{file_path.name}' to Decision Inbox: {result.explanation}")
        return result

    # action == "auto_move"
    if not result.destination:
        return result

    from .watcher import is_ready

    if not is_ready(file_path):
        if log:
            log(f"Skipped '{file_path.name}' this cycle. Still locked or still growing, will retry")
        return result

    try:
        target = _move_into(file_path, result.destination)
    except OSError as exc:
        if log:
            log(f"FAILED to move '{file_path.name}': {exc}")
        _record_event(
            profile=profile, filename=file_path.name, source_path=str(file_path),
            destination_path=str(result.destination / file_path.name), method=result.method,
            course_code=result.matched_rule_name or "", success=False, error_message=str(exc),
            explanation=result.explanation, confidence=result.confidence, decision_source=result.decision_type,
        )
        return result

    if log:
        log(f"Moved '{file_path.name}' -> {result.destination} ({result.explanation})")

    event = _record_event(
        profile=profile, filename=file_path.name, source_path=str(file_path),
        destination_path=str(target), method=result.method,
        course_code=result.matched_rule_name or "", success=True,
        explanation=result.explanation, confidence=result.confidence,
        decision_source=result.decision_type, undo_available=True,
    )
    SortDecision.objects.create(
        profile=profile, filename=file_path.name, source_path=str(file_path),
        suggested_destination=str(result.destination), final_destination=str(target),
        decision_type=result.decision_type, confidence=result.confidence,
        explanation=result.explanation, matched_rule=result.matched_rule_name[:120],
        status="moved", move_event=event, resolved_at=timezone.now(),
    )

    notifications.notify_file_sorted(file_path.name, result.method, str(target), profile=profile)

    threading.Thread(
        target=_backup_and_record, args=(event.pk, str(target)), kwargs={"log": log or write_log}, daemon=True,
    ).start()

    return result


def _backup_and_record(move_event_pk, target_path, log=None):
    """The fire-and-forget Drive backup after a move used to just discard
    its own result -- a failed upload (offline, quota, token expired)
    vanished with nothing to retry and nothing to show the user. Now it
    records what happened on the MoveEvent itself, via
    MoveEvent.drive_backup_status, so retry_failed_drive_backups() below
    has something to find. Left at "not_attempted" (the default) rather
    than "failed" when Drive backup simply isn't turned on -- that's not
    a failure, there's nothing to retry."""
    from . import drive_api
    from organizer.models import MoveEvent

    config = drive_api.load_drive_config()
    if not config or not config.get("enabled"):
        return

    event = MoveEvent.objects.select_related("profile").filter(pk=move_event_pk).first()
    if not event:
        return

    success = drive_api.backup_file(target_path, log=log)
    updated = MoveEvent.objects.filter(pk=move_event_pk).update(
        drive_backup_status="success" if success else "failed"
    )
    if updated and not success and event.profile_id:
        from . import notifications

        notifications.notify_drive_backup_attention(event.profile)


def retry_failed_drive_backups(profile, task=None, log=None):
    """Re-attempts every MoveEvent this profile has marked
    drive_backup_status="failed" whose file is still where Orch left it.
    Meant to be run through organizer.core.jobs.enqueue() -- see
    organizer.views.integrations.drive_backup_retry."""
    from . import drive_api
    from organizer.models import MoveEvent

    config = drive_api.load_drive_config()
    if not config or not config.get("enabled"):
        return "Google Drive backup isn't turned on, so there's nothing to retry."

    failed = list(MoveEvent.objects.filter(profile=profile, drive_backup_status="failed"))
    retried = succeeded = missing = 0

    for i, event in enumerate(failed):
        if task:
            task.update(i, total=len(failed))
        target = Path(event.destination_path) if event.destination_path else None
        if not target or not target.exists():
            missing += 1
            continue
        retried += 1
        if drive_api.backup_file(str(target), log=log):
            succeeded += 1
            MoveEvent.objects.filter(pk=event.pk).update(drive_backup_status="success")

    if task:
        task.update(len(failed), total=len(failed))

    return (
        f"Retried {retried} backup(s): {succeeded} succeeded, {retried - succeeded} still failed"
        f"{f', {missing} skipped (file no longer where Orch left it)' if missing else ''}."
    )


# ---------------------------------------------------------------------------
# 2b. Large Folder Scan Mode — run process_file() over a whole folder
# ---------------------------------------------------------------------------

def sort_folder(root_path, profile, task=None, log: Callable | None = None):
    """Runs every file already sitting in an arbitrary folder through the
    exact same trust-layer pipeline the watcher uses for new downloads
    (process_file() -- sensitive-file safety net, profile routing, user
    rules, Decision Inbox for anything uncertain, all included for free).
    This is the only difference from the watcher: it walks a folder the
    user points at instead of reacting to new downloads.

    Incremental: organizer.core.file_index skips a file that's unchanged
    since the last sort_folder() pass over this same folder, so a file
    that was left in place or held for review last time doesn't get
    re-evaluated (and, if uncertain, re-added to the Decision Inbox) on
    every re-run -- files that WERE moved simply aren't in the folder
    walk anymore, so they're skipped automatically too.

    task: an organizer.core.jobs.ProgressReporter, if this is running
    through jobs.enqueue() -- also polled for a "cancelled" status every
    few files, so a long scan can be stopped between files (never
    mid-move) without losing what's already been sorted."""
    from . import file_index

    root = Path(root_path)
    if not root.exists() or not root.is_dir():
        return "That folder does not exist or cannot be opened."

    files = [p for p in root.rglob("*") if p.is_file()]
    total = len(files)
    moved = skipped_unchanged = held_or_suggested = left = failed = 0
    cancelled = False

    for i, file_path in enumerate(files):
        if task and i % 5 == 0 and task.is_cancelled():
            cancelled = True
            break

        if file_index.check(file_path, profile=profile):
            skipped_unchanged += 1
        else:
            try:
                result = process_file(file_path, profile, log=log)
                if result.action == "auto_move":
                    moved += 1
                elif result.action in ("hold", "suggest"):
                    held_or_suggested += 1
                else:
                    left += 1
            except Exception as exc:
                failed += 1
                if log:
                    log(f"Large folder sort: failed on '{file_path.name}': {exc}")
            file_index.record(file_path, profile=profile)

        if task:
            task.update(
                i + 1, total=total,
                message=f"{moved} moved, {held_or_suggested} sent to Decision Inbox, {skipped_unchanged} unchanged, {failed} failed",
            )

    summary = (
        f"{'Cancelled after' if cancelled else 'Scanned'} {i + 1 if cancelled else total} of {total} file(s): "
        f"{moved} moved, {held_or_suggested} sent to Decision Inbox, "
        f"{left} left in place, {skipped_unchanged} unchanged since last time, {failed} failed."
    )
    return summary


# ---------------------------------------------------------------------------
# 3. Decision Inbox — approve / reroute / reject a pending SortDecision
# ---------------------------------------------------------------------------

def approve_inbox_item(item, remember: bool = False, log: Callable | None = None) -> bool:
    """Approve a pending SortDecision — move the file to its suggested
    destination. If `remember` is set, also teach Orch an OrganizationMemoryRule
    from this approval ("Always do this"). Returns True on success."""
    from organizer.models import MoveEvent
    from .watcher import _record_event

    if item.status != "pending":
        return False

    dest_path = Path(item.suggested_destination) if item.suggested_destination else None
    source = Path(item.source_path) if item.source_path else None

    if not dest_path or not source or not source.exists():
        item.status = "rejected"
        item.resolved_at = timezone.now()
        item.save()
        return False

    try:
        target = _move_into(source, dest_path)
    except OSError as exc:
        if log:
            log(f"Failed to approve inbox item '{item.filename}': {exc}")
        return False

    method = _method_for_decision_type(item.decision_type, item.filename)
    event = _record_event(
        profile=item.profile,
        filename=item.filename,
        source_path=str(source),
        destination_path=str(target),
        method=method,
        course_code=item.matched_rule or "",
        success=True,
        explanation=item.explanation,
        confidence=item.confidence,
        decision_source=item.decision_type,
        undo_available=True,
    )
    item.final_destination = str(target)
    item.status = "approved"
    item.resolved_at = timezone.now()
    item.move_event = event
    item.save()

    if remember:
        remember_decision(item)

    return True


def reroute_inbox_item(item, new_destination: str, log: Callable | None = None) -> bool:
    """Reroute a pending inbox item to a different destination the user
    picked themselves, then move it there and approve it."""
    if item.status != "pending":
        return False

    item.suggested_destination = new_destination
    item.save()
    return approve_inbox_item(item, log=log)


def reject_inbox_item(item, never_again: bool = False) -> None:
    """Reject a pending inbox item (leave the file in place). If
    `never_again` is set, teaches Orch to stop suggesting this pattern by
    penalizing the matching OrganizationMemoryRule (or creating a disabled
    one so future scoring for this pattern starts already penalized)."""
    if item.status != "pending":
        return
    item.status = "rejected"
    item.resolved_at = timezone.now()
    item.save()

    if never_again:
        reject_pattern(item)


def remember_decision(item, name: str = "") -> object | None:
    """"Always do this": create/update an OrganizationMemoryRule from an
    approved SortDecision, matched by file extension — the simplest signal
    that generalizes across future files without over-fitting to one exact
    filename."""
    from organizer.models import OrganizationMemoryRule

    ext = Path(item.filename).suffix.lstrip(".").lower()
    # suggested_destination is always a folder, even after a reroute (which
    # overwrites it with the user's chosen folder before approving) --
    # final_destination is the exact moved-to *file* path and would teach a
    # rule to route future files into a name that doesn't exist yet.
    dest = item.suggested_destination
    if not ext or not dest:
        return None

    rule, created = OrganizationMemoryRule.objects.get_or_create(
        profile=item.profile,
        match_type="extension",
        match_value=ext,
        defaults={
            "name": name[:120] or f"Files ending in .{ext}",
            "destination_path": dest,
        },
    )
    if not created:
        rule.destination_path = dest
        rule.enabled = True
    rule.times_approved += 1
    rule.last_used_at = timezone.now()
    rule.save()
    return rule


def reject_pattern(item) -> object | None:
    """Records a rejection against the OrganizationMemoryRule matching this
    item's extension (if one exists yet), so score_confidence's
    prior_rejected penalty applies to the next similar file."""
    from organizer.models import OrganizationMemoryRule

    ext = Path(item.filename).suffix.lstrip(".").lower()
    if not ext:
        return None
    rule = OrganizationMemoryRule.objects.filter(
        profile=item.profile, match_type="extension", match_value=ext
    ).first()
    if rule:
        rule.times_rejected += 1
        rule.save()
    return rule


def _method_for_decision_type(decision_type: str, filename: str) -> str:
    """MoveEvent.method for a manually-approved inbox item — resolved from
    the file's own extension/category rather than trusting decision_type
    alone, since "global_suggested"/"held_sensitive" don't map 1:1 to a
    single method value."""
    from . import decision

    if decision_type == "profile_auto":
        return "course_code"
    if decision_type == "held_sensitive":
        return "sensitive"
    ext = Path(filename).suffix.lstrip(".").lower()
    category_key = decision.classify_global_category(filename, ext)
    return decision.CATEGORY_METHOD.get(category_key, "unsorted")


def get_pending_inbox(profile, limit: int = 50):
    """Get all pending SortDecisions for a profile."""
    from organizer.models import SortDecision

    return SortDecision.objects.filter(
        profile=profile, status="pending"
    ).order_by("-created_at")[:limit]


def get_inbox_stats(profile) -> dict:
    """Get inbox statistics for a profile."""
    from organizer.models import SortDecision

    qs = SortDecision.objects.filter(profile=profile)
    return {
        "pending": qs.filter(status="pending").count(),
        "approved": qs.filter(status="approved").count(),
        "rejected": qs.filter(status="rejected").count(),
        "moved": qs.filter(status="moved").count(),
        "total": qs.count(),
    }


# ---------------------------------------------------------------------------
# 3. Import From Existing Folders — Approve & Apply
# ---------------------------------------------------------------------------

def approve_import_plan(plan, profile, log: Callable | None = None) -> bool:
    """Approve an import plan — adopt proposed subjects and rules.

    Returns True on success.
    """
    if plan.status not in ("draft", "scanned"):
        return False

    from organizer.models import CourseConfig

    plan.status = "approved"
    plan.save()

    # Adopt proposed subjects into the profile's config. Mutate the
    # profile's own cached `.config` (rather than a fresh query) so a
    # caller already holding `profile` sees the update reflected too.
    try:
        config = profile.config
    except CourseConfig.DoesNotExist:
        config = None
    if config and plan.proposed_subjects:
        existing = set(config.groups or [])
        new_subjects = [s for s in plan.proposed_subjects if s not in existing]
        if new_subjects:
            config.groups = list(existing) + new_subjects
            config.save()

    # Create FolderRules for proposed rules
    from organizer.models import FolderRule

    rules_created = 0
    for rule_data in plan.proposed_rules:
        existing = FolderRule.objects.filter(
            profile=profile,
            name=rule_data.get("name", ""),
        ).first()
        if existing:
            continue
        FolderRule.objects.create(
            profile=profile,
            name=rule_data.get("name", f"Import rule {rules_created + 1}")[:120],
            priority=100 + rules_created,
            match_field=rule_data.get("match_field", "filename"),
            operator=rule_data.get("operator", "contains"),
            pattern=rule_data.get("pattern", ""),
            subject_code=rule_data.get("subject_code", ""),
            category=rule_data.get("category", "Study Material"),
            action=rule_data.get("action", "route"),
            enabled=True,
        )
        rules_created += 1

    if log:
        log(f"Import plan approved: {len(plan.proposed_subjects)} subjects, {rules_created} rules")

    return True


def apply_import_plan(plan, profile, log: Callable | None = None) -> bool:
    """Apply an approved import plan — create folder structure.

    Creates the folder hierarchy defined by the plan's discovered folders
    under the profile's root path.

    Returns True on success.
    """
    if plan.status != "approved":
        if log:
            log(f"Cannot apply plan in status '{plan.status}', must be 'approved' first")
        return False

    root = Path(plan.root_path)
    if not root.exists():
        if log:
            log(f"Import root '{root}' does not exist")
        return False

    from organizer.models import CourseConfig

    config = CourseConfig.objects.filter(profile=profile).first()
    if not config:
        if log:
            log("Profile has no config, cannot create folder structure")
        return False

    profile_root = Path(profile.root_path)
    primary = config.primary_value
    secondary = config.secondary_value

    folders_created = 0
    for folder_path in plan.discovered_folders:
        parts = folder_path.split("/")
        if len(parts) >= 2:
            # Structure: <subject>/<category>
            subject, category = parts[0], "/".join(parts[1:])
            dest = profile_root / primary / secondary / subject / category
        elif len(parts) == 1:
            dest = profile_root / primary / secondary / parts[0]
        else:
            continue

        try:
            dest.mkdir(parents=True, exist_ok=True)
            folders_created += 1
        except OSError as exc:
            if log:
                log(f"Failed to create folder '{dest}': {exc}")

    plan.status = "imported"
    plan.save()

    if log:
        log(f"Import applied: {folders_created} folders created under '{profile_root}'")

    return True


def reject_import_plan(plan) -> None:
    """Reject an import plan."""
    if plan.status in ("draft", "scanned", "approved"):
        plan.status = "rejected"
        plan.save()


def relocate_move_event(event, new_destination: str, log: Callable | None = None) -> bool:
    """Manually move an already-sorted file to a different folder the user
    picked themselves, e.g. because Orch's routing wasn't quite right.

    Updates the event's own destination_path in place rather than creating
    a new event (unlike undo, which deliberately creates a new record to
    trace the reversal) -- a relocate isn't a new "thing that happened,"
    it's correcting where this same file already lives, so Undo and
    Summarize on this same event keep working against wherever the file
    actually is now.
    """
    if not event.destination_path or not event.success:
        return False

    current = Path(event.destination_path)
    if not current.exists():
        if log:
            log(f"Cannot relocate '{event.filename}': not found at {current}")
        return False

    dest_dir = Path(new_destination)
    dest_dir.mkdir(parents=True, exist_ok=True)
    new_path = dest_dir / current.name

    try:
        shutil.move(str(current), str(new_path))
    except OSError as exc:
        if log:
            log(f"Failed to relocate '{event.filename}': {exc}")
        return False

    event.destination_path = str(new_path)
    event.save(update_fields=["destination_path"])
    if log:
        log(f"Relocated '{event.filename}' -> {new_path}")
    return True


def ensure_subject_folders(profile) -> dict:
    """Make sure every subject/course-unit folder for this profile's
    current config exists on disk, and that there's only ever ONE folder
    per subject -- never a bare "CODE" sitting alongside a "CODE - Name"
    for the same course.

    - If "CODE - Name" already exists: nothing to do.
    - If only bare "CODE" exists and the real name is known: renamed to
      "CODE - Name" in place (contents preserved) -- a bare code folder is
      never left as a redundant duplicate once its name is knowable.
    - If neither exists: created fresh, named if the name is known.
    - If the name isn't known (not a Makerere course Orch has researched,
      or a manually-typed subject): stays as bare "CODE", same as before.

    Returns {"created": [codes], "existing": [codes], "renamed": [codes]}.
    """
    config = getattr(profile, "config", None)
    if not profile or not profile.root_path or not config or not config.groups:
        return {"created": [], "existing": [], "renamed": []}

    base = Path(profile.root_path) / config.primary_value / config.secondary_value
    created, existing, renamed = [], [], []

    for code in config.groups:
        name = makerere_curricula.name_for_code(code)
        named_folder = (base / f"{code} - {name}") if name else None
        bare_folder = base / code

        if named_folder and named_folder.exists():
            existing.append(code)
        elif bare_folder.exists():
            if named_folder:
                bare_folder.rename(named_folder)
                renamed.append(code)
            else:
                existing.append(code)
        else:
            target = named_folder or bare_folder
            target.mkdir(parents=True, exist_ok=True)
            created.append(code)

    return {"created": created, "existing": existing, "renamed": renamed}