"""
Personal Roblox Account Management Utility.

Roblox rotates .ROBLOSECURITY on authenticated responses. The only
safe cookie to return is whatever sits in the shared session jar
after the final authenticated request has completed.
"""

from core.utils import make_roblox_session, sanitize_cookie

PRIMARY_EMOJI = "<a:arrow:1497344031238127686>"
SECONDARY_EMOJI = "<:clipboard:1497344037294702762>"
SUCCESS_EMOJI = "<:check:1497344035696672959>"
WARNING_EMOJI = "<:warning:1497344059017003079>"


def refresh_cookie(cookie: str) -> dict:
    """
    Rotates a Roblox session token by ending all active sessions
    and generating a fresh token for the same account.

    Returns a dict with:
        success (bool), steps (list of str), new_cookie (str or None),
        username (str or None), user_id (int or None), error (str or None)
    """
    result = {
        "success": False,
        "steps": [],
        "new_cookie": None,
        "username": None,
        "user_id": None,
        "error": None,
    }

    cookie = sanitize_cookie(cookie)

    if not cookie:
        result["error"] = "No cookie provided."
        return result

    if not cookie.startswith("_|WARNING"):
        result["steps"].append(f"{WARNING_EMOJI} Cookie format looks unusual. Continuing anyway...")

    result["steps"].append(f"{PRIMARY_EMOJI} Cookie length: {len(cookie)} characters")

    session = make_roblox_session(cookie)

    verify_url = "https://users.roblox.com/v1/users/authenticated"
    try:
        verify_response = session.get(verify_url, timeout=10)
        if verify_response.status_code == 200:
            user_data = verify_response.json()
            result["username"] = user_data.get("name")
            result["user_id"] = user_data.get("id")
            result["steps"].append(
                f"{PRIMARY_EMOJI} Cookie is VALID - Account: {result['username']} (ID: {result['user_id']})"
            )
        else:
            result["error"] = f"Cookie is INVALID (HTTP {verify_response.status_code})"
            return result
    except Exception as e:
        result["error"] = f"Failed to verify cookie: {e}"
        return result

    csrf_url = "https://auth.roblox.com/v2/logout"
    try:
        csrf_response = session.post(csrf_url, json={}, timeout=10)
        csrf_token = csrf_response.headers.get("x-csrf-token")
        if not csrf_token:
            result["error"] = "No x-csrf-token in response headers"
            return result
        result["steps"].append(f"{PRIMARY_EMOJI} CSRF Token obtained")
    except Exception as e:
        result["error"] = f"CSRF request failed: {e}"
        return result

    logout_url = "https://auth.roblox.com/v2/logoutfromallsessionsandreauthenticate"
    headers = {
        "X-CSRF-TOKEN": csrf_token,
        "Content-Type": "application/json",
    }

    try:
        logout_response = session.post(logout_url, headers=headers, json={}, timeout=10)
        result["steps"].append(f"{SECONDARY_EMOJI} Logout response: HTTP {logout_response.status_code}")

        if logout_response.status_code == 200:
            new_cookie = session.cookies.get(".ROBLOSECURITY")
            if not new_cookie:
                result["error"] = "Session completed but Roblox did not return a fresh cookie."
                return result

            if new_cookie == cookie:
                result["error"] = "New cookie is identical to old - Roblox did not generate a new session."
                return result

            result["steps"].append(f"{SUCCESS_EMOJI} New cookie generated (different from original)")
            result["new_cookie"] = new_cookie
            result["success"] = True
            result["steps"].append(
                f"{SUCCESS_EMOJI} New cookie ready - returning session jar value without re-verify"
            )
        elif logout_response.status_code == 400:
            result["error"] = f"400 Bad Request - {logout_response.text or 'Endpoint may require different params'}"
        elif logout_response.status_code == 403:
            result["error"] = "403 Forbidden - CSRF token may be invalid"
        elif logout_response.status_code == 401:
            result["error"] = "401 Unauthorized - Cookie expired during process"
        else:
            detail = logout_response.text[:300] if logout_response.text else ""
            result["error"] = f"Unexpected status: {logout_response.status_code} - {detail}"

    except Exception as e:
        result["error"] = f"Logout request failed: {e}"

    return result
