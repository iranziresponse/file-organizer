"""Downloads files from MUELE courses and feeds them into Orch's
routing system. Runs as a background thread, polling MUELE on a
configurable interval.

Files come in through the same get_destination() logic as manual
downloads, so they land in the correct profile folder automatically
and are logged as MoveEvent entries with method="muele_sync".
"""

import hashlib
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Callable

import requests
from django.utils import timezone

from . import muele_api, rules as routing_rules
from .paths import BASE_DIR

logger = logging.getLogger("organizer.muele_downloader")

# How often to check for new files (in seconds)
_DEFAULT_POLL_INTERVAL = 30 * 60  # 30 minutes

# Where downloaded files land temporarily before routing
_DOWNLOAD_CACHE = BASE_DIR / "_muele_cache"

# Max retries for downloading a single file
_DOWNLOAD_MAX_RETRIES = 3
_DOWNLOAD_RETRY_DELAY = 3  # seconds


# ---------------------------------------------------------------------------
# Sync tracking
# ---------------------------------------------------------------------------

_SYNC_STATE_PATH = BASE_DIR / "_muele_sync_state.json"


def _load_sync_state() -> dict:
    try:
        import json

        if _SYNC_STATE_PATH.exists():
            return json.loads(_SYNC_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {"synced_files": [], "last_sync": None}


def _save_sync_state(state: dict) -> None:
    import json

    try:
        _SYNC_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _is_already_synced(course_id: int, filename: str) -> bool:
    key = f"{course_id}:{filename}"
    state = _load_sync_state()
    return key in state.get("synced_files", [])


def _mark_synced(course_id: int, filename: str) -> None:
    key = f"{course_id}:{filename}"
    state = _load_sync_state()
    synced = state.get("synced_files", [])
    if key not in synced:
        synced.append(key)
    state["synced_files"] = synced[-5000:]  # keep last 5000 entries
    state["last_sync"] = datetime.now().isoformat()
    _save_sync_state(state)


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------


def _file_fingerprint(file_url: str, file_size: int) -> str:
    """Create a fingerprint from URL + size to detect changed files."""
    return hashlib.md5(f"{file_url}:{file_size}".encode()).hexdigest()


def download_file(
    file_info: dict,
    profile,
    token: str | None = None,
    log: Callable | None = None,
) -> str | None:
    """Download a file from MUELE and route it through Orch's rules engine,
    into `profile`'s own folders. Takes the profile explicitly rather than
    reading Profile.get_active() -- this can run from a background sync
    loop syncing a profile that isn't the one currently active in the UI,
    and files must land in (and MoveEvents attribute to) the profile that
    was actually being synced, not whichever one happens to be active at
    the moment the download finishes.

    Returns the destination path string on success, None on failure.
    """
    filename = file_info.get("filename", "unnamed")
    file_url = file_info.get("fileurl", "")
    course_id = file_info.get("course_id")
    section_name = file_info.get("section_name", "General")

    if not file_url or not course_id:
        return None

    # Check if already synced
    if _is_already_synced(course_id, filename):
        return None

    # Download the file to the cache with retry logic
    _DOWNLOAD_CACHE.mkdir(parents=True, exist_ok=True)
    local_path = _DOWNLOAD_CACHE / filename

    last_error = None
    downloaded = False
    for attempt in range(_DOWNLOAD_MAX_RETRIES):
        try:
            resp = requests.get(file_url, timeout=120, stream=True, verify=muele_api.MUELE_CA_BUNDLE)
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            downloaded = True
            break
        except requests.Timeout:
            last_error = f"MUELE download timed out for '{filename}'"
            if log:
                log(f"{last_error} (attempt {attempt + 1}/{_DOWNLOAD_MAX_RETRIES})")
            if attempt < _DOWNLOAD_MAX_RETRIES - 1:
                time.sleep(_DOWNLOAD_RETRY_DELAY * (2 ** attempt))
            continue
        except requests.ConnectionError:
            last_error = f"MUELE connection lost for '{filename}'"
            if log:
                log(f"{last_error} (attempt {attempt + 1}/{_DOWNLOAD_MAX_RETRIES})")
            if attempt < _DOWNLOAD_MAX_RETRIES - 1:
                time.sleep(_DOWNLOAD_RETRY_DELAY * (2 ** attempt))
            continue
        except Exception as exc:
            last_error = f"MUELE download failed for '{filename}': {exc}"
            if log:
                log(last_error)
            return None

    if not downloaded:
        if log:
            log(last_error or f"MUELE download failed for '{filename}' after retries")
        return None

    if not local_path.exists() or local_path.stat().st_size == 0:
        if log:
            log(f"MUELE download empty for '{filename}'")
        local_path.unlink(missing_ok=True)
        return None

    # Route using the profile actually being synced, not Profile.get_active()
    from organizer.models import AppSettings

    profile_root = profile.root_path if profile else None
    settings = AppSettings.get_solo()

    dest = routing_rules.get_destination(
        local_path,
        profile_root=profile_root,
        library_inbox=Path(settings.library_inbox_path),
        ai_classify=None,
    )
    if dest is None:
        return None

    # If routing says it's a doc that goes to a profile path, use that
    # For files that match no profile subject, put them under the profile's
    # MUELE section instead of _Unsorted
    if profile and dest.method in ("unsorted", "needs_sorting") and profile_root:
        from . import paths as app_paths

        config = routing_rules.load_config(profile_root)
        if config:
            dest_path = (
                Path(profile_root)
                / config.get("primary_value", "_Unknown")
                / config.get("secondary_value", "_Unknown")
                / f"_MUELE_{section_name.replace(' ', '_')}"
                / filename
            )
        else:
            dest_path = (
                Path(profile_root)
                / "_MUELE"
                / section_name.replace(" ", "_")
                / filename
            )
    else:
        dest_path = dest.path / filename

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Handle name collisions
    if dest_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = dest_path.stem
        dest_path = dest_path.with_name(f"{stem}_{stamp}{dest_path.suffix}")

    try:
        os.rename(str(local_path), str(dest_path))
        _mark_synced(course_id, filename)

        # Record the move event
        _record_muele_event(
            profile=profile,
            filename=filename,
            source_path=str(file_url),
            destination_path=str(dest_path),
            course_code=str(course_id),
            success=True,
        )

        if log:
            log(f"MUELE downloaded '{filename}' -> {dest_path}")
        return str(dest_path)
    except OSError as exc:
        if log:
            log(f"MUELE failed to move '{filename}': {exc}")
        _record_muele_event(
            profile=profile,
            filename=filename,
            source_path=str(file_url),
            destination_path=str(dest_path),
            course_code=str(course_id),
            success=False,
            error_message=str(exc),
        )
        return None


def _record_muele_event(**fields) -> None:
    from organizer.models import MoveEvent

    try:
        MoveEvent.objects.create(method="muele_sync", **fields)
    except Exception as exc:
        logger.warning("Could not record MoveEvent for MUELE sync: %s", exc)


# ---------------------------------------------------------------------------
# Full sync for a profile
# ---------------------------------------------------------------------------


def sync_profile_courses(
    profile,
    token: str | None = None,
    course_ids: list[int] | None = None,
    log: Callable | None = None,
) -> dict:
    """Sync all enabled MUELE courses for a profile.

    Returns a summary dict: {downloaded, skipped, errors, courses_synced}.
    """
    from organizer.models import IntegrationConnection, MueleCourse

    result = {"downloaded": 0, "skipped": 0, "errors": 0, "courses_synced": 0}

    connection = IntegrationConnection.objects.filter(
        profile=profile, provider="muele", status="connected"
    ).first()
    if not connection:
        return result

    if token is None:
        token = muele_api.load_connection_token(connection)
    if not token:
        if log:
            log("MUELE sync skipped: no token in keyring")
        return result

    # Get courses the student wants to sync
    courses = MueleCourse.objects.filter(connection=connection, auto_download=True)
    if course_ids:
        courses = courses.filter(course_id__in=course_ids)

    for course in courses:
        try:
            files, error = muele_api.get_course_files(course.course_id, token=token, log=log)
            if error:
                if log:
                    log(f"MUELE sync error for '{course.course_name}': {error}")
                result["errors"] += 1
                continue

            for file_info in files:
                if _is_already_synced(course.course_id, file_info["filename"]):
                    result["skipped"] += 1
                    continue

                dest = download_file(file_info, profile, token=token, log=log)
                if dest:
                    result["downloaded"] += 1
                else:
                    result["errors"] += 1

            course.last_sync_at = timezone.now()
            course.save(update_fields=["last_sync_at"])
            result["courses_synced"] += 1

        except Exception as exc:
            if log:
                log(f"MUELE sync crashed for '{course.course_name}': {exc}")
            result["errors"] += 1

    # last_sync_at means "last time this genuinely worked", not "last time
    # a sync was attempted" -- so unstable campus internet showing a string
    # of errors doesn't quietly overwrite the last known-good timestamp
    # with "just now". A sync with zero errors updates it and clears the
    # error status; one with any errors leaves the old timestamp alone and
    # flags the connection so the UI can say "last successful sync was
    # <real date>, most recent attempt failed" instead of just lying with
    # a freshly-bumped clock.
    if result["errors"] == 0:
        connection.last_sync_at = timezone.now()
        connection.status = "connected"
        connection.save(update_fields=["last_sync_at", "status", "updated_at"])
    else:
        connection.status = "error"
        connection.save(update_fields=["status", "updated_at"])

    return result


# ---------------------------------------------------------------------------
# Assignment sync
# ---------------------------------------------------------------------------


def sync_assignments(
    profile,
    token: str | None = None,
    log: Callable | None = None,
) -> int:
    """Sync MUELE assignments into AssignmentItem records.

    Returns the number of assignments created or updated.
    """
    from django.utils import timezone

    from organizer.models import AssignmentItem, IntegrationConnection, LearningActivity

    connection = IntegrationConnection.objects.filter(
        profile=profile, provider="muele", status="connected"
    ).first()
    if not connection:
        return 0

    if token is None:
        token = muele_api.load_connection_token(connection)
    if not token:
        return 0

    assignments, error = muele_api.get_assignments(token=token, log=log)
    if error:
        return 0

    now = timezone.now()
    count = 0

    for assign in assignments:
        due_date = assign.get("duedate")
        if due_date:
            due_aware = due_date.replace(tzinfo=timezone.utc) if timezone.is_aware(now) else due_date
        else:
            due_aware = None

        _, created = AssignmentItem.objects.update_or_create(
            profile=profile,
            source="muele",
            source_url=f"https://muele.mak.ac.ug/mod/assign/view.php?id={assign['cmid']}",
            title=assign["name"],
            defaults={
                "subject_code": assign.get("course_name", "")[:32],
                "due_at": due_aware,
                "status": "open" if (due_aware is None or due_aware > now) else "missed",
                "notes": assign.get("intro", "")[:240],
            },
        )
        if created:
            LearningActivity.objects.create(
                profile=profile,
                activity_type="muele_sync",
                subject_code=assign.get("course_name", "")[:32],
                title=f"New assignment: {assign['name']}",
                details=f"Due: {due_aware.strftime('%Y-%m-%d %H:%M') if due_aware else 'No deadline'}",
                metadata={"course_id": assign.get("course_id"), "assignment_id": assign.get("id")},
            )
            count += 1

    # Log the sync activity
    if count > 0:
        LearningActivity.objects.create(
            profile=profile,
            activity_type="muele_sync",
            title=f"MUELE sync: {count} new assignment(s)",
        )
        from . import notifications

        notifications.notify_new_items(
            "New MUELE assignments",
            "MUELE assignment",
            count,
            profile=profile,
        )

    return count


# ---------------------------------------------------------------------------
# Background sync loop
# ---------------------------------------------------------------------------


def run_muele_sync(
    stop_event: Event | None = None,
    poll_seconds: int = _DEFAULT_POLL_INTERVAL,
    log: Callable | None = None,
) -> None:
    """Blocking background loop that periodically syncs MUELE.

    Designed to run on a daemon thread, same pattern as the file watcher.
    """
    from organizer.models import IntegrationConnection, Profile

    def _log(msg):
        if log:
            log(msg)

    _log("MUELE sync daemon started")

    while stop_event is None or not stop_event.is_set():
        try:
            # Find all profiles with connected MUELE integrations. Each
            # connection's token is its own -- loaded per-iteration below,
            # never shared across profiles (see load_connection_token).
            connections = IntegrationConnection.objects.filter(
                provider="muele", status="connected"
            ).select_related("profile")

            for connection in connections:
                profile = connection.profile
                if not profile:
                    continue

                token = muele_api.load_connection_token(connection)
                if not token:
                    _log(f"MUELE sync skipped for '{profile.name}': no token configured")
                    continue

                _log(f"MUELE syncing profile: {profile.name}")

                # Sync files
                file_result = sync_profile_courses(profile, token=token, log=_log)
                _log(
                    f"MUELE file sync for '{profile.name}': "
                    f"{file_result['downloaded']} downloaded, "
                    f"{file_result['skipped']} skipped, "
                    f"{file_result['errors']} errors"
                )

                # Sync assignments
                assign_count = sync_assignments(profile, token=token, log=_log)
                if assign_count:
                    _log(f"MUELE assignment sync for '{profile.name}': {assign_count} new")

        except Exception as exc:
            _log(f"MUELE sync loop error: {exc}")

        if stop_event is not None:
            stop_event.wait(poll_seconds)
        else:
            time.sleep(poll_seconds)

    _log("MUELE sync daemon stopped")