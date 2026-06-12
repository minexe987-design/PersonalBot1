# ──────────────────────────────────────────────────────────────────
# Personal Roblox Account Information Viewer
#
# Roblox rotates `.ROBLOSECURITY` on every authenticated response via
# Set-Cookie, and each rotation kills the previous value. To keep the
# user's cookie usable after running this command, we split endpoints
# into AUTH (needs cookie) and PUBLIC (no cookie). Public endpoints
# are hit with a plain, *cookieless* requests call — Roblox has
# nothing to rotate because we never sent the cookie in the first
# place. This mirrors the behavior of the site (lib/roblox-account.ts).
# ──────────────────────────────────────────────────────────────────

from core.utils import (
    ROBLOX_REQUEST_ERRORS,
    make_roblox_session,
    record_rotated_cookie,
    roblox_get,
    sanitize_cookie,
)


def _public_get(url: str, **kwargs):
    """GET with NO cookie attached — safe to spam without rotating the user's session."""
    return roblox_get(url, timeout=15, **kwargs)


def check_account(cookie: str) -> dict:
    """
    Retrieves public and private profile information for a Roblox account.
    Only AUTH-required endpoints send the cookie; everything else goes out
    cookieless so the user's session doesn't get rotated to death.

    Returns a dict with:
        success (bool), steps (list of str), error (str or None),
        and all the account data fields.
    """
    result = {
        "success": False,
        "steps": [],
        "error": None,
        "username": None,
        "display_name": None,
        "user_id": None,
        "avatar_url": None,
        "robux": None,
        "rap": None,
        "limiteds_count": None,
        "email": None,
        "email_verified": None,
        "two_fa_enabled": None,
        "two_fa_methods": None,
        "has_premium": None,
        "cookie_was_rotated": False,
        "rotated_cookie": None,
    }

    cookie = sanitize_cookie(cookie)

    if not cookie:
        result["error"] = "No cookie provided."
        return result

    # Auth session: ONLY used for endpoints that require .ROBLOSECURITY.
    auth = make_roblox_session(cookie)

    # ── AUTH: users/authenticated ────────────────────────────────
    result["steps"].append("🔍 Verifying cookie...")
    try:
        r = auth.get("https://users.roblox.com/v1/users/authenticated", timeout=10)
        if r.status_code == 401:
            result["error"] = "Invalid or expired cookie (401 Unauthorized)."
            result["steps"].append("<:x:1497344061592436737> Cookie is **INVALID**")
            return result
        r.raise_for_status()
        user = r.json()
        result["username"] = user.get("name")
        result["display_name"] = user.get("displayName")
        result["user_id"] = user.get("id")
        result["steps"].append(
            f"<:check:1497344035696672959> Authenticated as **{result['display_name']}** "
            f"(`{result['username']}` — ID: `{result['user_id']}`)"
        )
    except ROBLOX_REQUEST_ERRORS as e:
        result["error"] = f"Failed to verify cookie: {e}"
        return result

    user_id = result["user_id"]

    # ── PUBLIC: avatar thumbnail (no cookie) ─────────────────────
    try:
        r = _public_get(
            "https://thumbnails.roblox.com/v1/users/avatar-headshot",
            params={"userIds": user_id, "size": "150x150", "format": "Png"},
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            if data:
                result["avatar_url"] = data[0].get("imageUrl")
    except Exception:
        pass  # avatar is optional

    # ── AUTH: Robux balance ──────────────────────────────────────
    result["steps"].append("<a:moneybag:1497344054990733535> Fetching Robux balance...")
    try:
        r = auth.get(f"https://economy.roblox.com/v1/users/{user_id}/currency", timeout=10)
        if r.status_code == 200:
            result["robux"] = r.json().get("robux", 0)
            result["steps"].append(f"<:check:1497344035696672959> Robux: **R$ {result['robux']:,}**")
        else:
            result["robux"] = "N/A"
            result["steps"].append(f"<:warning:1497344059017003079> Could not fetch Robux (HTTP {r.status_code})")
    except Exception as e:
        result["robux"] = "N/A"
        result["steps"].append(f"<:warning:1497344059017003079> Robux fetch error: {e}")

    # ── PUBLIC: collectibles (RAP + limiteds count) — no cookie ──
    result["steps"].append("📦 Loading inventory (collectible items)...")
    try:
        rap_total = 0
        limiteds = 0
        cursor = ""
        for _page in range(5):  # cap at 5 pages like the site
            url = f"https://inventory.roblox.com/v1/users/{user_id}/assets/collectibles"
            params = {"sortOrder": "Asc", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            r = _public_get(url, params=params)
            if r.status_code != 200:
                result["steps"].append(f"<:warning:1497344059017003079> Inventory API returned HTTP {r.status_code}")
                break
            body = r.json()
            for item in body.get("data", []):
                rap_total += item.get("recentAveragePrice", 0) or 0
                limiteds += 1
            cursor = body.get("nextPageCursor")
            if not cursor:
                break

        result["rap"] = rap_total
        result["limiteds_count"] = limiteds
        result["steps"].append(f"<:check:1497344035696672959> Limiteds: **{limiteds:,}** — RAP: **R$ {rap_total:,}**")
    except Exception as e:
        result["rap"] = "N/A"
        result["limiteds_count"] = "N/A"
        result["steps"].append(f"<:warning:1497344059017003079> Inventory error: {e}")

    # ── AUTH: email settings ─────────────────────────────────────
    result["steps"].append("<:email:1497344042076344350> Checking email settings...")
    try:
        r = auth.get("https://accountsettings.roblox.com/v1/email", timeout=10)
        if r.status_code == 200:
            email_data = r.json()
            result["email"] = email_data.get("emailAddress", "Not set")
            result["email_verified"] = email_data.get("verified", False)
            verified_str = "<:check:1497344035696672959> Verified" if result["email_verified"] else "<:x:1497344061592436737> Not verified"
            display_email = result["email"] if result["email"] else "Not set"
            result["steps"].append(f"<:check:1497344035696672959> Email: `{display_email}` ({verified_str})")
        else:
            result["email"] = "N/A"
            result["email_verified"] = False
            result["steps"].append(f"<:warning:1497344059017003079> Email fetch returned HTTP {r.status_code}")
    except Exception as e:
        result["email"] = "N/A"
        result["email_verified"] = False
        result["steps"].append(f"<:warning:1497344059017003079> Email error: {e}")

    # ── AUTH: 2FA metadata ───────────────────────────────────────
    result["steps"].append("<:lock:1497344050078941344> Checking 2FA status...")
    try:
        r = auth.get("https://twostepverification.roblox.com/v1/metadata", timeout=10)
        if r.status_code == 200:
            meta = r.json()
            enabled = meta.get("twoStepVerificationEnabled", False)
            methods = []
            if meta.get("authenticatorEnabled"):
                methods.append("Authenticator")
            if meta.get("emailEnabled"):
                methods.append("Email")
            if meta.get("securityKeyEnabled"):
                methods.append("Security Key")

            result["two_fa_enabled"] = enabled
            result["two_fa_methods"] = ", ".join(methods) if methods else "None"

            status_str = "<:check:1497344035696672959> Enabled" if enabled else "<:x:1497344061592436737> Disabled"
            methods_str = f" ({result['two_fa_methods']})" if methods else ""
            result["steps"].append(f"<:check:1497344035696672959> 2FA: {status_str}{methods_str}")
        else:
            result["two_fa_enabled"] = None
            result["two_fa_methods"] = "N/A"
            result["steps"].append(f"<:warning:1497344059017003079> 2FA check returned HTTP {r.status_code}")
    except Exception as e:
        result["two_fa_enabled"] = None
        result["two_fa_methods"] = "N/A"
        result["steps"].append(f"<:warning:1497344059017003079> 2FA error: {e}")

    # ── PUBLIC: premium membership (no cookie) ───────────────────
    result["steps"].append("<a:crown:1497344039584923778> Checking premium status...")
    try:
        r = _public_get(
            f"https://premiumfeatures.roblox.com/v1/users/{user_id}/validate-membership"
        )
        if r.status_code == 200:
            body = r.json()
            result["has_premium"] = body is True
            prem_str = "<:check:1497344035696672959> Active" if result["has_premium"] else "<:x:1497344061592436737> Not active"
            result["steps"].append(f"<:check:1497344035696672959> Premium: {prem_str}")
        else:
            result["has_premium"] = None
            result["steps"].append(f"<:warning:1497344059017003079> Premium check returned HTTP {r.status_code}")
    except Exception as e:
        result["has_premium"] = None
        result["steps"].append(f"<:warning:1497344059017003079> Premium error: {e}")

    record_rotated_cookie(
        result,
        auth,
        cookie,
        "<:warning:1497344059017003079> Roblox rotated this cookie during account checks. Use the fresh cookie sent privately.",
    )

    result["success"] = True
    result["steps"].append("🎉 Account check **complete**!")
    return result
