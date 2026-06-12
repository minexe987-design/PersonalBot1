# ──────────────────────────────────────────────────────────────────
# Multi-token Discord user-account pool.
#
# The previous design used ONE user token to scan ~30 cheating
# servers per /in-cheating-servers call. Discord's anti-self-bot
# heuristics flagged the traffic shape (uniform 0.25s pacing, single
# UA, sequential REST against many guilds) and suspended the account.
#
# This pool fixes that by:
#   * Sharding the watched servers across scanner tokens —
#     each token only ever sees its own subset, so per-token
#     traffic is light and the work fans out in parallel.
#   * A dedicated token for /bancheckv2 + /reportercheck so the
#     riskier whole-channel-list endpoint is isolated. If a scanner
#     token dies, bancheck stays alive, and vice versa.
#   * Per-token STABLE Chrome-shaped identity (UA, sec-ch-ua,
#     X-Super-Properties, locale) chosen at import time. Real
#     clients don't shuffle UA mid-session — randomizing per-request
#     is itself a fingerprint, so we DON'T do that.
#   * Per-token Lock so concurrent commands can't double-spend a
#     token, and 429 Retry-After is respected per-token.
#   * 401 → token marked permanently dead, admin alerted, dropped
#     from rotation until manually replaced.
#
# Maintenance:
#   * To bump client_build_number: open discord.com in a browser,
#     devtools → Network → any "science" request → copy the
#     X-Super-Properties header, base64-decode it, copy the
#     client_build_number, paste below.
#   * To add a server: extend CHEATING_SERVERS in
#     commands/cheating_servers_cmd.py AND TOKEN_SERVER_RANGES below
#     so the index map stays aligned.
# ──────────────────────────────────────────────────────────────────

from __future__ import annotations

import base64
import json
import os
import random
import threading
import time
from typing import Optional
from urllib.parse import urlencode

import requests

from core.logging import log_admin_alert


MAX_HISTORY_MESSAGE_RESULTS = int(os.environ.get("CHEATING_HISTORY_MESSAGE_LIMIT", "1000"))
CHANNEL_CACHE_SECONDS = 600.0
_CHANNEL_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}


# ── Server → token mapping (1-indexed positions in CHEATING_SERVERS) ──
# Each scanner token is in a disjoint subset of the active cheating servers.
# The numbers are 1-indexed positions in the canonical CHEATING_SERVERS
# list (see commands/cheating_servers_cmd.py).
TOKEN_SERVER_RANGES: dict[str, list[int]] = {
    "MOD_DISCORD_USER_TOKEN_1": [1, 2, 3, 4, 5, 6, 7, 8],
    "MOD_DISCORD_USER_TOKEN_2": [9, 10, 11, 12, 13, 14],
    "MOD_DISCORD_USER_TOKEN_3": [17, 18, 19, 20, 22, 23],
    "MOD_DISCORD_USER_TOKEN_4": [24, 25, 26, 27, 28, 29],
    "MOD_DISCORD_USER_TOKEN_5": [30, 31, 32, 35, 41, 42, 43],
    "MOD_DISCORD_USER_TOKEN_6": [36, 37, 39, 44, 45, 46],
}

# These guilds stay in the canonical watch list, but no current scanner
# account can access them. Add them back to TOKEN_SERVER_RANGES after an
# account joins them again.
TEMP_UNCOVERED_GUILD_IDS: set[str] = {
    "1324451703302651976",  # 15 - Cryptic Studios
    "1069840556307525772",  # 16 - 1 F0 : Community (Vega X)
    "1254157599457415208",  # 21 - Ronin
    "1347244153305825374",  # 33 - bunni.fun
    "1169780008374521856",  # 34 - WeAreDevs
    "1454779122667491445",  # 38 - Falcon
    "942431667807735888",   # 40 - Roblox Scripts!
}
BANCHECK_TOKEN_ENV = "MOD_DISCORD_USER_TOKEN_BANCHECK"

# Backwards-compat: if someone still has the old single-token env var
# set, fall back to it for bancheck. New deployments should use the
# suffixed name above.
_BANCHECK_FALLBACK_ENV = "MOD_DISCORD_USER_TOKEN"


# ── Per-token identity ────────────────────────────────────────────
# Each token gets a distinct OS/browser/locale combo so the 5
# accounts don't all look identical to Discord's fingerprinter.
# Stable for the life of the process — never randomized per-request.

def _super_properties(props: dict) -> str:
    return base64.b64encode(json.dumps(props, separators=(",", ":")).encode()).decode()


def _identity(
    *,
    os_name: str,
    os_version: str,
    sec_ch_ua_platform: str,
    ua: str,
    chrome_major: str,
    chrome_full: str,
    locale: str,
    timezone: str,
    build_number: int,
    sec_ch_ua: str,
) -> dict:
    return {
        "user_agent": ua,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_platform": sec_ch_ua_platform,
        "locale": locale,
        "timezone": timezone,
        "super_properties": _super_properties({
            "os": os_name,
            "browser": "Chrome",
            "device": "",
            "system_locale": locale,
            "browser_user_agent": ua,
            "browser_version": chrome_full,
            "os_version": os_version,
            "referrer": "",
            "referring_domain": "",
            "referrer_current": "",
            "referring_domain_current": "",
            "release_channel": "stable",
            "client_build_number": build_number,
            "client_event_source": None,
        }),
    }


# Five distinct identities. Build numbers and Chrome versions roughly
# track late-2025 / early-2026 stable. Bump as needed (see top doc).
_IDENTITIES: dict[str, dict] = {
    "MOD_DISCORD_USER_TOKEN_1": _identity(
        os_name="Windows", os_version="10", sec_ch_ua_platform='"Windows"',
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        chrome_major="131", chrome_full="131.0.0.0",
        locale="en-US", timezone="America/New_York",
        build_number=354000,
        sec_ch_ua='"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    ),
    "MOD_DISCORD_USER_TOKEN_2": _identity(
        os_name="Mac OS X", os_version="10.15.7", sec_ch_ua_platform='"macOS"',
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        chrome_major="130", chrome_full="130.0.0.0",
        locale="en-GB", timezone="Europe/London",
        build_number=353000,
        sec_ch_ua='"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    ),
    "MOD_DISCORD_USER_TOKEN_3": _identity(
        os_name="Windows", os_version="10", sec_ch_ua_platform='"Windows"',
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        chrome_major="132", chrome_full="132.0.0.0",
        locale="de-DE", timezone="Europe/Berlin",
        build_number=355000,
        sec_ch_ua='"Not A(Brand";v="99", "Google Chrome";v="132", "Chromium";v="132"',
    ),
    "MOD_DISCORD_USER_TOKEN_4": _identity(
        os_name="Linux", os_version="", sec_ch_ua_platform='"Linux"',
        ua="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        chrome_major="131", chrome_full="131.0.0.0",
        locale="en-US", timezone="America/Los_Angeles",
        build_number=354100,
        sec_ch_ua='"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    ),
    "MOD_DISCORD_USER_TOKEN_5": _identity(
        os_name="Mac OS X", os_version="14.4.1", sec_ch_ua_platform='"macOS"',
        ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        chrome_major="132", chrome_full="132.0.0.0",
        locale="en-US", timezone="America/Chicago",
        build_number=355200,
        sec_ch_ua='"Not A(Brand";v="99", "Google Chrome";v="132", "Chromium";v="132"',
    ),
    "MOD_DISCORD_USER_TOKEN_6": _identity(
        os_name="Windows", os_version="10", sec_ch_ua_platform='"Windows"',
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        chrome_major="133", chrome_full="133.0.0.0",
        locale="fr-FR", timezone="Europe/Paris",
        build_number=355500,
        sec_ch_ua='"Not/A)Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
    ),
    BANCHECK_TOKEN_ENV: _identity(
        os_name="Windows", os_version="10", sec_ch_ua_platform='"Windows"',
        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        chrome_major="130", chrome_full="130.0.0.0",
        locale="en-US", timezone="America/New_York",
        build_number=353900,
        sec_ch_ua='"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    ),
}


# ── Mutable per-token state ───────────────────────────────────────

class _TokenState:
    __slots__ = ("env_var", "dead", "dead_reason", "ratelimit_until", "lock")

    def __init__(self, env_var: str):
        self.env_var = env_var
        self.dead: bool = False
        self.dead_reason: Optional[str] = None
        self.ratelimit_until: float = 0.0
        # Serializes requests on this token so two concurrent commands
        # can't double-spend it.
        self.lock = threading.Lock()


_STATE: dict[str, _TokenState] = {env: _TokenState(env) for env in _IDENTITIES}
_alerted_dead: set[str] = set()


def _alert_dead(env_var: str, reason: str) -> None:
    if env_var in _alerted_dead:
        return
    _alerted_dead.add(env_var)
    try:
        log_admin_alert(
            f"🚨 Discord token failed: {env_var}",
            (
                f"`{env_var}` was rejected by Discord and dropped from rotation.\n\n"
                f"**Reason:** {reason}\n\n"
                f"Replace the token in Railway and redeploy to bring it back online."
            ),
        )
    except Exception:
        pass


# ── Token resolution ──────────────────────────────────────────────

def _resolve_token_value(env_var: str) -> str:
    """
    Read the token value for env_var. For BANCHECK, fall back to the
    legacy single-token env var so existing deployments keep working
    during the rollout.
    """
    val = os.environ.get(env_var, "").strip()
    if val:
        return val
    if env_var == BANCHECK_TOKEN_ENV:
        return os.environ.get(_BANCHECK_FALLBACK_ENV, "").strip()
    return ""


# ── Header builder ────────────────────────────────────────────────

API = "https://discord.com/api/v10"


def _build_headers(env_var: str) -> dict:
    ident = _IDENTITIES[env_var]
    token = _resolve_token_value(env_var)
    return {
        "Authorization": token,
        "User-Agent": ident["user_agent"],
        "Accept": "*/*",
        "Accept-Language": f"{ident['locale']},en;q=0.9",
        "X-Discord-Locale": ident["locale"],
        "X-Discord-Timezone": ident["timezone"],
        "X-Super-Properties": ident["super_properties"],
        "X-Debug-Options": "bugReporterEnabled",
        "Origin": "https://discord.com",
        "Referer": "https://discord.com/channels/@me",
        "sec-ch-ua": ident["sec_ch_ua"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": ident["sec_ch_ua_platform"],
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


# ── Public request API ────────────────────────────────────────────

class TokenDead(Exception):
    """Raised when the requested token has been marked dead."""


def request(
    env_var: str,
    method: str,
    path: str,
    *,
    timeout: float = 10.0,
    mark_forbidden_dead: bool = True,
) -> requests.Response:
    """
    Authenticated Discord REST call routed through the identity bound
    to env_var. Serializes per-token. Marks the token dead on 401, and
    optionally on 403, pauses on 429. Caller handles other status codes.
    """
    if env_var not in _STATE:
        raise KeyError(f"Unknown discord token env var: {env_var}")

    state = _STATE[env_var]
    if state.dead:
        raise TokenDead(f"{env_var}: {state.dead_reason}")
    if not _resolve_token_value(env_var):
        state.dead = True
        state.dead_reason = "env var not set"
        _alert_dead(env_var, "env var not set")
        raise TokenDead(f"{env_var}: env var not set")

    # Wait out any per-token rate-limit window before grabbing the lock
    # — sleeping with the lock held would needlessly block other paths.
    delay = state.ratelimit_until - time.time()
    if delay > 0:
        time.sleep(min(delay, 30.0))

    with state.lock:
        url = path if path.startswith("http") else f"{API}{path}"
        r = requests.request(
            method, url, headers=_build_headers(env_var), timeout=timeout,
        )
        if r.status_code == 401 or (r.status_code == 403 and mark_forbidden_dead):
            state.dead = True
            state.dead_reason = f"HTTP {r.status_code}"
            _alert_dead(env_var, f"HTTP {r.status_code} on {method} {path}")
        elif r.status_code == 429:
            try:
                retry = float(r.headers.get("Retry-After", "1"))
            except Exception:
                retry = 1.0
            state.ratelimit_until = time.time() + min(max(retry, 0.5), 30.0)
        return r


def _retry_after_seconds(response: requests.Response, *, cap: float = 10.0) -> float:
    try:
        retry = float(response.headers.get("Retry-After", "1"))
    except Exception:
        retry = 1.0
    return min(max(retry, 0.75), cap)


# ── Membership probe (hot path for /in-cheating-servers) ──────────

def check_membership(env_var: str, guild_id: str, user_id: str) -> dict:
    """
    GET /guilds/{guild_id}/members/{user_id} via the given token.

    Returns:
        {"in": True, "joined_at": str|None}                      on 200
        {"in": False}                                             on 404
        {"in": None, "error": str, "dead": bool}                  otherwise
    """
    try:
        r = request(
            env_var,
            "GET",
            f"/guilds/{guild_id}/members/{user_id}",
            mark_forbidden_dead=False,
        )
    except TokenDead as e:
        return {"in": None, "error": str(e), "dead": True}
    except Exception as e:
        return {"in": None, "error": f"network: {e}", "dead": False}

    if r.status_code == 200:
        try:
            return {"in": True, "joined_at": r.json().get("joined_at")}
        except Exception:
            return {"in": True, "joined_at": None}
    if r.status_code == 404:
        return {"in": False}
    if r.status_code == 401:
        return {"in": None, "error": "HTTP 401", "dead": True}
    if r.status_code == 403:
        return {"in": None, "error": "HTTP 403 / missing access", "dead": False}
    if r.status_code == 429:
        return {"in": None, "error": "rate-limited", "dead": False}
    return {"in": None, "error": f"HTTP {r.status_code}", "dead": False}


# ── Server-grouping helpers ───────────────────────────────────────

def group_servers_by_token(
    cheating_servers: list[tuple[str, str]],
) -> dict[str, list[tuple[str, str]]]:
    """
    Split cheating_servers into a dict keyed by responsible token env
    var, using TOKEN_SERVER_RANGES (1-indexed positions).
    """
    groups: dict[str, list[tuple[str, str]]] = {env: [] for env in TOKEN_SERVER_RANGES}
    for env_var, indices in TOKEN_SERVER_RANGES.items():
        for idx in indices:
            if 1 <= idx <= len(cheating_servers):
                server = cheating_servers[idx - 1]
                if server[0] not in TEMP_UNCOVERED_GUILD_IDS:
                    groups[env_var].append(server)
    return groups


def temporarily_uncovered_servers(
    cheating_servers: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Return watched guilds that are intentionally paused from scanning."""
    return [
        server
        for server in cheating_servers
        if server[0] in TEMP_UNCOVERED_GUILD_IDS
    ]


def scan_user_serial_for_token(
    env_var: str,
    servers: list[tuple[str, str]],
    user_id: str,
) -> dict:
    """
    Scan ONE token's subset of servers serially with jittered sleeps
    and shuffled order. If the token dies mid-scan, the remaining
    servers in this group are reported as errored.
    """
    in_servers: list[dict] = []
    not_in: list[str] = []
    checked_servers: list[dict] = []
    errors: list[str] = []
    token_dead = False

    shuffled = list(servers)
    random.shuffle(shuffled)

    for i, (gid, gname) in enumerate(shuffled):
        result = check_membership(env_var, gid, user_id)

        if result.get("dead"):
            token_dead = True
            errors.append(f"{gname}: {env_var} dead")
            for sub_gid, sub_gname in shuffled[i + 1:]:
                errors.append(f"{sub_gname}: {env_var} dead (skipped)")
            break

        if result["in"] is True:
            in_servers.append({
                "guild_id": gid,
                "guild_name": gname,
                "joined_at": result.get("joined_at"),
            })
            checked_servers.append({
                "guild_id": gid,
                "guild_name": gname,
                "in": True,
                "joined_at": result.get("joined_at"),
                "env_var": env_var,
            })
        elif result["in"] is False:
            not_in.append(gname)
            checked_servers.append({
                "guild_id": gid,
                "guild_name": gname,
                "in": False,
                "env_var": env_var,
            })
        else:
            checked_servers.append({
                "guild_id": gid,
                "guild_name": gname,
                "in": None,
                "error": result.get("error"),
                "env_var": env_var,
            })
            errors.append(f"{gname}: {result.get('error', '?')}")

        # Jittered pacing — real users don't tick at fixed 0.25s.
        time.sleep(random.uniform(0.6, 1.4))

    return {
        "env_var": env_var,
        "token_dead": token_dead,
        "in_servers": in_servers,
        "not_in": not_in,
        "checked_servers": checked_servers,
        "errors": errors,
    }


def _message_from_hit_group(hit_group) -> Optional[dict]:
    if not hit_group:
        return None
    msg = hit_group[0] if isinstance(hit_group, list) else hit_group
    return msg if isinstance(msg, dict) else None


def _compact_message(msg: dict) -> dict:
    attachments = msg.get("attachments") or []
    embeds = msg.get("embeds") or []
    stickers = msg.get("sticker_items") or msg.get("stickers") or []
    return {
        "id": str(msg.get("id") or ""),
        "channel_id": str(msg.get("channel_id") or ""),
        "timestamp": msg.get("timestamp"),
        "content": (msg.get("content") or "")[:1000],
        "type": msg.get("type"),
        "attachments": [
            {
                "filename": att.get("filename") or "attachment",
                "content_type": att.get("content_type"),
            }
            for att in attachments[:5]
        ],
        "embeds": [
            {
                "type": embed.get("type"),
                "title": embed.get("title"),
                "description": (embed.get("description") or "")[:250],
            }
            for embed in embeds[:3]
        ],
        "stickers": [
            sticker.get("name") or "sticker"
            for sticker in stickers[:5]
        ],
    }


def search_user_messages_in_guild(
    env_var: str,
    guild_id: str,
    user_id: str,
    *,
    offset: int = 0,
    channel_id: Optional[str] = None,
    max_429_retries: int = 4,
    retry_after_cap: float = 10.0,
) -> dict:
    """
    Search a guild for messages authored by user_id.

    This is historical evidence only: it proves the user has message history in
    the guild, not that they are currently a member.
    """
    params = {"author_id": str(user_id), "offset": int(offset)}
    if channel_id:
        params["channel_id"] = str(channel_id)
    query = urlencode(params)
    r = None
    for attempt in range(max_429_retries):
        try:
            r = request(
                env_var,
                "GET",
                f"/guilds/{guild_id}/messages/search?{query}",
                timeout=12,
                mark_forbidden_dead=False,
            )
        except TokenDead as e:
            return {"found": None, "error": str(e), "dead": True}
        except Exception as e:
            return {"found": None, "error": f"network: {e}", "dead": False}

        if r.status_code != 429:
            break
        time.sleep(_retry_after_seconds(r, cap=retry_after_cap) + random.uniform(0.25, 0.75))

    if r is None:
        return {"found": None, "error": "request failed", "dead": False}

    if r.status_code == 200:
        try:
            data = r.json()
        except Exception:
            return {"found": None, "error": "json parse failed", "dead": False}

        total = int(data.get("total_results") or 0)
        latest_ts = None
        page_messages: list[dict] = []
        messages = data.get("messages") or []
        for hit_group in messages:
            msg = _message_from_hit_group(hit_group)
            if not msg:
                continue
            ts = msg.get("timestamp")
            if latest_ts is None:
                latest_ts = ts
            page_messages.append(_compact_message(msg))
        return {
            "found": total > 0,
            "total_results": total,
            "last_message_at": latest_ts,
            "messages": page_messages,
            "dead": False,
        }
    if r.status_code == 429:
        return {"found": None, "error": "rate-limited", "dead": False}
    if r.status_code == 401:
        return {"found": None, "error": "HTTP 401", "dead": True}
    if r.status_code in (403, 404):
        return {"found": False, "total_results": 0, "dead": False}
    return {"found": None, "error": f"HTTP {r.status_code}", "dead": False}


def fetch_guild_channel_names(env_var: str, guild_id: str) -> dict:
    """Return a channel_id -> channel_name map for one guild."""
    cache_key = (env_var, str(guild_id))
    cached = _CHANNEL_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < CHANNEL_CACHE_SECONDS:
        return cached[1]

    r = None
    for attempt in range(4):
        try:
            r = request(
                env_var,
                "GET",
                f"/guilds/{guild_id}/channels",
                timeout=15,
                mark_forbidden_dead=False,
            )
        except TokenDead as e:
            return {"channels": {}, "error": str(e), "dead": True}
        except Exception as e:
            return {"channels": {}, "error": f"network: {e}", "dead": False}

        if r.status_code != 429:
            break
        time.sleep(_retry_after_seconds(r) + random.uniform(0.25, 0.75))

    if r is None:
        return {"channels": {}, "error": "request failed", "dead": False}

    if r.status_code == 200:
        try:
            channels = r.json()
        except Exception:
            return {"channels": {}, "error": "json parse failed", "dead": False}
        result = {
            "channels": {
                str(channel.get("id") or ""): channel.get("name") or "unknown-channel"
                for channel in channels
                if channel.get("id")
            },
            "searchable_channel_ids": [
                str(channel.get("id") or "")
                for channel in channels
                if channel.get("id") and int(channel.get("type", -1)) in (0, 5, 10, 11, 12, 15)
            ],
            "dead": False,
        }
        _CHANNEL_CACHE[cache_key] = (time.time(), result)
        return result
    if r.status_code == 401:
        return {"channels": {}, "error": "HTTP 401", "dead": True}
    return {"channels": {}, "error": f"HTTP {r.status_code}", "dead": False}


def _add_channel_names(messages: list[dict], channel_names: dict[str, str]) -> list[dict]:
    for msg in messages:
        channel_id = str(msg.get("channel_id") or "")
        msg["channel_name"] = channel_names.get(channel_id, "unknown-channel")
    return messages


def search_user_messages_in_guild_deep(env_var: str, guild_id: str, user_id: str) -> dict:
    """
    Search guild-wide first, then fallback to per-channel search if guild-wide
    misses messages that Discord only exposes through channel-scoped search.
    """
    channel_result = fetch_guild_channel_names(env_var, guild_id)
    channel_names = channel_result.get("channels") or {}
    searchable_channel_ids = channel_result.get("searchable_channel_ids") or []

    guild_result = search_user_messages_in_guild(env_var, guild_id, user_id)
    if guild_result.get("dead") or guild_result.get("found") is None:
        return guild_result
    if guild_result.get("found") is True:
        guild_result["messages"] = _add_channel_names(guild_result.get("messages") or [], channel_names)
        guild_result["search_scope"] = "guild"
        return guild_result

    combined_messages: list[dict] = []
    total_results = 0
    latest_ts = None
    errors: list[str] = []

    for channel_id in searchable_channel_ids:
        result = search_user_messages_in_guild(
            env_var,
            guild_id,
            user_id,
            channel_id=channel_id,
        )
        if result.get("dead"):
            return result
        if result.get("found") is None:
            errors.append(f"{channel_names.get(channel_id, channel_id)}: {result.get('error', '?')}")
            continue
        if result.get("found") is not True:
            continue

        total_results += int(result.get("total_results") or 0)
        for msg in result.get("messages") or []:
            msg["channel_name"] = channel_names.get(str(msg.get("channel_id") or ""), "unknown-channel")
            combined_messages.append(msg)
        if latest_ts is None:
            latest_ts = result.get("last_message_at")
        time.sleep(random.uniform(0.2, 0.5))

    return {
        "found": bool(total_results),
        "total_results": total_results,
        "last_message_at": latest_ts,
        "messages": combined_messages[:25],
        "search_scope": "channel_fallback",
        "channel_lookup_error": channel_result.get("error"),
        "channel_errors": errors[:10],
        "dead": False,
    }


def fetch_user_message_history(env_var: str, guild_id: str, user_id: str) -> dict:
    """Fetch paginated author message-search results for button detail views."""
    channel_result = fetch_guild_channel_names(env_var, guild_id)
    channel_names = channel_result.get("channels") or {}

    all_messages: list[dict] = []
    total_results = 0
    offset = 0

    while True:
        result = search_user_messages_in_guild(
            env_var,
            guild_id,
            user_id,
            offset=offset,
            max_429_retries=7,
            retry_after_cap=30.0,
        )
        if result.get("dead"):
            return {
                "messages": all_messages,
                "total_results": total_results,
                "error": result.get("error"),
                "dead": True,
            }
        if result.get("found") is None:
            return {
                "messages": all_messages,
                "total_results": total_results,
                "error": result.get("error"),
                "dead": False,
            }

        total_results = int(result.get("total_results") or 0)
        page_messages = result.get("messages") or []
        for msg in page_messages:
            channel_id = str(msg.get("channel_id") or "")
            msg["channel_name"] = channel_names.get(channel_id, "unknown-channel")
            all_messages.append(msg)

        if not page_messages:
            break
        offset += 25
        if offset >= total_results:
            break
        if len(all_messages) >= MAX_HISTORY_MESSAGE_RESULTS:
            break
        time.sleep(random.uniform(0.4, 0.8))

    return {
        "messages": all_messages[:MAX_HISTORY_MESSAGE_RESULTS],
        "total_results": total_results,
        "limited": total_results > len(all_messages),
        "channel_lookup_error": channel_result.get("error"),
        "dead": False,
    }


def fetch_user_message_history_page(
    env_var: str,
    guild_id: str,
    user_id: str,
    *,
    offset: int = 0,
) -> dict:
    """Fetch one Discord search page for interactive lazy pagination."""
    channel_result = fetch_guild_channel_names(env_var, guild_id)
    channel_names = channel_result.get("channels") or {}
    result = search_user_messages_in_guild(
        env_var,
        guild_id,
        user_id,
        offset=offset,
        max_429_retries=7,
        retry_after_cap=30.0,
    )
    if result.get("dead") or result.get("found") is None:
        return {
            "messages": [],
            "total_results": 0,
            "error": result.get("error"),
            "dead": bool(result.get("dead")),
        }

    messages = _add_channel_names(result.get("messages") or [], channel_names)
    total = int(result.get("total_results") or 0)
    next_offset = offset + 25 if offset + 25 < total else None
    return {
        "messages": messages,
        "total_results": total,
        "next_offset": next_offset,
        "offset": offset,
        "channel_lookup_error": channel_result.get("error"),
        "dead": False,
    }


def fetch_user_message_history_deep(env_var: str, guild_id: str, user_id: str) -> dict:
    """
    Fetch message history for the button detail view. Uses guild-wide search
    first; if that is empty, falls back to per-channel searches.
    """
    result = fetch_user_message_history(env_var, guild_id, user_id)
    if result.get("messages") or result.get("error"):
        result["search_scope"] = "guild"
        return result

    channel_result = fetch_guild_channel_names(env_var, guild_id)
    channel_names = channel_result.get("channels") or {}
    all_messages: list[dict] = []
    total_results = 0
    errors: list[str] = []

    for channel_id in channel_result.get("searchable_channel_ids") or []:
        offset = 0
        while True:
            page = search_user_messages_in_guild(
                env_var,
                guild_id,
                user_id,
                offset=offset,
                channel_id=channel_id,
            )
            if page.get("dead"):
                return {
                    "messages": all_messages,
                    "total_results": total_results,
                    "error": page.get("error"),
                    "dead": True,
                }
            if page.get("found") is None:
                errors.append(f"{channel_names.get(channel_id, channel_id)}: {page.get('error', '?')}")
                break

            page_messages = page.get("messages") or []
            total_results += int(page.get("total_results") or 0) if offset == 0 else 0
            for msg in page_messages:
                msg["channel_name"] = channel_names.get(str(msg.get("channel_id") or ""), "unknown-channel")
                all_messages.append(msg)

            if not page_messages:
                break
            offset += 25
            if offset >= int(page.get("total_results") or 0):
                break
            if len(all_messages) >= MAX_HISTORY_MESSAGE_RESULTS:
                break
            time.sleep(random.uniform(0.2, 0.5))

        if len(all_messages) >= MAX_HISTORY_MESSAGE_RESULTS:
            break

    return {
        "messages": all_messages[:MAX_HISTORY_MESSAGE_RESULTS],
        "total_results": total_results,
        "limited": total_results > len(all_messages),
        "channel_lookup_error": channel_result.get("error"),
        "channel_errors": errors[:10],
        "search_scope": "channel_fallback",
        "dead": False,
    }


def search_history_serial_for_token(
    env_var: str,
    servers: list[tuple[str, str]],
    user_id: str,
) -> dict:
    """Search one token's not-currently-in servers for author history."""
    historical_hits: list[dict] = []
    errors: list[str] = []
    token_dead = False

    for i, (gid, gname) in enumerate(servers):
        result = search_user_messages_in_guild(env_var, gid, user_id)

        if result.get("dead"):
            token_dead = True
            errors.append(f"{gname}: {env_var} dead during history search")
            for _sub_gid, sub_gname in servers[i + 1:]:
                errors.append(f"{sub_gname}: {env_var} dead (history skipped)")
            break

        if result.get("found") is True:
            channel_result = fetch_guild_channel_names(env_var, gid)
            channel_names = channel_result.get("channels") or {}
            historical_hits.append({
                "guild_id": gid,
                "guild_name": gname,
                "env_var": env_var,
                "source": "message_history",
                "total_messages": result.get("total_results"),
                "last_message_at": result.get("last_message_at"),
                "messages": _add_channel_names(result.get("messages") or [], channel_names),
            })
        elif result.get("found") is None:
            errors.append(f"{gname}: history search {result.get('error', '?')}")

        time.sleep(random.uniform(0.6, 1.2))

    return {
        "env_var": env_var,
        "token_dead": token_dead,
        "historical_hits": historical_hits,
        "errors": errors,
    }


# ── Diagnostic helpers ────────────────────────────────────────────

def all_scanner_tokens_dead() -> bool:
    return all(_STATE[env].dead for env in TOKEN_SERVER_RANGES)


def scanner_token_status() -> dict[str, str]:
    return {
        env: ("dead: " + (_STATE[env].dead_reason or "unknown"))
        if _STATE[env].dead else "ok"
        for env in TOKEN_SERVER_RANGES
    }


def is_token_dead(env_var: str) -> bool:
    state = _STATE.get(env_var)
    return bool(state and state.dead)


def safe_check_token(env_var: str) -> dict:
    """
    Probe a single token with GET /users/@me — the exact same request
    a real Discord client makes on every page load.  Returns:

        {"alive": True,  "username": "...", "id": "..."}
        {"alive": False, "reason": "..."}
        {"alive": None,  "reason": "..."}   # couldn't determine (timeout etc.)

    Does NOT mark the token dead in _STATE — that's left to the caller.
    """
    val = _resolve_token_value(env_var)
    if not val:
        return {"alive": False, "reason": "env var not set"}

    # If already flagged dead from a previous real call, report that
    # without hitting the network again.
    state = _STATE.get(env_var)
    if state and state.dead:
        return {"alive": False, "reason": f"cached dead: {state.dead_reason}"}

    try:
        ident = _IDENTITIES.get(env_var)
        if ident is None:
            # Token not in the identity map (e.g. monitor token) — use
            # a minimal header set.
            headers = {
                "Authorization": val,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            }
        else:
            headers = _build_headers(env_var)

        r = requests.get(f"{API}/users/@me", headers=headers, timeout=10)

        if r.status_code == 200:
            data = r.json()
            return {
                "alive": True,
                "username": data.get("username", "?"),
                "id": data.get("id", "?"),
            }
        if r.status_code in (401, 403):
            return {"alive": False, "reason": f"HTTP {r.status_code}"}
        if r.status_code == 429:
            return {"alive": None, "reason": "rate-limited (try again later)"}
        return {"alive": None, "reason": f"HTTP {r.status_code}"}
    except requests.Timeout:
        return {"alive": None, "reason": "timeout"}
    except Exception as e:
        return {"alive": None, "reason": f"error: {e}"}


# All token env vars that the status command should report on.
ALL_TOKEN_ENV_VARS: list[str] = (
    list(TOKEN_SERVER_RANGES.keys())
    + [BANCHECK_TOKEN_ENV]
)
