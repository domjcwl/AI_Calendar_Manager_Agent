import os
import json
import pickle
import asyncio
import datetime

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
]

# Google Tasks API does not support device flow scopes directly, but the
# Tasks API accepts the same OAuth token granted for Calendar. We request
# only the calendar scope during device flow and reuse the token for tasks.

TOKENS_DIR        = "tokens"
CREDENTIALS_PATH  = "credentials.json"


def _token_path(user_id: int) -> str:
    """Return the token file path for a given Telegram user_id."""
    os.makedirs(TOKENS_DIR, exist_ok=True)
    return os.path.join(TOKENS_DIR, f"token_{user_id}.pickle")


def get_credentials(user_id: int) -> Credentials:
    """
    Load and refresh credentials for *user_id*.
    Raises RuntimeError('not_authorised') if not yet authorised.
    """
    path = _token_path(user_id)
    if not os.path.exists(path):
        raise RuntimeError("not_authorised")

    with open(path, "rb") as f:
        creds: Credentials = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(path, "wb") as f:
                pickle.dump(creds, f)
        else:
            raise RuntimeError("not_authorised")

    return creds


def get_calendar_service(user_id: int):
    """Return an authorised Google Calendar service for *user_id*."""
    creds = get_credentials(user_id)
    return build("calendar", "v3", credentials=creds)


def is_authorised(user_id: int) -> bool:
    """Return True if *user_id* has valid stored credentials."""
    try:
        get_credentials(user_id)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Device Authorization Flow  (no redirect / no local server needed)
# ---------------------------------------------------------------------------

def start_device_flow() -> dict:
    """
    Begin Google's Device Authorization Flow.
    Returns a dict with: device_code, user_code, verification_url, expires_in, interval.
    """
    import urllib.request
    import urllib.parse

    with open(CREDENTIALS_PATH) as f:
        client_cfg = json.load(f)

    cfg       = client_cfg.get("installed") or client_cfg.get("web")
    client_id = cfg["client_id"]

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "scope":     " ".join(SCOPES),
    }).encode()

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/device/code",
        data=data,
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


async def poll_device_flow(
    user_id: int,
    device_code: str,
    interval: int,
    expires_in: int,
) -> Credentials | None:
    """
    Poll Google's token endpoint until *user_id* approves or the code expires.
    Saves the token to tokens/token_{user_id}.pickle on success.
    Returns Credentials on success, None on expiry / denial.
    """
    import urllib.request
    import urllib.parse
    import urllib.error

    with open(CREDENTIALS_PATH) as f:
        client_cfg = json.load(f)

    cfg           = client_cfg.get("installed") or client_cfg.get("web")
    client_id     = cfg["client_id"]
    client_secret = cfg["client_secret"]

    deadline = asyncio.get_event_loop().time() + expires_in

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(interval)

        data = urllib.parse.urlencode({
            "client_id":     client_id,
            "client_secret": client_secret,
            "device_code":   device_code,
            "grant_type":    "urn:ietf:params:oauth:grant-type:device_code",
        }).encode()

        try:
            req = urllib.request.Request(
                "https://oauth2.googleapis.com/token",
                data=data,
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                token_data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = json.loads(e.read())
            err = error_body.get("error", "")
            if err == "authorization_pending":
                continue
            elif err == "slow_down":
                interval += 5
                continue
            else:
                return None

        expiry = datetime.datetime.utcnow() + datetime.timedelta(seconds=token_data["expires_in"])
        creds  = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=SCOPES,
            expiry=expiry,
        )
        path = _token_path(user_id)
        with open(path, "wb") as f:
            pickle.dump(creds, f)

        return creds

    return None  # timed out