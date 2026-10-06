from datetime import timedelta
from pathlib import Path

from django.db.models import Count, Max
from django.utils import timezone

from organizer.models import (
    AssignmentItem,
    CourseConfig,
    FolderImportPlan,
    FolderRule,
    IntegrationConnection,
    LearningActivity,
    LearningDigest,
    MoveEvent,
    ReviewItem,
    SubjectMemory,
    SubjectTheme,
    StudyGoal,
)

from . import makerere_curricula
from . import topics as topics_core

MUELE_BASE_URL = "https://muele.mak.ac.ug"


def is_makerere_profile(profile):
    return bool(profile and (profile.setup_path == "makerere" or "makerere" in profile.name.lower()))


def ensure_makerere_connection(profile):
    """Create a safe MUELE connection placeholder for Makerere profiles.

    No password or private token is stored in this row; the active token is
    kept in the OS keyring and the connection status reflects sync results.
    """
    if not is_makerere_profile(profile):
        return None

    connection, _ = IntegrationConnection.objects.get_or_create(
        profile=profile,
        provider="muele",
        display_name="Makerere MUELE",
        defaults={
            "base_url": MUELE_BASE_URL,
            "status": "planned",
            "config": {
                "platform": "Moodle",
                "sync_targets": ["course_files", "assignments", "calendar"],
                "credential_policy": "Store secrets in the operating system keyring, not in the database.",
            },
        },
    )
    if connection.base_url != MUELE_BASE_URL:
        connection.base_url = MUELE_BASE_URL
        connection.save(update_fields=["base_url", "updated_at"])
    return connection


def sync_subject_memories(profile):
    config = getattr(profile, "config", None)
    configured_codes = list(config.groups) if isinstance(config, CourseConfig) else []
    event_stats = {
        row["course_code"]: row
        for row in MoveEvent.objects.filter(profile=profile)
        .exclude(course_code__isnull=True)
        .exclude(course_code="")
        .values("course_code")
        .annotate(resource_count=Count("id"), last_touched_at=Max("timestamp"))
    }

    memories = []
    for code in sorted(set(configured_codes) | set(event_stats.keys())):
        stats = event_stats.get(code, {})
        name = makerere_curricula.name_for_code(code)
        memory, _ = SubjectMemory.objects.update_or_create(
            profile=profile,
            code=code,
            defaults={
                "title": f"{code} - {name}" if name else code,
                "resource_count": stats.get("resource_count", 0),
                "last_touched_at": stats.get("last_touched_at"),
            },
        )
        memories.append(memory)
    return memories


def ensure_default_goal(profile):
    title = "Keep this context organized"
    goal, _ = StudyGoal.objects.get_or_create(
        profile=profile,
        title=title,
        defaults={
            "status": "active",
            "notes": "Default goal created so every study context has a planning anchor.",
        },
    )
    return goal


def _theme_words(filename):
    stop_words = {
        "and", "the", "for", "with", "from", "into", "notes", "lecture",
        "assignment", "tutorial", "slides", "final", "draft", "copy",
    }
    stem = Path(filename).stem.lower()
    words = []
    for raw in stem.replace("_", " ").replace("-", " ").split():
        word = "".join(ch for ch in raw if ch.isalnum())
        if len(word) >= 4 and not word.isdigit() and word not in stop_words:
            words.append(word)
    return words[:6]


def sync_subject_themes(profile, limit=40):
    """Intelligent topic extraction for all subjects. Uses the new NLP + AI
    pipeline from topics.py, with the old simple word extraction as a fallback
    for subjects with very few files."""
    try:
        results = topics_core.process_all_subjects(profile)
        if results:
            return SubjectTheme.objects.filter(profile=profile)
    except Exception:
        pass

    # Fallback: old simple word extraction
    memories = {m.code: m for m in SubjectMemory.objects.filter(profile=profile)}
    events = (
        MoveEvent.objects.filter(profile=profile, success=True)
        .exclude(course_code__isnull=True)
        .exclude(course_code="")
        .order_by("-timestamp")[:limit]
    )
    touched = []
    for event in events:
        memory = memories.get(event.course_code)
        for word in _theme_words(event.filename):
            theme, created = SubjectTheme.objects.get_or_create(
                profile=profile,
                subject_code=event.course_code,
                name=word,
                defaults={
                    "subject_memory": memory,
                    "source": "filename",
                    "evidence": [event.filename],
                },
            )
            if not created:
                evidence = list(theme.evidence or [])
                if event.filename not in evidence:
                    evidence.append(event.filename)
                theme.evidence = evidence[-8:]
                theme.weight = max(theme.weight, len(theme.evidence))
                if memory and theme.subject_memory_id != memory.id:
                    theme.subject_memory = memory
                theme.save(update_fields=["evidence", "weight", "subject_memory", "last_seen_at"])
            touched.append(theme)
    return touched


def refresh_subject_topics(profile, subject_code=None, log=None):
    """Explicitly refresh topics for one or all subjects.
    Returns dict of subject_code -> theme_count."""
    if subject_code:
        themes = topics_core.process_subject_topics(profile, subject_code, log=log)
        return {subject_code: len(themes)}
    return topics_core.process_all_subjects(profile, log=log)


def schedule_review_seed(profile, limit=8):
    """Create first-pass review queue items from recent subject files."""
    now = timezone.now()
    events = (
        MoveEvent.objects.filter(profile=profile, success=True)
        .exclude(course_code__isnull=True)
        .exclude(course_code="")
        .order_by("-timestamp")[:limit]
    )
    created = []
    for event in events:
        if ReviewItem.objects.filter(profile=profile, move_event=event).exists():
            continue
        due_at = event.timestamp + timedelta(days=3)
        if due_at < now:
            due_at = now + timedelta(days=1)
        item = ReviewItem.objects.create(
            profile=profile,
            move_event=event,
            subject_code=event.course_code or "",
            title=f"Review {Path(event.filename).stem}",
            reason="Recently sorted study material",
            due_at=due_at,
            priority="normal",
            metadata={"source": "auto_seed"},
        )
        LearningActivity.objects.create(
            profile=profile,
            move_event=event,
            activity_type="review_scheduled",
            subject_code=event.course_code or "",
            title=f"Review scheduled for {event.filename}",
        )
        created.append(item)
    return created


def create_weekly_digest(profile):
    end = timezone.now()
    start = end - timedelta(days=7)
    events = MoveEvent.objects.filter(profile=profile, timestamp__gte=start, timestamp__lte=end)
    method_counts = list(events.values("method").annotate(total=Count("id")).order_by("-total"))
    subject_counts = list(
        events.exclude(course_code__isnull=True)
        .exclude(course_code="")
        .values("course_code")
        .annotate(total=Count("id"))
        .order_by("-total")[:8]
    )
    metrics = {
        "files_sorted": events.count(),
        "summaries_ready": events.filter(summary__isnull=False).count(),
        "unsorted_items": events.filter(method__in=["unsorted", "needs_sorting"]).count(),
        "methods": method_counts,
        "subjects": subject_counts,
        "reviews_due": ReviewItem.objects.filter(profile=profile, status="queued", due_at__lte=end).count(),
        "open_assignments": AssignmentItem.objects.filter(profile=profile, status="open").count(),
    }
    title = f"{profile.name} weekly study digest"
    lines = [
        f"{metrics['files_sorted']} files sorted in the last 7 days.",
        f"{metrics['summaries_ready']} summaries are ready.",
        f"{metrics['unsorted_items']} items still need sorting attention.",
        f"{metrics['reviews_due']} review items are due.",
        f"{metrics['open_assignments']} assignments are open.",
    ]
    if subject_counts:
        subjects = ", ".join(row["course_code"] for row in subject_counts[:4])
        lines.append(f"Most active subjects: {subjects}.")

    digest = LearningDigest.objects.create(
        profile=profile,
        period_start=start,
        period_end=end,
        title=title,
        content="\n".join(lines),
        metrics=metrics,
    )
    LearningActivity.objects.create(
        profile=profile,
        activity_type="digest_created",
        title=title,
        details=digest.content,
    )
    return digest


def create_rule_from_subject(profile, subject_code, extensions=None):
    extensions = extensions or ["pdf", "docx", "pptx"]
    rule, _ = FolderRule.objects.get_or_create(
        profile=profile,
        name=f"Route {subject_code}",
        defaults={
            "priority": 50,
            "match_field": "filename",
            "operator": "contains",
            "pattern": subject_code,
            "file_extensions": extensions,
            "subject_code": subject_code,
            "category": "Study Material",
            "destination_template": "{primary}/{secondary}/{subject}/{category}",
            "action": "route",
        },
    )
    return rule


def ensure_subject_rules(profile):
    config = getattr(profile, "config", None)
    codes = config.groups if isinstance(config, CourseConfig) else []
    return [create_rule_from_subject(profile, code) for code in codes]


def create_import_plan(root_path, profile=None, max_files=80):
    from . import file_index

    root = Path(root_path)
    plan = FolderImportPlan.objects.create(profile=profile, root_path=str(root), status="draft")
    if not root.exists() or not root.is_dir():
        plan.notes = "Folder does not exist or cannot be opened."
        plan.save(update_fields=["notes", "updated_at"])
        return plan

    folders = []
    files = []
    subjects = set()
    new_or_changed = 0
    already_indexed = 0
    for path in root.rglob("*"):
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            continue
        if path.is_dir():
            folders.append(relative)
            if path.name and not path.name.startswith("_"):
                subjects.add(path.name[:32])
            continue

        # Recorded regardless of the max_files display cap below, so a
        # folder far larger than that cap still gets full skip-detection
        # on its next scan -- not just whichever 80 files happened to be
        # the first ones this pass showed in the plan itself.
        if file_index.check_and_record(path, profile=profile):
            already_indexed += 1
        else:
            new_or_changed += 1
        if len(files) < max_files:
            files.append(relative)

    proposed_subjects = sorted(subjects)[:40]
    plan.discovered_folders = folders[:120]
    plan.discovered_files = files
    plan.proposed_subjects = proposed_subjects
    plan.proposed_rules = [
        {
            "name": f"Route {subject}",
            "match_field": "filename",
            "operator": "contains",
            "pattern": subject,
            "action": "route",
        }
        for subject in proposed_subjects[:20]
    ]
    plan.status = "scanned"
    if already_indexed:
        plan.notes = (
            f"{new_or_changed} new or changed file(s), {already_indexed} unchanged since a previous scan."
        )
    plan.save()
    return plan


def ensure_learning_foundation(profile):
    if not profile:
        return {
            "memories": [],
            "reviews_created": [],
            "muele_connection": None,
        }

    return {
        "memories": sync_subject_memories(profile),
        "themes": sync_subject_themes(profile),
        "reviews_created": schedule_review_seed(profile),
        "rules": ensure_subject_rules(profile),
        "goal": ensure_default_goal(profile),
        "muele_connection": ensure_makerere_connection(profile),
    }
