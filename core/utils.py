# ──────────────────────────────────────────────────────────────────
# Shared input helpers
# ──────────────────────────────────────────────────────────────────

import hashlib
import threading

from curl_cffi import requests as curl_requests

# Full Chrome-like header set. Roblox's fraud system flags requests that
# mint/use cookies with browser-incomplete OR stale-version fingerprints.
#
# MAINTENANCE NOTE: Chrome auto-updates every ~4 weeks. If Roblox starts
# invalidating cookies again "out of nowhere," it's almost always because
# Chrome stable has moved on and our pinned version looks suspicious.
# Bump CHROME_MAJOR below to whatever the current Chrome stable major is
# (check https://chromiumdash.appspot.com/releases?platform=Windows).
# Updating that single number cascades into User-Agent + sec-ch-ua.
CHROME_MAJOR = 147
ROBLOX_IMPERSONATE = "chrome"
ROBLOX_REQUEST_ERRORS = (curl_requests.RequestsError,)
_COOKIE_LOCKS: dict[str, threading.RLock] = {}
_COOKIE_LOCKS_GUARD = threading.Lock()

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.roblox.com",
    "Referer": "https://www.roblox.com/",
    "sec-ch-ua": (
        f'"Google Chrome";v="{CHROME_MAJOR}", '
        f'"Chromium";v="{CHROME_MAJOR}", '
        '"Not_A Brand";v="24"'
    ),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Priority": "u=1, i",
}


def _merge_browser_headers(headers: dict | None = None) -> dict:
    merged = dict(BROWSER_HEADERS)
    if headers:
        merged.update(headers)
    return merged


def roblox_get(url: str, **kwargs):
    kwargs["headers"] = _merge_browser_headers(kwargs.pop("headers", None))
    kwargs.setdefault("impersonate", ROBLOX_IMPERSONATE)
    return curl_requests.get(url, **kwargs)


def roblox_post(url: str, **kwargs):
    kwargs["headers"] = _merge_browser_headers(kwargs.pop("headers", None))
    kwargs.setdefault("impersonate", ROBLOX_IMPERSONATE)
    return curl_requests.post(url, **kwargs)


def _cookie_lock(cookie: str) -> threading.RLock:
    key = hashlib.sha256((cookie or "").encode("utf-8")).hexdigest()
    with _COOKIE_LOCKS_GUARD:
        lock = _COOKIE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _COOKIE_LOCKS[key] = lock
        return lock


def run_with_cookie_lock(cookie: str, func, *args, **kwargs):
    with _cookie_lock(cookie):
        return func(*args, **kwargs)


def make_roblox_session(cookie: str | None = None) -> curl_requests.Session:
    """
    Build a curl_cffi Session pre-configured for Roblox API calls.

    Use this for EVERY authenticated Roblox call. Centralizing here means
    when Chrome version drifts again (and it will), we only update
    BROWSER_HEADERS / CHROME_MAJOR above and every command picks it up.

    Pass `cookie` to seed the .ROBLOSECURITY cookie in the session jar.
    Roblox rotates the cookie on auth responses; the session jar absorbs
    the rotation and the freshest value can be read with
    session.cookies.get(".ROBLOSECURITY") after the last auth call.
    """
    session = curl_requests.Session(impersonate=ROBLOX_IMPERSONATE)
    session.headers.update(BROWSER_HEADERS)
    if cookie:
        session.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
    return session


def sanitize_cookie(cookie: str) -> str:
    """
    Normalize a user-provided cookie string.

    Accepts cookies wrapped in any number of backticks (e.g. ``cookie``
    or ```cookie```), strips whitespace, and removes a leading
    `.ROBLOSECURITY=` prefix if present.
    """
    if cookie is None:
        return ""

    cookie = cookie.strip()

    # Peel off matching backtick wrappers (handles ``...``, ```...```, etc.)
    while len(cookie) >= 2 and cookie.startswith("`") and cookie.endswith("`"):
        cookie = cookie[1:-1].strip()

    if cookie.startswith(".ROBLOSECURITY="):
        cookie = cookie[len(".ROBLOSECURITY="):].strip()

    return cookie


def record_rotated_cookie(result: dict, session, original_cookie: str, step_message: str = None) -> None:
    """
    Capture the freshest .ROBLOSECURITY value from a session if Roblox
    rotated it during authenticated requests.
    """
    if not result or session is None:
        return

    try:
        latest_cookie = session.cookies.get(".ROBLOSECURITY")
    except Exception:
        latest_cookie = None

    if not latest_cookie or latest_cookie == original_cookie:
        return

    result["cookie_was_rotated"] = True
    result["rotated_cookie"] = latest_cookie

    if step_message:
        steps = result.setdefault("steps", [])
        if step_message not in steps:
            steps.append(step_message)
