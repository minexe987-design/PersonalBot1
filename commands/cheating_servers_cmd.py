# ──────────────────────────────────────────────────────────────────
# /in-cheating-servers — check if a Discord user is in known Roblox
# cheating/exploit servers.
#
# Multi-token sharded scan. The watched servers are split across
# scanner user-account tokens, see core/discord_pool.py. Each token only
# scans its own subset; the tokens run in parallel, so total
# wall-clock matches the slowest token (~8 jittered probes).
#
# To update the watch list:
#   1) Add the new invite to scripts/resolve_invites.py and run it.
#   2) Append the (guild_id, name) pair to CHEATING_SERVERS below.
#   3) Update TOKEN_SERVER_RANGES in core/discord_pool.py so the
#      new index is owned by whichever account is in that server.
# ──────────────────────────────────────────────────────────────────

import asyncio
import hashlib
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
import discord
from discord.ext import commands
from discord.ext import commands

from core import discord_pool
from core.logging import (
    log_command,
    log_inputs,
    log_result,
    log_user_first_use,
)
from core.tracking import track_command, track_discord_user
from core.tracking import (
    get_former_cheating_server_hits,
    is_cheating_server_user_whitelisted,
    track_cheating_server_scan,
    whitelist_cheating_server_user,
)


# ── Watch list (TOS-risky — see CLAUDE.md) ───────────────────────
# (guild_id, display_name) pairs. ORDER MATTERS — TOKEN_SERVER_RANGES
# in core/discord_pool.py uses 1-indexed positions in this list to
# decide which token covers which server. If you reorder this list,
# update those ranges or the wrong token will be used.
CHEATING_SERVERS: list[tuple[str, str]] = [
    ("1329189629466771577", ".Z (Public Beta)"),                  # 1
    ("1395020539915014185", "Seliware Community"),                # 2
    ("1448237723352825984", "Volt"),                              # 3
    ("1376842062007111750", "Water"),                             # 4
    ("1289988589052104846", "Potassium"),                         # 5
    ("1483453559692595252", "Madium"),                            # 6
    ("943223926509699072", "Velocity"),                           # 7
    ("1289659915790450849", "Xeno"),                              # 8
    ("1352066418744754318", "Ronix Studios"),                     # 9
    ("1147089171723321454", "WhatExpsAre.Online (WEAO)"),         # 10
    ("1369402831437828176", "Cosmic"),                            # 11
    ("1262951163943452723", "Raptor Development LLC"),            # 12
    ("1253107828835483679", "Opiumware [MacOS]"),                 # 13
    ("1221935816515911850", "Delta Dynamics"),                    # 14
    ("1324451703302651976", "Cryptic Studios 🌙"),                # 15
    ("1069840556307525772", "1 F0 : Community (Vega X)"),         # 16
    ("978632997425283092", "Codex Collective"),                   # 17
    ("1388071156032077906", "Serotonin"),                         # 18
    ("876072383033798667", "Severe"),                             # 19
    ("1425607951032258681", "RbxCli"),                            # 20
    ("1254157599457415208", "Ronin"),                             # 21
    ("1420041567481364703", "Matcha"),                            # 22
    ("1362053958398382130", "MTX Support"),                       # 23
    ("1010270696443740273", "Photon"),                            # 24
    ("920940258537910303", "Cult of Intellect™"),                 # 25
    ("1285204451413590027", "CheatsMarket | .gg/cheatsmarket"),   # 26
    ("1104033688938885120", "Alchemy Community"),                 # 27
    ("1104423636326166560", "Ilya Fun Club"),                     # 28
    ("1033461921921372291", "BloxProducts"),                      # 29
    ("1279483425002361003", "kHook"),                             # 30
    ("1473988645202563104", "*NEW* Cat Kingdom | Cat Vape"),      # 31
    ("1143463175019302942", "Voidware [Official] #100k 🎉"),      # 32
    ("1347244153305825374", "bunni.fun"),                         # 33
    ("1169780008374521856", "WeAreDevs"),                         # 34
    ("1038623510500753470", "Krnl"),                              # 35
    ("1456797021565354129", "AeroV4 Script"),                     # 36
    ("1418916177807413280", "FluxusZ | Community"),               # 37
    ("1454779122667491445", "Falcon"),                            # 38
    ("1134492999385108500", "RoXploits | Community Server"),      # 39
    ("942431667807735888", "Roblox Scripts!"),                    # 40
    ("919032826924515388", "Fluxus Windows - Support"),           # 41
    ("950143556641755187", "Arceus X Scripts"),                   # 42
    ("991702878257422347", "Arceus X | Intelligent Units"),       # 43
    ("1336720839881785417", "Volcano"),                            # 44
    ("1207420995112140950", "Wyv's Community"),                    # 45
    ("1497654383234515131", "projectreal"),                       # 46
]

# Bot-owner Discord IDs — searches against these get a hardcoded clean
# response (skipping the actual scan). Same set as the userinfo /
# monitor whitelists.
MIN_USER_IDS: set[str] = {
    "1338186029194154087",
    "930861591350624286",
    "1331949475467493448",
}

# Only this account can add users to the /in-cheating-servers whitelist.
CHEATING_SERVER_WHITELIST_MANAGER_IDS: set[str] = {
    "1338186029194154087",
}

# Forced-positive Discord IDs — searches against these always show the
# user as being in EVERY watched cheating server (used as a joke /
# call-out target). Skips the real scan entirely.
TROLL_USER_IDS: set[str] = {
    "1179926268163145828",
}

CUSTOM_LEGIT_USER_IDS: set[str] = set()
CUSTOM_LEGIT_NAMES: set[str] = {
    "schnitzel0606",
}
CUSTOM_LEGIT_MESSAGE = "schnitzel0606 totally plays legit 🙂"

# Pagination — number of cheating-server hits shown per embed page.
RESULTS_PER_PAGE = 5

# Queue / cooldown controls. The scanner remains sharded inside a
# single job, but jobs are serialized so scanner accounts do not get
# hit by overlapping command runs.
USER_COOLDOWN_SECONDS = 40.0
MAX_SCAN_BACKLOG = 5
ESTIMATED_SCAN_SECONDS = 18
COOLDOWN_MESSAGE = "Stop spamming my shi twin :v: it will break the bot thx"
QUEUE_FULL_MESSAGE = "Theres too many users rn, we'll get back to your request baby"
HISTORY_MESSAGE_SEARCH_ENABLED = (
    os.environ.get("CHEATING_HISTORY_MESSAGE_SEARCH", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)

# ── Emojis (re-using the existing palette) ───────────────────────
ARROW = "<a:anipinkarrow:1497344028004581386>"
GREEN_CHECK = "<:greencheck:1497344048267137144>"
RED_CHECK = "<:redcheck:1497344057041752235>"
CLIPBOARD = "<:clipboard:1497344037294702762>"
CHECK = "<:check:1497344035696672959>"
X_EMOJI = "<:x:1497344061592436737>"
LOCK = "<:lock:1497344050078941344>"
MAG = "<a:mag:1497344052709036125>"
EXPLOITERS = "<a:exploiters:1498648559623344158>"
WARNING = "<:warning:1497344059017003079>"
INFERRED_MESSAGE = "<:name:1508808060066594896>"
PRIVATE_COMMAND_MESSAGE = (
    f"{LOCK} This command is private now. If you want access, shoot **miny_z** "
    "a DM. It takes too much maintenance to keep open publicly."
)

# ── Sharded scan (parallel across tokens, serial within a token) ─

def _search_message_sets_for_token(
    env_var: str,
    current_servers: list[tuple[str, str]],
    historical_servers: list[tuple[str, str]],
    user_id: str,
) -> dict:
    current_hits: list[dict] = []
    historical_hits: list[dict] = []
    errors: list[str] = []
    token_dead = False

    if current_servers:
        result = discord_pool.search_history_serial_for_token(env_var, current_servers, user_id)
        current_hits.extend(result.get("historical_hits") or [])
        errors.extend(result.get("errors") or [])
        token_dead = token_dead or bool(result.get("token_dead"))

    if historical_servers and not token_dead:
        result = discord_pool.search_history_serial_for_token(env_var, historical_servers, user_id)
        historical_hits.extend(result.get("historical_hits") or [])
        errors.extend(result.get("errors") or [])
        token_dead = token_dead or bool(result.get("token_dead"))

    for hit in current_hits:
        hit["confidence"] = "exact_current"
    for hit in historical_hits:
        hit.setdefault("confidence", "inferred_messages")

    return {
        "env_var": env_var,
        "token_dead": token_dead,
        "current_hits": current_hits,
        "historical_hits": historical_hits,
        "errors": errors,
    }


async def _scan_user_sharded(user_id: str) -> dict:
    """
    Fan the scan across scanner tokens, each handling its own subset
    (see TOKEN_SERVER_RANGES in core/discord_pool.py).
    Each token's serial loop runs in a thread; the threads run
    concurrently via asyncio.gather.

    Result shape matches the old single-token output so the embed
    builder doesn't need to change.
    """
    groups = discord_pool.group_servers_by_token(CHEATING_SERVERS)

    coros = [
        asyncio.to_thread(
            discord_pool.scan_user_serial_for_token, env_var, servers, user_id,
        )
        for env_var, servers in groups.items() if servers
    ]
    per_token_results = await asyncio.gather(*coros)

    in_servers: list[dict] = []
    not_in: list[str] = []
    checked_servers: list[dict] = []
    not_in_by_env: dict[str, list[tuple[str, str]]] = {}
    current_by_env: dict[str, list[tuple[str, str]]] = {}
    message_hits: list[dict] = []
    historical_hits: list[dict] = []
    history_errors: list[str] = []
    errors: list[str] = []
    dead_tokens: list[str] = []

    for r in per_token_results:
        in_servers.extend(r["in_servers"])
        not_in.extend(r["not_in"])
        checked_servers.extend(r.get("checked_servers", []))
        errors.extend(r["errors"])
        if r["token_dead"]:
            dead_tokens.append(r["env_var"])
        not_in_by_env[r["env_var"]] = [
            (entry["guild_id"], entry["guild_name"])
            for entry in r.get("checked_servers", [])
            if entry.get("in") is False
        ]
        current_by_env[r["env_var"]] = [
            (entry["guild_id"], entry["guild_name"])
            for entry in r.get("checked_servers", [])
            if entry.get("in") is True
        ]

    if HISTORY_MESSAGE_SEARCH_ENABLED:
        message_coros = [
            asyncio.to_thread(
                _search_message_sets_for_token,
                env_var,
                current_by_env.get(env_var, []),
                not_in_by_env.get(env_var, []),
                user_id,
            )
            for env_var in groups
            if current_by_env.get(env_var) or not_in_by_env.get(env_var)
        ]
        message_results = await asyncio.gather(*message_coros) if message_coros else []
        for r in message_results:
            message_hits.extend(r.get("current_hits") or [])
            historical_hits.extend(r.get("historical_hits") or [])
            history_errors.extend(r.get("errors") or [])
            if r.get("token_dead"):
                dead_tokens.append(r["env_var"])

    return {
        "all_tokens_dead": discord_pool.all_scanner_tokens_dead(),
        "dead_tokens": dead_tokens,
        "in_servers": in_servers,
        "not_in": not_in,
        "checked_servers": checked_servers,
        "message_hits": message_hits,
        "historical_hits": historical_hits,
        "history_errors": history_errors,
        "errors": errors,
        "uncovered_servers": discord_pool.temporarily_uncovered_servers(CHEATING_SERVERS),
    }


# ── Embed builder ────────────────────────────────────────────────

def _format_joined(joined_at: Optional[str]) -> str:
    if not joined_at:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(joined_at.replace("Z", "+00:00"))
        ts = int(dt.timestamp())
        return f"<t:{ts}:F> (<t:{ts}:R>)"
    except Exception:
        return joined_at


def _format_unix(ts: Optional[int]) -> str:
    if not ts:
        return "Unknown"
    try:
        ts = int(ts)
        return f"<t:{ts}:F> (<t:{ts}:R>)"
    except Exception:
        return str(ts)


def _has_message_evidence_sample(hit: dict) -> bool:
    if hit.get("messages"):
        return True
    for key in ("total_messages", "total_results"):
        try:
            if int(hit.get(key) or 0) > 0:
                return True
        except Exception:
            continue
    return False


def _message_evidence_hits(result: dict) -> list[dict]:
    hits: list[dict] = []
    seen_guilds: set[str] = set()
    current_by_guild = {
        str(hit.get("guild_id") or ""): hit
        for hit in (result.get("in_servers") or [])
        if hit.get("guild_id")
    }
    previous_by_guild = {
        str(hit.get("guild_id") or ""): hit
        for hit in (result.get("previously_seen") or [])
        if hit.get("guild_id")
    }

    sample_hits = (result.get("message_hits") or []) + (result.get("historical_hits") or [])
    for sample in sample_hits:
        guild_id = str(sample.get("guild_id") or "")
        if not guild_id or guild_id in seen_guilds or not _has_message_evidence_sample(sample):
            continue
        seen_guilds.add(guild_id)
        base = current_by_guild.get(guild_id) or previous_by_guild.get(guild_id) or {}
        confidence = (
            "exact_current"
            if guild_id in current_by_guild
            else (base.get("confidence") or sample.get("confidence") or "inferred_messages")
        )
        hits.append({**base, **sample, "confidence": confidence})

    for hit in (result.get("previously_seen") or []):
        guild_id = str(hit.get("guild_id") or "")
        if not guild_id or guild_id in seen_guilds or not _has_message_evidence_sample(hit):
            continue
        confidence = (hit.get("confidence") or "").lower()
        if confidence not in {"exact_historical", "inferred_messages"}:
            continue
        seen_guilds.add(guild_id)
        hits.append(hit)
    return hits


async def _send_message_history(
    ctx: discord.ApplicationContext,
    *,
    owner_id: int,
    target_id: str,
    hit: dict,
) -> None:
    guild_name = hit.get("guild_name") or "Unknown server"
    guild_id = hit.get("guild_id") or "?"
    env_var = hit.get("env_var") or _env_for_guild_id(str(guild_id))
    if not env_var:
        await ctx.respond(
            f"Could not resolve a scanner token for **{guild_name}**.",
            ephemeral=True,
        )
        return

    loading_message = await ctx.respond(
        f"Fetching messages for **{guild_name}**...",
        ephemeral=True,
        wait=True,
    )
    print(
        f"[IN-CHEATING/MESSAGES] start target={target_id} "
        f"guild={guild_name} ({guild_id}) env={env_var}",
        flush=True,
    )

    sample_messages = hit.get("messages") or []
    if sample_messages:
        try:
            total_messages = int(hit.get("total_messages") or len(sample_messages))
        except Exception:
            total_messages = len(sample_messages)
        page_result = {
            "messages": sample_messages,
            "total_results": total_messages,
            "next_offset": 25 if total_messages > len(sample_messages) else None,
        }
    else:
        page_result = await asyncio.to_thread(
            discord_pool.fetch_user_message_history_page,
            env_var,
            str(guild_id),
            target_id,
            offset=0,
        )

    if page_result.get("error") and not page_result.get("messages"):
        print(
            f"[IN-CHEATING/MESSAGES] error target={target_id} "
            f"guild={guild_name} ({guild_id}) env={env_var} error={page_result['error']}",
            flush=True,
        )
        await loading_message.edit(
            content=f"Could not fetch messages for **{guild_name}**: {page_result['error']}",
            embed=None,
            view=None,
        )
        return

    print(
        f"[IN-CHEATING/MESSAGES] ok target={target_id} "
        f"guild={guild_name} ({guild_id}) env={env_var} "
        f"messages={len(page_result.get('messages') or [])} "
        f"total={page_result.get('total_results')} next={page_result.get('next_offset')}",
        flush=True,
    )
    view = MessageHistoryView(
        owner_id=owner_id,
        env_var=env_var,
        guild_id=str(guild_id),
        guild_name=guild_name,
        target_id=target_id,
        messages=page_result.get("messages") or [],
        total_results=int(page_result.get("total_results") or 0),
        next_offset=page_result.get("next_offset"),
    )
    await loading_message.edit(
        content=None,
        embed=view.build_embed(),
        view=view if view.has_multiple_pages else None,
    )


def _attach_message_samples(result: dict, rows: list[dict]) -> list[dict]:
    sample_hits = (result.get("message_hits") or []) + (result.get("historical_hits") or [])
    samples_by_guild = {
        str(hit.get("guild_id") or ""): {
            "messages": hit.get("messages") or [],
            "total_messages": hit.get("total_messages"),
            "env_var": hit.get("env_var"),
        }
        for hit in sample_hits
        if hit.get("guild_id")
    }
    for row in rows:
        data = samples_by_guild.get(str(row.get("guild_id") or ""), {})
        if not data:
            continue
        row["messages"] = data.get("messages") or []
        row["total_messages"] = data.get("total_messages")
        row["env_var"] = data.get("env_var") or row.get("env_var")
    return rows


def _env_for_guild_id(guild_id: str) -> Optional[str]:
    index = next(
        (
            i
            for i, (known_guild_id, _name) in enumerate(CHEATING_SERVERS, start=1)
            if known_guild_id == str(guild_id)
        ),
        None,
    )
    if index is None:
        return None
    for env_var, indices in discord_pool.TOKEN_SERVER_RANGES.items():
        if index in indices:
            return env_var
    return None


_MESSAGE_TYPE_LABELS = {
    0: "Message",
    1: "Added recipient",
    2: "Recipient removed",
    3: "Call",
    4: "Channel name changed",
    5: "Channel icon changed",
    6: "Pinned a message",
    7: "Joined the server",
    8: "Boosted the server",
    9: "Boost level 1",
    10: "Boost level 2",
    11: "Boost level 3",
    12: "Channel followed",
    18: "Thread created",
    19: "Reply",
    20: "Slash command",
    21: "Thread starter",
    23: "Context menu command",
    24: "AutoMod action",
}


def _format_message_body(msg: dict) -> str:
    content = (msg.get("content") or "").strip()
    if content:
        return content

    parts: list[str] = []
    message_type = msg.get("type")
    label = _MESSAGE_TYPE_LABELS.get(message_type)
    if label and message_type != 0:
        parts.append(label)

    stickers = msg.get("stickers") or []
    if stickers:
        parts.append("Sticker: " + ", ".join(str(s) for s in stickers))

    attachments = msg.get("attachments") or []
    if attachments:
        names = [att.get("filename") or "attachment" for att in attachments]
        parts.append("Attachment: " + ", ".join(names))

    embeds = msg.get("embeds") or []
    for embed in embeds:
        title = embed.get("title")
        description = embed.get("description")
        embed_type = embed.get("type") or "embed"
        if title:
            parts.append(f"Embed ({embed_type}): {title}")
        elif description:
            parts.append(f"Embed ({embed_type}): {description}")
        else:
            parts.append(f"Embed ({embed_type})")

    return "\n".join(parts) if parts else "Message has no text body"


_FAKE_JOIN_BASE = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
_FAKE_JOIN_SPAN_SECONDS = 60 * 60 * 24 * 365 * 2  # ~2 years of plausible window


def _fake_join_date(user_id: str, guild_id: str) -> str:
    """
    Fully-deterministic plausible-looking join date for forced-positive
    targets. Seeded by (user_id, guild_id) so the same user shows the
    same dates every time the command is run, regardless of when.
    """
    seed = int(hashlib.sha256(f"{user_id}:{guild_id}".encode()).hexdigest()[:16], 16)
    ts = _FAKE_JOIN_BASE + (seed % _FAKE_JOIN_SPAN_SECONDS)
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _build_troll_result(user_id: str) -> dict:
    """Fabricate a 'found in every watched server' result for TROLL_USER_IDS."""
    in_servers = [
        {
            "guild_id": gid,
            "guild_name": gname,
            "joined_at": _fake_join_date(user_id, gid),
        }
        for gid, gname in CHEATING_SERVERS
    ]
    return {
        "token_dead": False,
        "in_servers": in_servers,
        "not_in": [],
        "errors": [],
    }


def _total_pages(in_servers: list) -> int:
    return max(1, (len(in_servers) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)


def _build_embed(
    target_id: str,
    target_label: str,
    result: dict,
    *,
    page: int = 0,
) -> discord.Embed:
    in_servers = result["in_servers"]
    previously_seen = result.get("previously_seen") or []
    exact_previous = [
        h for h in previously_seen
        if (h.get("confidence") or "").lower() == "exact_historical"
    ]
    message_evidence = [
        h for h in previously_seen
        if (h.get("confidence") or "").lower() == "inferred_messages"
    ]
    errors = result["errors"]
    total_pages = _total_pages(in_servers)
    page = max(0, min(page, total_pages - 1))

    if in_servers:
        color = discord.Color.red()
        description = (
            f"{EXPLOITERS} {target_label} is in **{len(in_servers)}** "
            f"watched cheating server(s)."
        )
    elif exact_previous:
        color = discord.Color.gold()
        description = (
            f"{WARNING} {target_label} is **not currently in** any watched cheating "
            f"servers, but was previously seen in **{len(exact_previous)}**."
        )
    elif message_evidence:
        color = discord.Color.gold()
        description = (
            f"{WARNING} {target_label} is **not currently in** any watched cheating "
            f"servers, but has message history in **{len(message_evidence)}**."
        )
    else:
        color = discord.Color.green()
        description = (
            f"{GREEN_CHECK} {target_label} is **NOT** in any of the watched cheating servers."
        )

    embed = discord.Embed(
        title=f"{ARROW} {MAG} In-Cheating-Servers Check",
        description=description,
        color=color,
    )

    start = page * RESULTS_PER_PAGE
    for hit in in_servers[start:start + RESULTS_PER_PAGE]:
        gname = hit["guild_name"]
        gid = hit["guild_id"]
        joined = _format_joined(hit.get("joined_at"))
        embed.add_field(
            name=f"{EXPLOITERS} **{gname}**",
            value=(
                f"{LOCK} **Server ID:** `{gid}`\n"
                f"{CLIPBOARD} **Joined:** {joined}"
            ),
            inline=False,
        )

    if errors and page == 0:
        shown = errors[:8]
        body = "\n".join(f"• {e}" for e in shown)
        if len(errors) > 8:
            body += f"\n*(+{len(errors) - 8} more)*"
        embed.add_field(
            name=f"{WARNING} Errors during scan",
            value=body,
            inline=False,
        )

    if exact_previous and page == 0:
        rows = []
        for hit in exact_previous[:5]:
            name = hit.get("guild_name") or "Unknown server"
            gid = hit.get("guild_id") or "?"
            last_seen = _format_unix(hit.get("last_seen"))
            left_at = _format_unix(hit.get("left_at"))
            rows.append(
                f"{EXPLOITERS} **{name}**\n"
                f"{LOCK} **Server ID:** `{gid}`\n"
                f"{CLIPBOARD} **Last seen by scan:** {last_seen}\n"
                f"{WARNING} **No longer current as of:** {left_at}"
            )
        body = "\n\n".join(rows)
        if len(exact_previous) > 5:
            body += f"\n\n*(+{len(exact_previous) - 5} more previously seen)*"
        embed.add_field(
            name=f"{WARNING} Previously Seen",
            value=body,
            inline=False,
        )

    if message_evidence and page == 0:
        rows = []
        for hit in message_evidence[:5]:
            name = hit.get("guild_name") or "Unknown server"
            evidence_at = _format_unix(hit.get("last_seen"))
            rows.append(
                f"{INFERRED_MESSAGE} **{name}**\n"
                f"{CLIPBOARD} **was here at some point:** {evidence_at}"
            )
        body = "\n\n".join(rows)
        if len(message_evidence) > 5:
            body += f"\n\n*(+{len(message_evidence) - 5} more message-evidence hit(s))*"
        embed.add_field(
            name=f"{WARNING} Message History Evidence",
            value=body,
            inline=False,
        )

    if total_pages > 1:
        embed.set_footer(text=f"Page {page + 1}/{total_pages}")

    return embed


def _build_min_embed() -> discord.Embed:
    """Special-cased response when someone searches the bot owner's ID."""
    return discord.Embed(
        title=f"{ARROW} {MAG} In-Cheating-Servers Check",
        description=f"{GREEN_CHECK} **MIN** does **NOT** cheat vro....",
        color=discord.Color.green(),
    )


async def _is_custom_legit_target(
    bot: commands.Bot,
    target_id: str,
    target_user: Optional[discord.User],
) -> bool:
    if target_id in CUSTOM_LEGIT_USER_IDS:
        return True

    user = target_user
    if user is None:
        try:
            user = await bot.fetch_user(int(target_id))
        except Exception:
            user = None

    if user is None:
        return False

    names = {
        (getattr(user, "name", "") or "").lower(),
        (getattr(user, "global_name", "") or "").lower(),
        (getattr(user, "display_name", "") or "").lower(),
    }
    return bool(names & CUSTOM_LEGIT_NAMES)


def _is_in_cheating_servers_admin(user_id: int | str) -> bool:
    return str(user_id) in MIN_USER_IDS


def _can_manage_cheating_server_whitelist(user_id: int | str) -> bool:
    return str(user_id) in CHEATING_SERVER_WHITELIST_MANAGER_IDS


def _can_use_in_cheating_servers(user_id: int | str) -> bool:
    if _is_in_cheating_servers_admin(user_id) or _can_manage_cheating_server_whitelist(user_id):
        return True
    try:
        return is_cheating_server_user_whitelisted(user_id)
    except Exception:
        return False


def _clean_discord_id(raw: str) -> Optional[str]:
    cleaned = (raw or "").strip().strip("<@!>").strip(">")
    return cleaned if cleaned.isdigit() else None


async def _resolve_whitelist_target(
    bot: commands.Bot,
    ctx: commands.Context,
    raw_target: Optional[str],
) -> tuple[Optional[str], Optional[discord.User]]:
    if getattr(ctx.message, "mentions", None):
        user = ctx.message.mentions[0]
        return str(user.id), user

    target_id = _clean_discord_id(raw_target or "")
    if not target_id:
        return None, None

    user = bot.get_user(int(target_id))
    if user is None:
        try:
            user = await bot.fetch_user(int(target_id))
        except Exception:
            user = None
    return target_id, user


# ── Pagination view ──────────────────────────────────────────────

class CheatingServersView(discord.ui.View):
    """Prev/Next pagination for /in-cheating-servers results."""

    def __init__(self, *, owner_id: int, target_id: str, target_label: str, result: dict):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.target_id = target_id
        self.target_label = target_label
        self.result = result
        self.page = 0
        self.total_pages = _total_pages(result["in_servers"])
        if self.total_pages > 1:
            self._refresh_buttons()
        else:
            self.remove_item(self.previous_btn)
            self.remove_item(self.next_btn)
        if _message_evidence_hits(self.result):
            self.view_messages_btn.disabled = False
        else:
            self.remove_item(self.view_messages_btn)

    @property
    def has_controls(self) -> bool:
        return bool(self.children)

    def _refresh_buttons(self) -> None:
        self.previous_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1

    def build_embed(self) -> discord.Embed:
        return _build_embed(
            self.target_id,
            self.target_label,
            self.result,
            page=self.page,
        )

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                "Only the command runner can use these buttons.",
                ephemeral=True,
            )
            return
        self.page = max(0, min(self.total_pages - 1, self.page + delta))
        self._refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:prev")
    async def previous_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:next")
    async def next_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn(interaction, 1)

    @discord.ui.button(label="View Messages", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:messages", row=0)
    async def view_messages_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                "Only the command runner can use this button.",
                ephemeral=True,
            )
            return

        hits = _message_evidence_hits(self.result)
        if not hits:
            await interaction.respond(
                "No message evidence is available on this panel.",
                ephemeral=True,
            )
            return

        if len(hits) == 1:
            await interaction.response.defer(ephemeral=True)
            await _send_message_history(
                interaction,
                owner_id=self.owner_id,
                target_id=self.target_id,
                hit=hits[0],
            )
            return

        picker = MessageServerPickerView(
            owner_id=self.owner_id,
            target_id=self.target_id,
            hits=hits,
        )
        await interaction.respond(
            picker.content,
            view=picker,
            ephemeral=True,
        )


def _message_picker_description(confidence: str) -> str:
    if confidence == "exact_current":
        return "currently in server"
    if confidence == "exact_historical":
        return "previously seen"
    return "message evidence"


class MessageServerSelect(discord.ui.Select):
    def __init__(
        self,
        *,
        owner_id: int,
        target_id: str,
        hits: list[dict],
        start_index: int,
    ):
        self.owner_id = owner_id
        self.target_id = target_id
        self.hits_by_value: dict[str, dict] = {}
        options: list[discord.SelectOption] = []

        for offset, hit in enumerate(hits):
            absolute_index = start_index + offset
            value = str(absolute_index)
            name = hit.get("guild_name") or "Unknown server"
            confidence = (hit.get("confidence") or "").lower()
            self.hits_by_value[value] = hit
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=value,
                    description=_message_picker_description(confidence)[:100],
                )
            )

        super().__init__(
            placeholder="Select a server",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="incheatingservers:messages_select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        owner_id = getattr(view, "owner_id", self.owner_id)
        target_id = getattr(view, "target_id", self.target_id)
        if interaction.user.id != owner_id:
            await interaction.respond(
                "Only the command runner can use this menu.",
                ephemeral=True,
            )
            return

        hit = self.hits_by_value.get(self.values[0])
        if not hit:
            await interaction.respond("That server option expired.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await _send_message_history(
            interaction,
            owner_id=owner_id,
            target_id=target_id,
            hit=hit,
        )


class MessageServerPickerView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self, *, owner_id: int, target_id: str, hits: list[dict]):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.target_id = target_id
        self.hits = hits
        self.page = 0
        self.total_pages = max(1, math.ceil(len(self.hits) / self.PAGE_SIZE))
        self._server_select: MessageServerSelect | None = None
        self._refresh_items()

    @property
    def content(self) -> str:
        suffix = ""
        if self.total_pages > 1:
            suffix = f" Page {self.page + 1}/{self.total_pages}."
        return f"Choose a server to view messages.{suffix}"

    def _refresh_items(self) -> None:
        if self._server_select is not None:
            self.remove_item(self._server_select)
        start = self.page * self.PAGE_SIZE
        self._server_select = MessageServerSelect(
            owner_id=self.owner_id,
            target_id=self.target_id,
            hits=self.hits[start:start + self.PAGE_SIZE],
            start_index=start,
        )
        self.add_item(self._server_select)
        self.previous_servers_btn.disabled = self.page <= 0
        self.next_servers_btn.disabled = self.page >= self.total_pages - 1

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                "Only the command runner can use these buttons.",
                ephemeral=True,
            )
            return
        self.page = max(0, min(self.total_pages - 1, self.page + delta))
        self._refresh_items()
        await interaction.response.edit_message(content=self.content, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:server_prev", row=1)
    async def previous_servers_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:server_next", row=1)
    async def next_servers_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn(interaction, 1)


class MessageHistoryView(discord.ui.View):
    PAGE_SIZE = 4

    def __init__(
        self,
        *,
        owner_id: int,
        env_var: str,
        guild_id: str,
        guild_name: str,
        target_id: str,
        messages: list[dict],
        total_results: int,
        next_offset: Optional[int],
    ):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.env_var = env_var
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.target_id = target_id
        self.messages = messages
        self.total_results = total_results
        self.next_offset = next_offset
        self.page = 0
        self.total_pages = max(1, math.ceil(len(messages) / self.PAGE_SIZE))
        self._refresh_buttons()

    @property
    def has_multiple_pages(self) -> bool:
        return self.total_pages > 1 or self.next_offset is not None

    def _refresh_buttons(self) -> None:
        self.previous_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.total_pages - 1 and self.next_offset is None

    def build_embed(self) -> discord.Embed:
        description = f"Server: **{self.guild_name[:180]}**"
        if not self.messages:
            description += "\nNo messages were returned."

        embed = discord.Embed(
            title="Message Evidence",
            description=description,
            color=discord.Color.gold(),
        )

        start = self.page * self.PAGE_SIZE
        for msg in self.messages[start:start + self.PAGE_SIZE]:
            timestamp = _format_joined(msg.get("timestamp"))
            channel_name = (msg.get("channel_name") or "unknown-channel")[:80]
            content = _format_message_body(msg)
            value = f"Channel: `#{channel_name}`\n{content}"
            if len(value) > 950:
                value = value[:947] + "..."
            embed.add_field(
                name=timestamp[:250],
                value=value,
                inline=False,
            )

        total_text = f"{min(len(self.messages), self.total_results or len(self.messages))}/{self.total_results or len(self.messages)} message(s)"
        if self.next_offset is not None:
            total_text += " loaded"
        if self.total_pages > 1:
            total_text += f" | Page {self.page + 1}/{self.total_pages}"
        embed.set_footer(text=total_text)
        return embed

    async def _load_next_search_page(self) -> Optional[str]:
        if self.next_offset is None:
            return None
        result = await asyncio.to_thread(
            discord_pool.fetch_user_message_history_page,
            self.env_var,
            self.guild_id,
            self.target_id,
            offset=self.next_offset,
        )
        if result.get("error"):
            print(
                f"[IN-CHEATING/MESSAGES] page_error target={self.target_id} "
                f"guild={self.guild_name} ({self.guild_id}) env={self.env_var} "
                f"offset={self.next_offset} error={result.get('error')}",
                flush=True,
            )
            return str(result.get("error"))
        self.messages.extend(result.get("messages") or [])
        self.total_results = int(result.get("total_results") or self.total_results)
        self.next_offset = result.get("next_offset")
        self.total_pages = max(1, math.ceil(len(self.messages) / self.PAGE_SIZE))
        print(
            f"[IN-CHEATING/MESSAGES] page_ok target={self.target_id} "
            f"guild={self.guild_name} ({self.guild_id}) env={self.env_var} "
            f"loaded={len(self.messages)} total={self.total_results} next={self.next_offset}",
            flush=True,
        )
        return None

    async def _turn(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                "Only the command runner can use these buttons.",
                ephemeral=True,
            )
            return
        requested_page = self.page + delta
        if requested_page >= self.total_pages and self.next_offset is not None:
            await interaction.response.defer()
            err = await self._load_next_search_page()
            if err:
                await interaction.respond(
                    f"Could not load more messages: {err}",
                    ephemeral=True,
                )
                return
            self.page = max(0, min(self.total_pages - 1, requested_page))
            self._refresh_buttons()
            try:
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
            except Exception as exc:
                print(
                    f"[IN-CHEATING/MESSAGES] edit_error target={self.target_id} "
                    f"guild={self.guild_name} ({self.guild_id}) page={self.page + 1} "
                    f"error={exc}",
                    flush=True,
                )
                await interaction.respond(
                    f"Could not render this message page: {exc}",
                    ephemeral=True,
                )
            return

        self.page = max(0, min(self.total_pages - 1, requested_page))
        self._refresh_buttons()
        try:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        except Exception as exc:
            await interaction.respond(
                f"Could not render this message page: {exc}",
                ephemeral=True,
            )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:messages:page_prev")
    async def previous_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:messages:page_next")
    async def next_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn(interaction, 1)


class ExpiredCheatingServersView(discord.ui.View):
    """Persistent fallback so post-restart button clicks don't error out."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _expired(self, interaction: discord.Interaction):
        await interaction.respond(
            "This panel expired during a bot restart. Run the command again to rebuild it.",
            ephemeral=True,
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:prev")
    async def prev_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:next")
    async def next_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.button(label="View Messages", style=discord.ButtonStyle.secondary, custom_id="incheatingservers:messages")
    async def messages_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)


# ── Cog ──────────────────────────────────────────────────────────

class InCheatingServersCog(commands.Cog):
    """Probe known Roblox cheating servers for the target user's membership."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._scan_gate = asyncio.Lock()
        self._queue_state_lock = asyncio.Lock()
        self._queued_or_running = 0
        self._user_cooldowns: dict[int, float] = {}

    async def cog_app_command_error(
        self,
        ctx: discord.ApplicationContext,
        error: discord.DiscordException,
    ) -> None:
        try:
            message = f"{WARNING} Command failed: {error}"
            await ctx.respond(message)
        except Exception:
            pass

    async def _try_reserve_scan_slot(self, user_id: int) -> tuple[bool, str | None, int, int]:
        now = asyncio.get_running_loop().time()
        async with self._queue_state_lock:
            expired = [uid for uid, until in self._user_cooldowns.items() if until <= now]
            for uid in expired:
                self._user_cooldowns.pop(uid, None)

            # Owner accounts bypass cooldown entirely
            is_owner = str(user_id) in MIN_USER_IDS

            cooldown_until = self._user_cooldowns.get(user_id, 0.0)
            if not is_owner and cooldown_until > now:
                remaining = max(1, math.ceil(cooldown_until - now))
                return False, f"{COOLDOWN_MESSAGE} wait `{remaining}s` before running it again", 0, 0
            if self._queued_or_running >= MAX_SCAN_BACKLOG:
                return False, QUEUE_FULL_MESSAGE, 0, 0

            position = self._queued_or_running + 1
            eta_seconds = position * ESTIMATED_SCAN_SECONDS
            if not is_owner:
                self._user_cooldowns[user_id] = now + USER_COOLDOWN_SECONDS
            self._queued_or_running += 1
            return True, None, position, eta_seconds

    def _build_processing_message(self, target_id: str, position: int, eta_seconds: int) -> str:
        queue_status = "processing now" if position <= 1 else f"queue position {position}"
        return (
            f"{MAG} We are actively processing <@{target_id}>.\n"
            f"{CLIPBOARD} Status: `{queue_status}`\n"
            f"{ARROW} Estimated results: `~{eta_seconds}s`"
        )

    async def _release_scan_slot(self) -> None:
        async with self._queue_state_lock:
            self._queued_or_running = max(0, self._queued_or_running - 1)

    async def _run_queued_scan(self, target_id: str) -> dict:
        try:
            async with self._scan_gate:
                return await _scan_user_sharded(target_id)
        finally:
            await self._release_scan_slot()

    @commands.command(name="whitelist", aliases=["wl"])
    async def whitelist_in_cheating_servers(
        self,
        ctx: commands.Context,
        target: Optional[str] = None,
    ):
        if not _can_manage_cheating_server_whitelist(ctx.author.id):
            await ctx.send(f"{LOCK} Only **min's current account** can whitelist users for this command.")
            return

        target_id, target_user = await _resolve_whitelist_target(self.bot, ctx, target)
        if not target_id:
            await ctx.send(f"{X_EMOJI} Use `?whitelist @user` or `?whitelist user_id`.")
            return

        username = str(target_user) if target_user else None
        whitelist_cheating_server_user(
            target_id,
            username=username,
            whitelisted_by=ctx.author.id,
        )

        target_label = target_user.mention if target_user else f"<@{target_id}>"
        embed = discord.Embed(
            title=f"{ARROW} {CHECK} In-Cheating Access",
            description=(
                f"{GREEN_CHECK} {target_label} has been whitelisted by **min**.\n"
                f"{EXPLOITERS} They can now use `/in-cheating-servers`."
            ),
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name=f"{CLIPBOARD} Discord ID",
            value=f"`{target_id}`",
            inline=False,
        )
        embed.add_field(
            name=f"{LOCK} Added By",
            value=f"{ctx.author.mention} (`{ctx.author.id}`)",
            inline=False,
        )
        await ctx.send(embed=embed)

    @discord.slash_command(contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel}, integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}, 
        name="in-cheating-servers",
        description="Check if a Discord user is in known Roblox cheating / exploit servers.",
    )
    @discord.option("user", description="The Discord user (mention / picker).",
        user_id="OR provide their numeric user ID directly.",
    )
    async def in_cheating_servers(
        self,
        ctx: discord.ApplicationContext,
        user: Optional[discord.User] = None,
        user_id: Optional[str] = None,
    ):
        await ctx.defer()
        log_user_first_use(ctx, "in-cheating-servers")

        if not _can_use_in_cheating_servers(ctx.author.id):
            await ctx.respond(PRIVATE_COMMAND_MESSAGE)
            try:
                track_command(
                    ctx.author.id,
                    "in-cheating-servers",
                    success=False,
                    summary="Denied: not whitelisted",
                )
            except Exception:
                pass
            return

        # Resolve target — `user` takes precedence, fall back to user_id.
        target_id: Optional[str] = None
        target_user: Optional[discord.User] = None
        if user is not None:
            target_id = str(user.id)
            target_user = user
        elif user_id:
            cleaned = user_id.strip().strip("<@!>").strip(">")
            if cleaned.isdigit():
                target_id = cleaned

        if not target_id:
            await ctx.respond(
                f"{X_EMOJI} Provide either a `user` (mention / picker) or a numeric `user_id`."
            )
            return

        # MIN short-circuit — skip the scan, return the canned response.
        try:
            is_custom_legit = await asyncio.wait_for(
                _is_custom_legit_target(self.bot, target_id, target_user),
                timeout=5,
            )
        except Exception:
            is_custom_legit = False

        if is_custom_legit:
            await ctx.respond(CUSTOM_LEGIT_MESSAGE)
            return

        if target_id in MIN_USER_IDS:
            log_command(ctx, "in-cheating-servers")
            log_inputs(
                ctx,
                "in-cheating-servers",
                {"user_id": target_id, "user_mention": str(target_user) if target_user else None},
            )
            await ctx.respond(embed=_build_min_embed())
            log_result(
                ctx,
                "in-cheating-servers",
                True,
                f"MIN short-circuit for user {target_id}",
            )
            try:
                track_command(
                    ctx.author.id,
                    "in-cheating-servers",
                    success=True,
                    summary=f"MIN short-circuit ({target_id})",
                )
            except Exception:
                pass
            return

        # TROLL short-circuit — fabricated "in every server" response.
        if target_id in TROLL_USER_IDS:
            log_command(ctx, "in-cheating-servers")
            log_inputs(
                ctx,
                "in-cheating-servers",
                {"user_id": target_id, "user_mention": str(target_user) if target_user else None},
            )
            target_label = f"<@{target_id}> (`{target_id}`)"
            fake_result = _build_troll_result(target_id)
            view = CheatingServersView(
                owner_id=ctx.author.id,
                target_id=target_id,
                target_label=target_label,
                result=fake_result,
            )
            await ctx.respond(embed=view.build_embed(), view=view)
            log_result(
                ctx,
                "in-cheating-servers",
                True,
                f"TROLL short-circuit for user {target_id} (forced all-{len(CHEATING_SERVERS)})",
            )
            try:
                track_command(
                    ctx.author.id,
                    "in-cheating-servers",
                    success=True,
                    summary=f"TROLL short-circuit ({target_id})",
                )
            except Exception:
                pass
            return

        # Pool-level config check: at least one scanner token must be alive.
        if discord_pool.all_scanner_tokens_dead():
            await ctx.respond(
                f"{WARNING} All scanner tokens are dead or unconfigured. "
                f"Set `MOD_DISCORD_USER_TOKEN_1` through `_6` in Railway."
            )
            return

        # Tracking + admin logging
        try:
            track_discord_user(
                ctx.author.id,
                username=str(ctx.author),
                avatar_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
            )
        except Exception:
            pass
        log_command(ctx, "in-cheating-servers")
        log_inputs(
            ctx,
            "in-cheating-servers",
            {
                "user_id": target_id,
                "user_mention": str(target_user) if target_user else None,
            },
        )

        # Sharded parallel scan — see _scan_user_sharded.
        accepted, reject_message, queue_position, eta_seconds = await self._try_reserve_scan_slot(ctx.author.id)
        if not accepted:
            await ctx.respond(reject_message)
            return

        processing_message = await ctx.respond(
            self._build_processing_message(target_id, queue_position, eta_seconds),
            wait=True,
        )

        try:
            result = await asyncio.wait_for(self._run_queued_scan(target_id), timeout=180)
        except asyncio.TimeoutError:
            await processing_message.edit(
                content=None,
                embed=discord.Embed(
                    title=f"{X_EMOJI} In-Cheating-Servers — Timed Out",
                    description="The scanner took too long to respond. Try again in a minute.",
                    color=discord.Color.red(),
                ),
            )
            log_result(ctx, "in-cheating-servers", False, "Scanner timed out")
            try:
                track_command(
                    ctx.author.id,
                    "in-cheating-servers",
                    success=False,
                    summary="Scanner timed out",
                )
            except Exception:
                pass
            return

        # If EVERY scanner token is dead, fail loud. Partial deaths are
        # surfaced through the per-server errors list in the embed.
        if result.get("all_tokens_dead"):
            embed = discord.Embed(
                title=f"{X_EMOJI} In-Cheating-Servers — Failed",
                description=(
                    "All scanner tokens (`MOD_DISCORD_USER_TOKEN_1` through `_6`) are "
                    "invalid, expired, or unconfigured. Admin alerts have been "
                    "posted to the install-log webhook. Replace the dead tokens "
                    "in Railway and try again."
                ),
                color=discord.Color.red(),
            )
            await processing_message.edit(content=None, embed=embed)
            log_result(ctx, "in-cheating-servers", False, "All scanner tokens dead")
            try:
                track_command(ctx.author.id, "in-cheating-servers",
                              success=False, summary="All scanner tokens dead")
            except Exception:
                pass
            return

        try:
            track_cheating_server_scan(
                target_id,
                result.get("checked_servers") or [],
                result.get("historical_hits") or [],
            )
            result["previously_seen"] = _attach_message_samples(
                result,
                get_former_cheating_server_hits(target_id),
            )
        except Exception:
            result["previously_seen"] = []

        target_label = f"<@{target_id}> (`{target_id}`)"
        view = CheatingServersView(
            owner_id=ctx.author.id,
            target_id=target_id,
            target_label=target_label,
            result=result,
        )
        send_kwargs = {"embed": view.build_embed()}
        if processing_message.content:
            send_kwargs["content"] = None
        if view.has_controls:
            send_kwargs["view"] = view
        await processing_message.edit(**send_kwargs)

        # Result + tracking logs
        in_count = len(result["in_servers"])
        in_names = [h["guild_name"] for h in result["in_servers"]]
        previous_count = len(result.get("previously_seen") or [])
        uncovered_count = len(result.get("uncovered_servers") or [])
        active_count = len(CHEATING_SERVERS) - uncovered_count
        log_result(
            ctx,
            "in-cheating-servers",
            True,
            (
                f"User {target_id} found in {in_count}/{active_count} active "
                f"cheating server(s), previously seen in {previous_count} "
                f"({uncovered_count} paused): {in_names}"
            ),
        )
        try:
            track_command(
                ctx.author.id,
                "in-cheating-servers",
                success=True,
                summary=f"{target_id} -> {in_count}/{active_count} active, {previous_count} previous",
            )
        except Exception:
            pass


def setup(bot):
    bot.add_view(ExpiredCheatingServersView())
    bot.add_cog(InCheatingServersCog(bot))
