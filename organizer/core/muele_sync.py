"""Real MUELE integration — course import, timetable, assignments,
resource sync, and semester-aware routing.

This module provides the actual sync operations that connect to
https://muele.mak.ac.ug via Moodle's REST API.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from django.db import transaction
from django.utils import timezone

from . import muele_api, muele_calendar


def import_courses_for_profile(profile, token: str | None = None, log: Callable | None = None) -> dict:
    """Import courses from MUELE into the profile's subject list.

    Fetches the user's enrolled courses from MUELE and adds them as
    subjects in the profile's CourseConfig. Also creates MueleCourse
    records for each course found.

    Returns: {imported: int, total: int, errors: list}
    """
    from organizer.models import CourseConfig, IntegrationConnection, MueleCourse

    result = {"imported": 0, "total": 0, "errors": []}

    # Get or create the connection
    connection, _ = IntegrationConnection.objects.get_or_create(
        profile=profile,
        provider="muele",
        defaults={
            "display_name": "Makerere MUELE",
            "base_url": muele_api.MUELE_BASE_URL,
            "status": "configured",
        },
    )

    if token is None:
        token = muele_api.load_connection_token(connection)
    if not token:
        connection.status = "error"
        connection.save(update_fields=["status", "updated_at"])
        result["errors"].append("No MUELE token configured")
        return result

    # Fetch courses from MUELE
    courses, error = muele_api.get_courses(token=token, log=log)
    if error:
        connection.status = "error"
        connection.save(update_fields=["status", "updated_at"])
        result["errors"].append(error)
        return result

    if any(not isinstance(course, dict) or course.get("id") is None for course in courses):
        connection.status = "error"
        connection.save(update_fields=["status", "updated_at"])
        result["errors"].append("MUELE returned a course without an ID.")
        return result

    result["total"] = len(courses)

    with transaction.atomic():
        config, _ = CourseConfig.objects.get_or_create(
            profile=profile,
            defaults={
                "primary_value": muele_calendar.get_current_year(),
                "secondary_value": muele_calendar.get_current_semester(),
                "groups": [],
            },
        )

        existing_codes = set(config.groups or [])

        for course in courses:
            code = course.get("shortname", "").strip().upper()
            name = course.get("fullname", "")

            MueleCourse.objects.update_or_create(
                connection=connection,
                course_id=course["id"],
                defaults={
                    "course_name": name,
                    "course_code": code,
                    "auto_download": True,
                    "enrolled": True,
                },
            )

            if code and code not in existing_codes:
                existing_codes.add(code)
                result["imported"] += 1
                if log:
                    log(f"Imported course: {code} - {name}")

        config.groups = sorted(existing_codes)
        config.primary_value = muele_calendar.get_current_year()
        config.secondary_value = muele_calendar.get_current_semester()
        config.save()
        connection.status = "connected"
        connection.last_sync_at = timezone.now()
        connection.save(update_fields=["status", "last_sync_at", "updated_at"])

    if log:
        log(f"Course import complete: {result['imported']} new, {result['total']} total")

    return result


def sync_assignments_from_muele(profile, token: str | None = None, log: Callable | None = None) -> dict:
    """Sync assignments from MUELE into AssignmentItem records.

    Fetches all assignments from MUELE for the profile's enrolled courses
    and creates/updates AssignmentItem records with due dates.

    Returns: {created: int, updated: int, errors: list}
    """
    from organizer.models import AssignmentItem, IntegrationConnection, LearningActivity

    result = {"created": 0, "updated": 0, "errors": []}

    if token is None:
        connection = IntegrationConnection.objects.filter(profile=profile, provider="muele").first()
        token = muele_api.load_connection_token(connection) if connection else None
    if not token:
        result["errors"].append("No MUELE token configured")
        return result

    # Get assignments from MUELE
    assignments, error = muele_api.get_assignments(token=token, log=log)
    if error:
        result["errors"].append(error)
        return result

    now = timezone.now()

    for assign in assignments:
        course_code = assign.get("course_name", "")[:32]
        due_date = assign.get("duedate")
        if due_date:
            due_aware = due_date.replace(tzinfo=timezone.utc) if timezone.is_aware(now) else due_date
        else:
            due_aware = None

        source_url = f"https://muele.mak.ac.ug/mod/assign/view.php?id={assign['cmid']}"

        _, created = AssignmentItem.objects.update_or_create(
            profile=profile,
            source="muele",
            source_url=source_url,
            title=assign["name"],
            defaults={
                "subject_code": course_code,
                "due_at": due_aware,
                "status": "open" if (due_aware is None or due_aware > now) else "missed",
                "notes": assign.get("intro", "")[:240],
            },
        )

        if created:
            result["created"] += 1
            LearningActivity.objects.create(
                profile=profile,
                activity_type="muele_sync",
                subject_code=course_code,
                title=f"New MUELE assignment: {assign['name']}",
                details=f"Due: {due_aware.strftime('%Y-%m-%d %H:%M') if due_aware else 'No deadline'}",
            )
        else:
            result["updated"] += 1

    if result["created"]:
        from . import notifications

        notifications.notify_new_items(
            "New MUELE assignments",
            "MUELE assignment",
            result["created"],
            profile=profile,
        )

    if log:
        log(f"Assignment sync: {result['created']} created, {result['updated']} updated")

    return result


def sync_course_files(profile, token: str | None = None, log: Callable | None = None) -> dict:
    """Download course files from MUELE for auto-download enabled courses.

    Returns: {downloaded: int, skipped: int, errors: int}
    """
    from organizer.models import IntegrationConnection, MueleCourse

    result = {"downloaded": 0, "skipped": 0, "errors": 0}

    connection = IntegrationConnection.objects.filter(
        profile=profile, provider="muele", status="connected"
    ).first()
    if not connection:
        result["errors"] = 1
        return result

    if token is None:
        token = muele_api.load_connection_token(connection)
    if not token:
        result["errors"] = 1
        return result

    courses = MueleCourse.objects.filter(connection=connection, auto_download=True)

    for course in courses:
        files, error = muele_api.get_course_files(course.course_id, token=token, log=log)
        if error:
            result["errors"] += 1
            if log:
                log(f"Failed to fetch files for {course.course_name}: {error}")
            continue

        for file_info in files:
            from .muele_downloader import download_file
            dest = download_file(file_info, profile, token=token, log=log)
            if dest:
                result["downloaded"] += 1
            else:
                result["skipped"] += 1

        course.last_sync_at = timezone.now()
        course.save(update_fields=["last_sync_at"])

    if connection:
        connection.last_sync_at = timezone.now()
        connection.save(update_fields=["last_sync_at", "updated_at"])

    return result


def get_muele_status(profile) -> dict:
    """Get the current MUELE sync status for a profile."""
    from organizer.models import AssignmentItem, IntegrationConnection, MueleCourse

    connection = IntegrationConnection.objects.filter(
        profile=profile, provider="muele"
    ).first()

    if not connection:
        return {"connected": False}

    courses = MueleCourse.objects.filter(connection=connection)
    assignments = AssignmentItem.objects.filter(profile=profile, source="muele")

    # Calendar info
    today = timezone.now().date()
    current_period = muele_calendar.get_current_period(today)
    semester_weeks = muele_calendar.get_semester_weeks(today)
    remaining_weeks = muele_calendar.get_remaining_weeks(today)

    return {
        "connected": connection.status == "connected",
        "last_sync": connection.last_sync_at,
        "courses": {
            "total": courses.count(),
            "auto_download": courses.filter(auto_download=True).count(),
        },
        "assignments": {
            "total": assignments.count(),
            "open": assignments.filter(status="open").count(),
            "missed": assignments.filter(status="missed").count(),
        },
        "calendar": {
            "semester": muele_calendar.get_current_semester(today),
            "academic_year": muele_calendar.get_academic_year_range(today),
            "week": semester_weeks,
            "weeks_remaining": remaining_weeks,
            "is_exam_period": muele_calendar.is_exam_period(today),
            "is_holiday": muele_calendar.is_holiday(today),
            "current_period": current_period.name if current_period else None,
            "upcoming": [
                {"name": p.name, "start": p.start.isoformat(), "end": p.end.isoformat()}
                for p in muele_calendar.get_upcoming_periods(today, count=3)
            ],
        },
    }