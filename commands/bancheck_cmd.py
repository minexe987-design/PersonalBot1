# ──────────────────────────────────────────────────────────────────
# /bancheckv2  —  search a moderation server's channel names for a
# Roblox username.
#
# Use case: a Roblox in-game moderation server where staff rename
# ban-report tickets to include the offender's (and their teamers')
# Roblox usernames. We can't add the bot to that server, so we point
# this command at a regular Discord user account that's already a
# member there. The bot calls Discord's REST API with that user's
# token to list the server's channels (visible to that user) and
# substring-matches their names against the username we're checking.
#
# Risk note: using a user-account token for automated requests
# violates Discord's TOS. Use only on a throwaway account you accept
# losing.
#
# Required env vars (otherwise the command returns a config error):
#   MOD_DISCORD_USER_TOKEN_BANCHECK  —  Discord user account token
#       dedicated to bancheck/reportercheck. NO "Bot " prefix.
#       Identity + rate limiting handled by core/discord_pool.py.
#       (Falls back to legacy MOD_DISCORD_USER_TOKEN if the suffixed
#       var isn't set, for migration purposes only.)
#   MOD_SERVER_GUILD_IDS             —  Comma-separated Discord guild IDs
#       for moderation servers. Legacy MOD_SERVER_GUILD_ID still works.
#
# Access: public — no runner whitelist (anyone can use /bancheckv2
# and /reportercheck).
# ──────────────────────────────────────────────────────────────────

import asyncio
import os
import time
from typing import Optional

import discord
import discord
from discord.ext import commands
from discord.ext import commands

from core import discord_pool
from core.tracking import track_command, track_discord_user

# Bancheck/reportercheck use a dedicated user-account token (in the
# Pelican mod server only). Routing through core.discord_pool gives it
# the same Chrome-shaped identity treatment as the scanner tokens, and
# the pool handles dead-token detection + admin alerts on 401/403.
_BC = discord_pool.BANCHECK_TOKEN_ENV

# ── Channel cache ─────────────────────────────────────────────────
# Discord rate-limits user-token guild/channels endpoints. We cache the
# response in-process for a few minutes so that running /bancheckv2
# multiple times in a row doesn't slam the API.
_CACHE_TTL_SECONDS = 300  # 5 minutes
_channel_cache: dict[str, dict[str, object]] = {}
_role_cache: dict[str, dict[str, object]] = {}
_member_cache: dict[tuple[str, str], Optional[dict]] = {}
_VIEW_CHANNEL = 1 << 10
_TICKET_TOOL_USER_ID = "557628352828014614"
_NWR_GUILD_ID = "1153950961195290645"
_NWR_REPORT_CATEGORY_ID = "1153956175033946142"
_EDENSTARS_REPORT_CATEGORY_ID = "1501628320440389632"
_KNOWN_REPORT_CATEGORY_IDS = {
    _EDENSTARS_REPORT_CATEGORY_ID,
    _NWR_REPORT_CATEGORY_ID,
}
_REPORT_CATEGORY_IDS_BY_GUILD: dict[str, set[str]] = {
    _NWR_GUILD_ID: {_NWR_REPORT_CATEGORY_ID},
}


def _configured_guild_ids() -> list[str]:
    raw = (
        os.environ.get("MOD_SERVER_GUILD_IDS", "").strip()
        or os.environ.get("MOD_SERVER_GUILD_ID", "").strip()
    )
    guild_ids: list[str] = []
    seen: set[str] = set()
    for part in raw.replace("\n", ",").split(","):
        guild_id = part.strip()
        if guild_id and guild_id not in seen:
            guild_ids.append(guild_id)
            seen.add(guild_id)
    return guild_ids


def _format_guild_list(names: list[str]) -> str:
    if not names:
        return "the configured moderation server(s)"
    if len(names) == 1:
        return names[0]
    return f"{len(names)} moderation servers"


def _emojis() -> dict[str, str]:
    return {
        "primary": "<a:anipinkarrow:1497344028004581386>",
        "secondary": "<:greencheck:1497344048267137144>",
        "success": "<:greencheck:1497344048267137144>",
        "clipboard": "<:clipboard:1497344037294702762>",
        "check": "<:check:1497344035696672959>",
        "mag": "<a:mag:1497344052709036125>",
        "lock": "<:lock:1497344050078941344>",
        "exploiters": "<a:exploiters:1498648559623344158>",
    }

# ── Staff role hierarchy (Pelican Reports server) ─────────────────
# Highest tier first — used by /reportercheck to label the looked-up
# user with a single concrete role (e.g. "Senior Report Checker")
# instead of the old "Staff / role-holder" boolean. If a user holds
# multiple staff roles, the one nearest the top of this list wins.
#
# To add a new staff role: insert a (role_id, "Display Name") tuple
# at the right priority position. Role IDs were discovered via
# scripts/inspect_staff_hierarchy.py — re-run it if the role list
# in the mod server changes significantly.
STAFF_ROLE_HIERARCHY: list[tuple[str, str]] = [
    ("1260593580322455615", "Owner"),                  # pos 113
    ("1341627929511198852", "Co-Owner"),               # pos 110
    ("1267035582660612207", "Lead AC Mod"),            # pos 109
    ("1260663657067708486", "AC Mod"),                 # pos 106
    ("1261418666822471801", "Community Manager"),      # pos 104
    ("1261679125362376754", "On-Call"),                # pos 103
    ("1260593671963676693", "Server Mod"),             # pos 100
    ("1320517153182449664", "Lead Report Checker"),    # pos 93
    ("1423362749907341382", "Senior Report Checker"),  # pos 90
    ("1265033449052438680", "Report Checker"),         # pos 58
    ("1314413553175760946", "Trial Report Checker"),   # pos 52
    ("1262118090435461150", "Report Access"),          # pos 34
    ("1287524241255698563", "Appeals"),                # pos 23
]


# ── Channel-type labels (Discord enum) ────────────────────────────
_CHANNEL_TYPES = {
    0: "text",
    2: "voice",
    4: "category",
    5: "announcement",
    10: "announcement-thread",
    11: "public-thread",
    12: "private-thread",
    13: "stage",
    15: "forum",
    16: "media",
}


def _channel_type_label(t: object) -> str:
    try:
        return _CHANNEL_TYPES.get(int(t), f"type-{t}")
    except Exception:
        return f"type-{t}"


def _fetch_guild_meta(guild_id: str) -> Optional[str]:
    """GET /guilds/{guild_id} via the bancheck pool token."""
    try:
        r = discord_pool.request(_BC, "GET", f"/guilds/{guild_id}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                name = data.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    except Exception:
        pass
    return None


def _fetch_guild_channels(guild_id: str) -> tuple[Optional[list[dict]], Optional[str]]:
    """
    GET /guilds/{guild_id}/channels via the bancheck pool token.

    The pool handles token-dead detection / admin alerts on 401/403,
    so we just translate the response into (data, error_string) here.
    """
    try:
        r = discord_pool.request(_BC, "GET", f"/guilds/{guild_id}/channels", timeout=15)
    except discord_pool.TokenDead as e:
        return None, f"Bancheck token unavailable: {e}"
    except Exception as e:
        return None, f"Network error: {e}"

    if r.status_code == 401:
        return None, "401 Unauthorized — the bancheck token is invalid or expired."
    if r.status_code == 403:
        return None, "403 Forbidden — the bancheck account is not in the configured guild, or lacks read access."
    if r.status_code == 404:
        return None, "404 Not Found — the guild ID in MOD_SERVER_GUILD_ID doesn't exist or the user isn't in it."
    if r.status_code == 429:
        return None, "429 Rate-limited — try again in a minute."
    if r.status_code != 200:
        return None, f"Discord API HTTP {r.status_code}: {(r.text or '')[:200]}"
    try:
        data = r.json()
        if isinstance(data, list):
            return data, None
        return None, "Unexpected response shape from Discord."
    except Exception as e:
        return None, f"Couldn't parse Discord response: {e}"


def _get_channels_cached(guild_id: str) -> tuple[Optional[list[dict]], Optional[str]]:
    now = time.time()
    cache = _channel_cache.get(guild_id) or {}
    cached = cache.get("channels")
    fetched_at = cache.get("fetched_at") or 0.0
    if (
        cached is not None
        and (now - float(fetched_at)) < _CACHE_TTL_SECONDS
    ):
        return cached, None  # type: ignore[return-value]

    fresh, err = _fetch_guild_channels(guild_id)
    if err:
        return None, err
    _channel_cache[guild_id] = {
        "channels": fresh,
        "fetched_at": now,
        "guild_name": _fetch_guild_meta(guild_id),
    }
    return fresh, None


def _cached_guild_name(guild_id: str) -> Optional[str]:
    cache = _channel_cache.get(guild_id) or {}
    name = cache.get("guild_name")
    return name if isinstance(name, str) and name else None


def _fetch_guild_roles_cached(guild_id: str) -> dict[str, str]:
    now = time.time()
    cache = _role_cache.get(guild_id) or {}
    cached = cache.get("roles")
    fetched_at = cache.get("fetched_at") or 0.0
    if (
        isinstance(cached, dict)
        and (now - float(fetched_at)) < _CACHE_TTL_SECONDS
    ):
        return cached  # type: ignore[return-value]

    roles: dict[str, str] = {}
    try:
        r = discord_pool.request(_BC, "GET", f"/guilds/{guild_id}/roles", timeout=3)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                for role in data:
                    if isinstance(role, dict) and role.get("id"):
                        roles[str(role["id"])] = str(role.get("name") or role["id"])
    except Exception:
        roles = {}

    _role_cache[guild_id] = {
        "roles": roles,
        "fetched_at": now,
    }
    return roles


def _fetch_guild_member_cached(guild_id: str, user_id: str) -> Optional[dict]:
    key = (guild_id, user_id)
    if key in _member_cache:
        return _member_cache[key]

    member: Optional[dict] = None
    try:
        r = discord_pool.request(_BC, "GET", f"/guilds/{guild_id}/members/{user_id}", timeout=3)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                member = data
    except Exception:
        member = None

    _member_cache[key] = member
    return member


def _member_display_name(member: Optional[dict], fallback_id: str) -> str:
    if not isinstance(member, dict):
        return fallback_id
    user = member.get("user") or {}
    return (
        member.get("nick")
        or user.get("global_name")
        or user.get("username")
        or fallback_id
    )


def _member_role_names(member: Optional[dict], role_names: dict[str, str]) -> list[str]:
    if not isinstance(member, dict):
        return []
    return [
        role_names.get(str(role_id), str(role_id))
        for role_id in member.get("roles", [])
    ]


def _is_ticket_tool(member: Optional[dict], display_name: str, role_names: dict[str, str]) -> bool:
    if display_name.lower() == "ticket tool":
        return True
    roles = _member_role_names(member, role_names)
    return any(role.lower() == "ticket tool" for role in roles)


def _format_name_list(names: list[str], *, limit: int = 8) -> str:
    if not names:
        return "Unknown"
    shown = names[:limit]
    text = ", ".join(discord.utils.escape_markdown(name) for name in shown)
    if len(names) > limit:
        text += f", +{len(names) - limit} more"
    return text


def _format_role_list(names: list[str], *, limit: int = 8) -> str:
    if not names:
        return "Unknown"
    shown = names[:limit]
    text = ", ".join(
        f"@{discord.utils.escape_markdown(name.lstrip('@'))}"
        for name in shown
    )
    if len(names) > limit:
        text += f", +{len(names) - limit} more"
    return text


def _is_staff_member(member: Optional[dict], role_names: dict[str, str]) -> bool:
    staff_markers = (
        "ac mod",
        "lead ac mod",
        "report checker",
        "trial report checker",
        "lead report checker",
        "senior report checker",
        "server mod",
        "community manager",
        "admin",
        "moderator",
    )
    roles = [role.lower() for role in _member_role_names(member, role_names)]
    return any(any(marker in role for marker in staff_markers) for role in roles)


def _format_member_label(target_id: str, member: Optional[dict]) -> str:
    display_name = _member_display_name(member, target_id).lstrip("@")
    if display_name == target_id:
        return f"<@{target_id}>"
    return f"<@{target_id}> (@{display_name})"


def _channel_created_unix(channel_id: object) -> Optional[int]:
    try:
        return int(discord.utils.snowflake_time(int(channel_id)).timestamp())
    except Exception:
        return None


def _ticket_access_summary(
    c: dict,
    *,
    guild_id: str,
    role_names: dict[str, str],
) -> tuple[str, str, str, str]:
    overwrites = c.get("permission_overwrites") or []
    if not isinstance(overwrites, list):
        return "Unknown", "Unknown", "Unknown", "Unknown"

    reporter_names: list[str] = []
    staff_names: list[str] = []
    unknown_member_names: list[str] = []
    allowed_roles: list[str] = []
    denied_roles: list[str] = []

    for overwrite in overwrites:
        if not isinstance(overwrite, dict):
            continue
        target_id = str(overwrite.get("id") or "")
        target_type = overwrite.get("type")
        try:
            allow = int(overwrite.get("allow") or 0)
            deny = int(overwrite.get("deny") or 0)
        except Exception:
            allow = 0
            deny = 0

        if target_type == 0:
            role_name = role_names.get(target_id, target_id)
            if deny & _VIEW_CHANNEL:
                denied_roles.append(role_name)
            elif allow & _VIEW_CHANNEL:
                allowed_roles.append(role_name)
            continue

        if target_type != 1:
            continue

        if target_id == _TICKET_TOOL_USER_ID:
            continue
        member = _fetch_guild_member_cached(guild_id, target_id)
        label = _format_member_label(target_id, member)
        if member is None:
            unknown_member_names.append(label)
        elif _is_staff_member(member, role_names):
            staff_names.append(label)
        else:
            reporter_names.append(label)

    if not reporter_names and unknown_member_names:
        reporter_names.append(unknown_member_names.pop(0))
    if unknown_member_names:
        staff_names.extend(unknown_member_names)
    if not staff_names and allowed_roles:
        staff_names.append("Role-based access")

    return (
        _format_name_list(reporter_names),
        _format_name_list(staff_names),
        _format_role_list(allowed_roles),
        _format_role_list(denied_roles),
    )


def _split_ticket_name(name: str) -> tuple[str, list[str]]:
    cleaned = (name or "").strip()
    if "-b-" not in cleaned:
        return cleaned, []

    exploiter, boosters_raw = cleaned.split("-b-", 1)
    boosters = [part.strip() for part in boosters_raw.split("-") if part.strip()]
    return exploiter.strip() or cleaned, boosters


def _has_member_overwrite(c: dict, user_id: str) -> bool:
    overwrites = c.get("permission_overwrites") or []
    if not isinstance(overwrites, list):
        return False
    for overwrite in overwrites:
        if not isinstance(overwrite, dict):
            continue
        if overwrite.get("type") == 1 and str(overwrite.get("id") or "") == user_id:
            return True
    return False


def _category_map(channels: list[dict]) -> dict[str, str]:
    categories: dict[str, str] = {}
    for c in channels:
        try:
            if int(c.get("type", -1)) == 4 and c.get("id"):
                categories[str(c["id"])] = c.get("name") or "?"
        except Exception:
            continue
    return categories


def _report_channels_for_guild(guild_id: str, channels: list[dict]) -> list[dict]:
    allowed_category_ids = _REPORT_CATEGORY_IDS_BY_GUILD.get(str(guild_id), set())
    if not allowed_category_ids:
        guild_category_ids = {
            str(c.get("id"))
            for c in channels
            if str(c.get("id") or "") in _KNOWN_REPORT_CATEGORY_IDS
        }
        allowed_category_ids = guild_category_ids

    if not allowed_category_ids:
        return channels

    return [
        c for c in channels
        if str(c.get("parent_id") or "") in allowed_category_ids
    ]


def _annotate_channel(
    c: dict,
    *,
    guild_id: str,
    guild_name: str,
    categories: dict[str, str],
    role_names: dict[str, str],
    channels_count: int,
) -> dict:
    annotated = dict(c)
    annotated["_guild_id"] = guild_id
    annotated["_guild_name"] = guild_name
    annotated["_categories"] = categories
    annotated["_role_names"] = role_names
    annotated["_channels_count"] = channels_count
    return annotated


_RESULTS_PER_PAGE = 4
_REPORTER_RESULTS_PER_PAGE = 1


def _build_match_field_value(
    c: dict,
    *,
    categories: dict[str, str],
    guild_id: str,
    role_names: dict[str, str],
    emojis: dict[str, str],
) -> str:
    name = c.get("name") or "?"
    cid = c.get("id") or "—"
    match_guild_id = str(c.get("_guild_id") or guild_id)
    match_guild_name = str(c.get("_guild_name") or "")
    match_categories = c.get("_categories") if isinstance(c.get("_categories"), dict) else categories
    match_role_names = c.get("_role_names") if isinstance(c.get("_role_names"), dict) else role_names
    parent_id = c.get("parent_id")
    parent_label = match_categories.get(str(parent_id)) if parent_id else None
    topic = c.get("topic")
    ticket_url = f"https://discord.com/channels/{match_guild_id}/{cid}"
    created_unix = _channel_created_unix(cid)
    exploiter, boosters = _split_ticket_name(name)
    boosters_text = ", ".join(boosters) if boosters else "None"
    reporter, ticket_staff, _, _ = _ticket_access_summary(
        c,
        guild_id=match_guild_id,
        role_names=match_role_names,
    )

    value_lines = [
        f"**Ticket:** [Open ticket]({ticket_url})",
        *([f"**Server:** {discord.utils.escape_markdown(match_guild_name)}"] if match_guild_name else []),
        f"**Exploiters:** {discord.utils.escape_markdown(exploiter)}",
        f"**Boosters:** {discord.utils.escape_markdown(boosters_text)}",
        "",
        f"**Reporter:** {reporter}",
        f"**Ticket Staff:** {ticket_staff}",
        "",
    ]
    if parent_label:
        value_lines.append(f"{emojis['lock']} **Category:** {parent_label}")
    if created_unix is not None:
        value_lines.append(f"{emojis['clipboard']} **Created:** <t:{created_unix}:F>")
    if topic:
        snippet = topic.replace("\n", " ")
        if len(snippet) > 200:
            snippet = snippet[:197] + "…"
        value_lines.append(f"📝 **Topic:** {snippet}")

    field_value = "\n".join(value_lines)
    if len(field_value) > 1024:
        field_value = field_value[:1021] + "…"
    return field_value


def _build_matches_embed(
    *,
    cleaned: str,
    guild_name: str,
    channels_count: int,
    matches: list[dict],
    categories: dict[str, str],
    guild_id: str,
    role_names: dict[str, str],
    emojis: dict[str, str],
    page: int,
) -> discord.Embed:
    total_pages = max(1, (len(matches) + _RESULTS_PER_PAGE - 1) // _RESULTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * _RESULTS_PER_PAGE
    page_matches = matches[start:start + _RESULTS_PER_PAGE]

    embed = discord.Embed(
        title=f"{emojis['primary']} {emojis['mag']} BanCheck — {cleaned}",
        description=(
            f"{emojis['secondary']} **{len(matches)}** matching ticket(s) found for {cleaned} "
            f"in **{guild_name}** (out of **{channels_count}** channels)."
        ),
        color=discord.Color.red(),
    )

    embed.set_footer(text=f"Page {page + 1}/{total_pages}")

    for offset, c in enumerate(page_matches, start=start + 1):
        channel_name = discord.utils.escape_markdown(str(c.get("name") or "unknown-ticket"))
        embed.add_field(
            name=f"{emojis['exploiters']} Report {offset}: #{channel_name}",
            value=_build_match_field_value(
                c,
                categories=categories,
                guild_id=guild_id,
                role_names=role_names,
                emojis=emojis,
            ),
            inline=False,
        )

    return embed


class BanCheckResultsView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        cleaned: str,
        guild_name: str,
        channels_count: int,
        matches: list[dict],
        categories: dict[str, str],
        guild_id: str,
        role_names: dict[str, str],
        emojis: dict[str, str],
    ):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.cleaned = cleaned
        self.guild_name = guild_name
        self.channels_count = channels_count
        self.matches = matches
        self.categories = categories
        self.guild_id = guild_id
        self.role_names = role_names
        self.emojis = emojis
        self.page = 0
        self.total_pages = max(1, (len(matches) + _RESULTS_PER_PAGE - 1) // _RESULTS_PER_PAGE)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.previous_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        return _build_matches_embed(
            cleaned=self.cleaned,
            guild_name=self.guild_name,
            channels_count=self.channels_count,
            matches=self.matches,
            categories=self.categories,
            guild_id=self.guild_id,
            role_names=self.role_names,
            emojis=self.emojis,
            page=self.page,
        )

    async def _turn_page(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                "Only the command runner can use these buttons.",
                ephemeral=True,
            )
            return
        self.page = max(0, min(self.total_pages - 1, self.page + delta))
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="bancheckv2:previous")
    async def previous_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn_page(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="bancheckv2:next")
    async def next_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn_page(interaction, 1)


def _reporter_classification(member: Optional[dict], role_names: dict[str, str]) -> str:
    if not isinstance(member, dict):
        return "Unknown"
    return "Staff / role-holder" if _is_staff_member(member, role_names) else "Reporter / non-staff"


def _format_member_roles(member: Optional[dict], role_names: dict[str, str], *, limit: int = 12) -> str:
    roles = _member_role_names(member, role_names)
    if not roles:
        return "Unknown"
    shown = roles[:limit]
    text = ", ".join(discord.utils.escape_markdown(role) for role in shown)
    if len(roles) > limit:
        text += f", +{len(roles) - limit} more"
    return text


def _classify_user_role(member: Optional[dict]) -> str:
    """
    Return a single role label for the looked-up user, picked from
    STAFF_ROLE_HIERARCHY by highest priority (top of list wins).
    Falls back to "Normal Reporter" for users with no staff roles.
    """
    if not isinstance(member, dict):
        return "Unknown"
    held_role_ids = {str(rid) for rid in member.get("roles", [])}
    for role_id, display_name in STAFF_ROLE_HIERARCHY:
        if role_id in held_role_ids:
            return display_name
    return "Normal Reporter"


def _build_reporter_match_value(
    c: dict,
    *,
    categories: dict[str, str],
    guild_id: str,
    emojis: dict[str, str],
) -> str:
    name = c.get("name") or "?"
    ctype = _channel_type_label(c.get("type"))
    cid = c.get("id") or "—"
    match_guild_id = str(c.get("_guild_id") or guild_id)
    match_guild_name = str(c.get("_guild_name") or "")
    match_categories = c.get("_categories") if isinstance(c.get("_categories"), dict) else categories
    parent_id = c.get("parent_id")
    parent_label = match_categories.get(str(parent_id)) if parent_id else None
    ticket_url = f"https://discord.com/channels/{match_guild_id}/{cid}"
    created_unix = _channel_created_unix(cid)
    exploiter, boosters = _split_ticket_name(name)
    boosters_text = ", ".join(boosters) if boosters else "None"

    value_lines = [
        f"**Channel:** {discord.utils.escape_markdown(str(name))}",
        f"**Link:** [Open ticket]({ticket_url})",
    ]
    if match_guild_name:
        value_lines.append(f"**Server:** {discord.utils.escape_markdown(match_guild_name)}")
    if "-b-" in str(name):
        value_lines.extend([
            f"**Exploiters:** {discord.utils.escape_markdown(exploiter)}",
            f"**Boosters:** {discord.utils.escape_markdown(boosters_text)}",
        ])
    value_lines.append("")
    if parent_label:
        value_lines.append(f"{emojis['lock']} **Category:** {parent_label}")
    value_lines.append(f"{emojis['clipboard']} **Channel ID:** {cid}")
    if created_unix is not None:
        value_lines.append(f"{emojis['clipboard']} **Created:** <t:{created_unix}:F>")
    value_lines.append(f"{emojis['primary']} **Type:** {ctype}")

    field_value = "\n".join(value_lines)
    if len(field_value) > 1024:
        field_value = field_value[:1021] + "…"
    return field_value


def _build_reporter_embed(
    *,
    user_id: str,
    member: Optional[dict],
    role_names: dict[str, str],
    matches: list[dict],
    categories: dict[str, str],
    guild_id: str,
    emojis: dict[str, str],
    page: int,
) -> discord.Embed:
    total_pages = max(1, (len(matches) + _REPORTER_RESULTS_PER_PAGE - 1) // _REPORTER_RESULTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * _REPORTER_RESULTS_PER_PAGE
    page_matches = matches[start:start + _REPORTER_RESULTS_PER_PAGE]
    display_name = _member_display_name(member, user_id)
    role_label = _classify_user_role(member)
    is_staff = role_label != "Normal Reporter" and role_label != "Unknown"
    if matches:
        description = (
            f"{emojis['secondary']} Found **{len(matches)}** active channel access match(es) "
            f"for <@{user_id}>."
        )
    elif is_staff:
        description = (
            f"{emojis['secondary']} <@{user_id}> is **{role_label}**. "
            "No active reporter channel access matches were found."
        )
    else:
        description = (
            f"{emojis['secondary']} Found **0** active channel access match(es) "
            f"for <@{user_id}>."
        )

    embed = discord.Embed(
        title=f"{emojis['primary']} {emojis['mag']} ReporterCheck — {discord.utils.escape_markdown(display_name)}",
        description=description,
        color=discord.Color.red() if matches else discord.Color.green(),
    )
    embed.add_field(
        name=f"{emojis['exploiters']} **User:**",
        value=(
            f"**User:** <@{user_id}>\n"
            f"**User ID:** {user_id}\n"
            f"**Role:** {role_label}"
        ),
        inline=False,
    )

    for c in page_matches:
        embed.add_field(
            name=f"{emojis['exploiters']} **Active Report:**",
            value=_build_reporter_match_value(
                c,
                categories=categories,
                guild_id=guild_id,
                emojis=emojis,
            ),
            inline=False,
        )

    if not page_matches:
        empty_text = (
            "This user is moderation staff / a role-holder, not an active reporter match."
            if is_staff
            else "No active channel access matches found for this user."
        )
        embed.add_field(
            name=f"{emojis['success']} **Active Reports:**",
            value=empty_text,
            inline=False,
        )

    if total_pages > 1:
        embed.set_footer(text=f"Page {page + 1}/{total_pages}")
    return embed


class ReporterCheckResultsView(discord.ui.View):
    def __init__(
        self,
        *,
        owner_id: int,
        user_id: str,
        member: Optional[dict],
        role_names: dict[str, str],
        matches: list[dict],
        categories: dict[str, str],
        guild_id: str,
        emojis: dict[str, str],
    ):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.user_id = user_id
        self.member = member
        self.role_names = role_names
        self.matches = matches
        self.categories = categories
        self.guild_id = guild_id
        self.emojis = emojis
        self.page = 0
        self.total_pages = max(1, (len(matches) + _REPORTER_RESULTS_PER_PAGE - 1) // _REPORTER_RESULTS_PER_PAGE)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.previous_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        return _build_reporter_embed(
            user_id=self.user_id,
            member=self.member,
            role_names=self.role_names,
            matches=self.matches,
            categories=self.categories,
            guild_id=self.guild_id,
            emojis=self.emojis,
            page=self.page,
        )

    async def _turn_page(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                "Only the command runner can use these buttons.",
                ephemeral=True,
            )
            return
        self.page = max(0, min(self.total_pages - 1, self.page + delta))
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="reportercheck:previous")
    async def previous_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn_page(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="reportercheck:next")
    async def next_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn_page(interaction, 1)


class ExpiredBanCheckView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _expired(self, interaction: discord.Interaction):
        await interaction.respond(
            "This result panel expired during a bot restart. Run the command again to rebuild the pages.",
            ephemeral=True,
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="bancheckv2:previous")
    async def ban_prev(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="bancheckv2:next")
    async def ban_next(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="reportercheck:previous")
    async def reporter_prev(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="reportercheck:next")
    async def reporter_next(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)


# ══════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════

class BanCheckCog(commands.Cog):
    """Search a moderation server's channel names for a Roblox username."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_app_command_error(
        self,
        ctx: discord.ApplicationContext,
        error: discord.DiscordException,
    ) -> None:
        try:
            message = f"⚠️ Command failed: {error}"
            await ctx.respond(message)
        except Exception:
            pass

    @discord.slash_command(contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel}, integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}, 
        name="bancheckv2",
        description="Check if a Roblox Bedwars username appears in the moderation server's ban tickets.",
    )
    @discord.option("username", description="The Roblox username to search for in the mod server's channel names",)
    async def bancheckv2(self, ctx: discord.ApplicationContext, username: str):
        await ctx.defer()

        from core.logging import log_command, log_inputs, log_result, log_user_first_use
        log_user_first_use(ctx, "bancheckv2")
        log_command(ctx, "bancheckv2")
        log_inputs(ctx, "bancheckv2", {"username": username})

        # Always-on tracking (matches the rest of the bot's commands).
        try:
            track_discord_user(
                ctx.author.id,
                username=str(ctx.author),
                avatar_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
            )
        except Exception:
            pass

        cleaned = (username or "").strip().lstrip("@")
        if not cleaned:
            await ctx.respond("Provide a username to search for.")
            log_result(ctx, "bancheckv2", False, "Empty username")
            try:
                track_command(ctx.author.id, "bancheckv2", success=False, summary="Empty username")
            except Exception:
                pass
            return

        guild_ids = _configured_guild_ids()
        if not guild_ids:
            await ctx.respond(
                "⚠️ The mod server isn't configured. Set `MOD_SERVER_GUILD_IDS` "
                "and `MOD_DISCORD_USER_TOKEN_BANCHECK` env vars in Railway."
            )
            try:
                log_result(ctx, "bancheckv2", False, "Mod server not configured")
                track_command(ctx.author.id, "bancheckv2", success=False, summary="Mod server not configured")
            except Exception:
                pass
            return

        needle = cleaned.lower()
        matches = []
        searched_channels = 0
        searched_guild_names: list[str] = []
        fetch_errors: list[str] = []

        for guild_id in guild_ids:
            channels, err = await asyncio.to_thread(_get_channels_cached, guild_id)
            if err or not channels:
                fetch_errors.append(f"{guild_id}: {err or 'No channels returned.'}")
                continue

            guild_name = _cached_guild_name(guild_id) or guild_id
            searched_guild_names.append(guild_name)
            categories = _category_map(channels)
            report_channels = _report_channels_for_guild(guild_id, channels)
            searched_channels += len(report_channels)
            guild_matches = [
                c for c in report_channels
                if needle in str(c.get("name") or "").lower()
            ]
            if not guild_matches:
                continue

            role_names = await asyncio.to_thread(_fetch_guild_roles_cached, guild_id)
            matches.extend(
                _annotate_channel(
                    c,
                    guild_id=guild_id,
                    guild_name=guild_name,
                    categories=categories,
                    role_names=role_names,
                    channels_count=len(report_channels),
                )
                for c in guild_matches
            )

        if not searched_channels:
            err_text = "; ".join(fetch_errors) if fetch_errors else "No channels returned."
            await ctx.respond(f"Warning: {err_text}")
            try:
                log_result(ctx, "bancheckv2", False, err_text)
                track_command(ctx.author.id, "bancheckv2", success=False, summary=err_text)
            except Exception:
                pass
            return

        guild_name = _format_guild_list(searched_guild_names)
        emojis = _emojis()
        EMOJI_PRIMARY = emojis["primary"]
        EMOJI_MAG = emojis["mag"]
        EMOJI_SUCCESS = emojis["success"]
        EMOJI_SECONDARY = emojis["secondary"]

        if not matches:
            embed = discord.Embed(
                title=f"{EMOJI_PRIMARY} {EMOJI_MAG} BanCheck - {cleaned}",
                description=(
                    f"{EMOJI_SUCCESS} **No matches** for {cleaned} in **{guild_name}**.\n"
                    f"Searched **{searched_channels}** channels."
                ),
                color=discord.Color.green(),
            )
            await ctx.respond(embed=embed)
            log_result(
                ctx,
                "bancheckv2",
                True,
                f"`{cleaned}` - 0 matches across {searched_channels} channels",
            )
            try:
                track_command(
                    ctx.author.id,
                    "bancheckv2",
                    success=True,
                    summary=f"`{cleaned}` - 0 matches across {searched_channels} channels",
                )
            except Exception:
                pass
            return

        matches.sort(key=lambda c: (str(c.get("_guild_name") or ""), str(c.get("parent_id") or ""), c.get("name") or ""))
        view = BanCheckResultsView(
            owner_id=ctx.author.id,
            cleaned=cleaned,
            guild_name=guild_name,
            channels_count=searched_channels,
            matches=matches,
            categories={},
            guild_id=str(matches[0].get("_guild_id") or guild_ids[0]),
            role_names={},
            emojis=emojis,
        )
        try:
            send_kwargs = {
                "embed": await asyncio.to_thread(view.build_embed),
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if view.total_pages > 1:
                send_kwargs["view"] = view
            await ctx.respond(**send_kwargs)
            result_summary = f"`{cleaned}` - {len(matches)} match(es) across {searched_channels} channels"
        except Exception as e:
            fallback = discord.Embed(
                title=f"{EMOJI_PRIMARY} {EMOJI_MAG} BanCheck - {cleaned}",
                description=(
                    f"{EMOJI_SECONDARY} Found **{len(matches)}** matching ticket(s), "
                    f"but couldn't render ticket metadata: {e}"
                ),
                color=discord.Color.red(),
            )
            await ctx.respond(embed=fallback)
            result_summary = (
                f"`{cleaned}` - {len(matches)} match(es), but metadata render failed: {e}"
            )
        log_result(ctx, "bancheckv2", True, result_summary)

        try:
            track_command(
                ctx.author.id,
                "bancheckv2",
                success=True,
                summary=f"`{cleaned}` - {len(matches)} match(es) across {searched_channels} channels",
            )
        except Exception:
            pass
        return

    @discord.slash_command(contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel}, integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}, 
        name="reportercheck",
        description="Check which active ticket/report channels a Discord user has access to.",
    )
    @discord.option("user_id", description="Discord user ID to search for in ticket/report channel access",)
    async def reportercheck(self, ctx: discord.ApplicationContext, user_id: str):
        await ctx.defer()

        from core.logging import log_command, log_inputs, log_result, log_user_first_use
        log_user_first_use(ctx, "reportercheck")
        log_command(ctx, "reportercheck")
        log_inputs(ctx, "reportercheck", {"user_id": user_id})

        try:
            track_discord_user(
                ctx.author.id,
                username=str(ctx.author),
                avatar_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
            )
        except Exception:
            pass

        cleaned_user_id = (user_id or "").strip().strip("<@!>").strip(">")
        if not cleaned_user_id.isdigit():
            await ctx.respond("Provide a valid Discord user ID.")
            log_result(ctx, "reportercheck", False, "Invalid user ID")
            try:
                track_command(ctx.author.id, "reportercheck", success=False, summary="Invalid user ID")
            except Exception:
                pass
            return

        guild_ids = _configured_guild_ids()
        if not guild_ids:
            await ctx.respond(
                "⚠️ The mod server isn't configured. Set `MOD_SERVER_GUILD_IDS` "
                "and `MOD_DISCORD_USER_TOKEN_BANCHECK` env vars in Railway."
            )
            try:
                log_result(ctx, "reportercheck", False, "Mod server not configured")
                track_command(ctx.author.id, "reportercheck", success=False, summary="Mod server not configured")
            except Exception:
                pass
            return

        matches = []
        searched_channels = 0
        member: Optional[dict] = None
        role_names_for_member: dict[str, str] = {}
        fetch_errors: list[str] = []

        for guild_id in guild_ids:
            channels, err = await asyncio.to_thread(_get_channels_cached, guild_id)
            if err or not channels:
                fetch_errors.append(f"{guild_id}: {err or 'No channels returned.'}")
                continue

            guild_name = _cached_guild_name(guild_id) or guild_id
            guild_role_names = await asyncio.to_thread(_fetch_guild_roles_cached, guild_id)
            guild_member = await asyncio.to_thread(_fetch_guild_member_cached, guild_id, cleaned_user_id)
            if member is None and guild_member is not None:
                member = guild_member
                role_names_for_member = guild_role_names

            categories = _category_map(channels)
            report_channels = _report_channels_for_guild(guild_id, channels)
            searched_channels += len(report_channels)
            guild_matches = [
                c for c in report_channels
                if _has_member_overwrite(c, cleaned_user_id)
            ]
            matches.extend(
                _annotate_channel(
                    c,
                    guild_id=guild_id,
                    guild_name=guild_name,
                    categories=categories,
                    role_names=guild_role_names,
                    channels_count=len(report_channels),
                )
                for c in guild_matches
            )

        if not searched_channels:
            err_text = "; ".join(fetch_errors) if fetch_errors else "No channels returned."
            await ctx.respond(f"Warning: {err_text}")
            try:
                log_result(ctx, "reportercheck", False, err_text)
                track_command(ctx.author.id, "reportercheck", success=False, summary=err_text)
            except Exception:
                pass
            return

        matches.sort(key=lambda c: (str(c.get("_guild_name") or ""), str(c.get("parent_id") or ""), c.get("name") or ""))
        emojis = _emojis()
        EMOJI_PRIMARY = emojis["primary"]
        EMOJI_MAG = emojis["mag"]

        view = ReporterCheckResultsView(
            owner_id=ctx.author.id,
            user_id=cleaned_user_id,
            member=member,
            role_names=role_names_for_member,
            matches=matches,
            categories={},
            guild_id=str(matches[0].get("_guild_id") if matches else guild_ids[0]),
            emojis=emojis,
        )
        try:
            send_kwargs = {
                "embed": await asyncio.to_thread(view.build_embed),
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if view.total_pages > 1:
                send_kwargs["view"] = view
            await ctx.respond(**send_kwargs)
            result_summary = f"{cleaned_user_id} - {len(matches)} active channel access match(es)"
        except Exception as e:
            fallback = discord.Embed(
                title=f"{EMOJI_PRIMARY} {EMOJI_MAG} ReporterCheck - {cleaned_user_id}",
                description=f"Found **{len(matches)}** match(es), but couldn't render metadata: {e}",
                color=discord.Color.red(),
            )
            await ctx.respond(embed=fallback)
            result_summary = (
                f"{cleaned_user_id} - {len(matches)} match(es), but metadata render failed: {e}"
            )
        log_result(ctx, "reportercheck", True, result_summary)

        try:
            track_command(
                ctx.author.id,
                "reportercheck",
                success=True,
                summary=f"{cleaned_user_id} - {len(matches)} active channel access match(es)",
            )
        except Exception:
            pass
        return


def setup(bot):
    bot.add_view(ExpiredBanCheckView())
    bot.add_cog(BanCheckCog(bot))
