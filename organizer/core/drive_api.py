"""Google Drive backup: after Orch sorts a file, optionally upload a copy
to a "Orch Backups" folder in the user's own Drive.

Uses the OAuth2 "installed app" loopback flow directly against Google's
REST endpoints (no google-api-python-client dependency, same "plain
requests" approach as muele_api.py/youtube_api.py) -- the consent screen
opens in the user's browser, Google redirects back to Orch's own local
server (it's already listening on 127.0.0.1:8765) with an authorization
code, which gets exchanged for tokens.

Scope is drive.file only: Orch can only see/manage files it creates
itself, never the rest of the user's Drive. Client ID/secret (not
meaningfully secret for an installed app, per Google's own docs) live in
DRIVE_CONFIG_PATH; the refresh token, which really is sensitive, lives in
the OS keyring, same split MUELE's token uses.

Never allowed to throw or block whatever called it: any failure (no
network, expired/revoked auth, quota) logs and returns, so a backup
failure can never stop a real file move from completing.
"""

import json
import logging
import mimetypes
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from . import paths

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
SCOPE = "https://www.googleapis.com/auth/drive.file"
BACKUP_FOLDER_NAME = "Orch Backups"

_DEFAULT_TIMEOUT = 30
_KEYRING_SERVICE = "iranzi-file-organizer-drive"

logger = logging.getLogger("organizer.drive")

# In-memory only -- an access token is short-lived (~1h) and cheap to
# re-derive from the refresh token, so there's no need to persist it.
_access_token_cache = {"token": None, "expires_at": 0}
_storage_quota_cache = {"value": None, "updated_at": 0.0, "refreshing": False}
_storage_quota_lock = threading.Lock()
_STORAGE_QUOTA_TTL = 900


# ---------------------------------------------------------------------------
# Client config (not meaningfully secret) and refresh token (is) via keyring
# ---------------------------------------------------------------------------


def load_drive_config() -> dict | None:
    if not paths.DRIVE_CONFIG_PATH.exists():
        return None
    try:
        return json.loads(paths.DRIVE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def store_refresh_token(token: str) -> tuple[bool, str | None]:
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, "refresh_token", token)
        return True, None
    except ImportError:
        logger.warning("keyring package not installed; cannot store Drive refresh token.")
        return False, "The keyring package is not installed, so the connection can't be saved securely."
    except Exception as exc:
        logger.warning("Could not store Drive refresh token in the OS keyring: %s", exc)
        return False, f"Could not save the connection to your OS credential store: {exc}"


def load_refresh_token() -> str | None:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, "refresh_token")
    except ImportError:
        logger.warning("keyring package not installed; treating Drive as disconnected.")
        return None
    except Exception as exc:
        logger.warning("Could not read Drive refresh token from the OS keyring: %s", exc)
        return None


def clear_refresh_token() -> None:
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, "refresh_token")
    except ImportError:
        logger.warning("keyring package not installed; nothing to clear.")
    except Exception as exc:
        logger.warning("Could not clear Drive refresh token from the OS keyring: %s", exc)
    _access_token_cache["token"] = None
    _access_token_cache["expires_at"] = 0


def is_connected() -> bool:
    return bool(load_refresh_token())


# ---------------------------------------------------------------------------
# OAuth2 loopback flow
# ---------------------------------------------------------------------------


def build_auth_url(redirect_uri: str, state: str) -> str | None:
    config = load_drive_config()
    if not config or not config.get("client_id"):
        return None
    params = {
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        # Without this, Google only issues a refresh token the very first
        # time a given Google account authorizes this client -- a
        # reconnect after disconnecting would silently get no refresh
        # token at all. Always prompting for consent keeps reconnect
        # working every time.
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{requests.compat.urlencode(params)}"


def exchange_code_for_tokens(code: str, redirect_uri: str) -> tuple[dict | None, str | None]:
    config = load_drive_config()
    if not config or not config.get("client_id") or not config.get("client_secret"):
        return None, "Google Drive isn't configured yet."

    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json(), None
    except requests.RequestException as exc:
        logger.warning("Drive token exchange failed: %s", exc)
        return None, f"Google rejected the connection attempt: {exc}"


def get_valid_access_token(log: Callable | None = None) -> str | None:
    """Returns a usable access token, refreshing it if the cached one has
    expired. None if not connected or the refresh itself fails (e.g. the
    user revoked access from their Google Account settings)."""
    now = time.time()
    if _access_token_cache["token"] and now < _access_token_cache["expires_at"]:
        return _access_token_cache["token"]

    refresh_token = load_refresh_token()
    config = load_drive_config()
    if not refresh_token or not config or not config.get("client_id"):
        return None

    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": config["client_id"],
                "client_secret": config.get("client_secret", ""),
                "grant_type": "refresh_token",
            },
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        logger.warning("Drive access token refresh failed: %s", exc)
        if log:
            log(f"Google Drive backup skipped: could not refresh access ({exc}).")
        return None

    token = payload.get("access_token")
    if not token:
        return None
    _access_token_cache["token"] = token
    # Refresh a little early rather than right at expiry.
    _access_token_cache["expires_at"] = now + payload.get("expires_in", 3600) - 60
    return token


def get_account_email(access_token: str) -> str | None:
    try:
        resp = requests.get(
            ABOUT_URL,
            params={"fields": "user(emailAddress)"},
            headers={"Authorization": "Bearer " + access_token},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("user", {}).get("emailAddress")
    except requests.RequestException as exc:
        logger.warning("Could not read the connected Drive account: %s", exc)
        return None


def _refresh_storage_quota(profile) -> None:
    from django.db import close_old_connections

    close_old_connections()
    try:
        access_token = get_valid_access_token()
        if not access_token:
            quota = None
        else:
            response = requests.get(
                ABOUT_URL,
                params={"fields": "storageQuota(usage,limit)"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5,
            )
            response.raise_for_status()
            quota_data = response.json().get("storageQuota", {})
            usage = int(quota_data.get("usage", 0))
            limit = int(quota_data["limit"]) if quota_data.get("limit") else None
            quota = {"usage": usage, "limit": limit}
            if limit:
                quota["percent"] = min(100, round(usage * 100 / limit))
                quota["remaining"] = max(0, limit - usage)

        with _storage_quota_lock:
            _storage_quota_cache["value"] = quota
            _storage_quota_cache["updated_at"] = time.time()

        if quota and quota.get("percent", 0) >= 90 and profile:
            _notify_storage_warning(profile, quota)
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        logger.warning("Could not verify Google Drive storage usage: %s", exc)
        with _storage_quota_lock:
            _storage_quota_cache["value"] = None
            _storage_quota_cache["updated_at"] = time.time()
    except Exception:
        logger.exception("Unexpected error while refreshing Google Drive storage usage.")
        with _storage_quota_lock:
            _storage_quota_cache["value"] = None
            _storage_quota_cache["updated_at"] = time.time()
    finally:
        with _storage_quota_lock:
            _storage_quota_cache["refreshing"] = False
        close_old_connections()


def _notify_storage_warning(profile, quota: dict) -> None:
    from datetime import timedelta

    from django.utils import timezone

    from organizer.models import Notification

    title = "Google Drive storage is almost full"
    if Notification.objects.filter(
        profile=profile,
        title=title,
        created_at__gte=timezone.now() - timedelta(hours=24),
    ).exists():
        return

    used_percent = quota["percent"]
    if used_percent >= 100:
        message = "Drive is full. Free space, then retry the pending Orch backups."
    else:
        message = (
            f"Drive storage is {used_percent}% used. Free space before pending "
            "Orch backups need attention."
        )
    from . import notifications

    notifications.notify(title, message, urgency="critical", profile=profile)


def storage_quota_snapshot(profile=None) -> dict | None:
    """Return cached Drive usage and refresh it off the dashboard request path."""
    if not is_connected():
        return None

    with _storage_quota_lock:
        stale = time.time() - _storage_quota_cache["updated_at"] >= _STORAGE_QUOTA_TTL
        if stale and not _storage_quota_cache["refreshing"]:
            _storage_quota_cache["refreshing"] = True
            threading.Thread(
                target=_refresh_storage_quota,
                args=(profile,),
                daemon=True,
                name="drive-storage-quota",
            ).start()
        return _storage_quota_cache["value"]


# ---------------------------------------------------------------------------
# Backup upload
# ---------------------------------------------------------------------------


def _ensure_backup_folder(access_token: str) -> str | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        search = requests.get(
            FILES_URL,
            headers=headers,
            params={
                "q": (
                    f"name='{BACKUP_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' "
                    "and trashed=false"
                ),
                "fields": "files(id)",
                "spaces": "drive",
            },
            timeout=_DEFAULT_TIMEOUT,
        )
        search.raise_for_status()
        existing = search.json().get("files", [])
        if existing:
            return existing[0]["id"]

        created = requests.post(
            FILES_URL,
            headers=headers,
            json={"name": BACKUP_FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"},
            timeout=_DEFAULT_TIMEOUT,
        )
        created.raise_for_status()
        return created.json().get("id")
    except requests.RequestException as exc:
        logger.warning("Could not find/create the Drive backup folder: %s", exc)
        return None


def backup_file(file_path: str, log: Callable | None = None) -> bool:
    """Uploads one already-sorted file to the "Orch Backups" Drive folder.
    Returns False (never raises) on any failure -- not configured, not
    connected, offline, quota exceeded, whatever -- so this can always be
    called fire-and-forget right after a real move completes."""
    config = load_drive_config()
    if not config or not config.get("enabled"):
        return False

    path = Path(file_path)
    if not path.exists():
        return False

    access_token = get_valid_access_token(log=log)
    if not access_token:
        return False

    folder_id = _ensure_backup_folder(access_token)
    if not folder_id:
        return False

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    metadata = {"name": path.name, "parents": [folder_id]}

    try:
        with path.open("rb") as fh:
            resp = requests.post(
                UPLOAD_URL,
                params={"uploadType": "multipart"},
                headers={"Authorization": f"Bearer {access_token}"},
                files={
                    "metadata": (None, json.dumps(metadata), "application/json"),
                    "file": (path.name, fh, mime_type),
                },
                timeout=120,
            )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Drive backup upload failed for %s: %s", path.name, exc)
        if log:
            log(f"Google Drive backup failed for {path.name}: {exc}")
        return False

    if log:
        log(f"Backed up {path.name} to Google Drive.")
    return True
