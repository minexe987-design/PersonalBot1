# ──────────────────────────────────────────────────────────────────
# Channel Monitor — Discord Gateway WebSocket listener.
#
# Connects to the Discord Gateway as a user account and watches
# specific channels for new messages. When a message lands in a
# monitored channel, it immediately:
#   1) Downloads attachments (images, videos, clips) into memory
#      (NOT to disk — held in RAM, then immediately re-uploaded)
#   2) Forwards the full message (content + re-uploaded files) to a webhook
#
# This captures hacker-proof / hacker-report messages that get
# deleted by moderators within 30s–1min, preserving the evidence
# for bancheck improvement.
#
# Runs as a background asyncio.Task started from bot.py.
# ──────────────────────────────────────────────────────────────────

import asyncio
import json
import os
import random
import traceback
from datetime import datetime, timezone
from typing import Optional
from time import time

import aiohttp


# ── ANSI colors for console output ───────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"

# ── Configuration ────────────────────────────────────────────────
TOKEN_ENV = "MOD_DISCORD_USER_TOKEN_MONITOR"

# Channels to monitor: (guild_id, channel_id, label, webhook_url).
# Each channel can route to a DIFFERENT webhook.
# To add more channels, append entries here.
MONITORED_CHANNELS: list[tuple[str, str, str, str]] = [
    (
        "1138735137685786694",                         # Ranked | BedWars
        "1323859693189333083",                         # #hacker-proof
        "Ranked | BedWars — #hacker-proof",
        "https://discord.com/api/webhooks/"
        "1501331968611319898/"
        "d3kmVf81scrxgoDyr3HBR2bdh9iBg3WXfHCN0i5V-sCjKf3luzFRmobeGhtIkqzQK17k",
    ),
    (
        "1260440749204570294",                         # pelican reports cheaters
        "1462991670231564468",                         # #overflow-reports
        "Pelican — #overflow-reports",
        "https://discord.com/api/webhooks/"
        "1501335613691924710/"
        "AfY8-SkpNpN3EUoHaaJOOaRMh-XwHS7h4pivUOjdRFQTZwLIHQXSqnOKlZ90JCLfLXKT",
    ),
]

# Quick lookup structures built from MONITORED_CHANNELS.
_MONITORED_CHANNEL_IDS: set[str] = {c for _, c, _, _ in MONITORED_CHANNELS}
_CHANNEL_LABELS: dict[str, str] = {c: label for _, c, label, _ in MONITORED_CHANNELS}
_CHANNEL_WEBHOOKS: dict[str, str] = {c: wh for _, c, _, wh in MONITORED_CHANNELS}
_ALL_WEBHOOK_URLS: list[str] = list(dict.fromkeys(wh for _, _, _, wh in MONITORED_CHANNELS))
# Recent forwarded message dedupe: map message_id -> last forward epoch.
_RECENT_FORWARD_WINDOW = 300.0  # seconds
_RECENT_FORWARDED: dict[str, float] = {}

# Gateway
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Don't try to forward attachments beyond Discord's webhook request limit.
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024
MAX_WEBHOOK_UPLOAD_SIZE = 25 * 1024 * 1024


def _format_mb(size_bytes: int | float | None) -> str:
    try:
        size = float(size_bytes or 0)
    except Exception:
        size = 0.0
    return f"{size / 1_000_000:.2f} MB"


def _split_lines(lines: list[str], *, max_chars: int = 1800) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        addition = len(line) + (1 if current else 0)
        if current and current_len + addition > max_chars:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += addition
    if current:
        chunks.append("\n".join(current))
    return chunks


def _attachment_link_line(att_name: str, size_text: str, att_url: str) -> str:
    if att_url:
        return f"- [{att_name}]({att_url}) ({size_text})"
    return f"- `{att_name}` ({size_text}) - no CDN URL available"

# ── Gateway opcodes ──────────────────────────────────────────────
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


# ── Helpers ──────────────────────────────────────────────────────

def _log(level: str, msg: str):
    color = {"INFO": CYAN, "OK": GREEN, "WARN": YELLOW, "ERROR": RED}.get(level, "")
    print(f"  {color}[MONITOR/{level}]{RESET} {msg}")


async def _post_webhook(
    session: aiohttp.ClientSession,
    webhook_url: str,
    payload: dict,
    files: list[tuple[str, bytes, str]] | None = None,
):
    """Post to a webhook, optionally with file attachments."""
    try:
        if files:
            form = aiohttp.FormData()
            form.add_field(
                "payload_json",
                json.dumps(payload),
                content_type="application/json",
            )
            for i, (fname, fdata, fmime) in enumerate(files):
                form.add_field(
                    f"files[{i}]", fdata,
                    filename=fname, content_type=fmime,
                )
            async with session.post(webhook_url, data=form) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    _log("WARN", f"Webhook returned {resp.status}: {body[:200]}")
        else:
            async with session.post(webhook_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    _log("WARN", f"Webhook returned {resp.status}: {body[:200]}")
    except Exception as e:
        _log("ERROR", f"Failed to post webhook: {e}")


async def _send_status(
    session: aiohttp.ClientSession,
    title: str,
    description: str,
    color: int,
):
    """Send a status embed to ALL unique webhooks."""
    payload = {
        "username": "Hacker Report Monitor",
        "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Channel Monitor • Status"},
        }],
    }
    for wh_url in _ALL_WEBHOOK_URLS:
        await _post_webhook(session, wh_url, payload)


# ── Message forwarding ───────────────────────────────────────────

async def _forward_message(
    session: aiohttp.ClientSession,
    msg_data: dict,
    channel_label: str,
    webhook_url: str,
):
    """Download attachments and forward a captured message to the webhook."""
    author = msg_data.get("author", {})
    content = msg_data.get("content", "")
    attachments = msg_data.get("attachments", [])
    embeds_in = msg_data.get("embeds", [])
    timestamp = msg_data.get("timestamp", "")
    message_id = str(msg_data.get("id", "?"))

    # Deduplicate recent forwards to avoid double-posts when Discord replays
    # MESSAGE_CREATE around Gateway reconnect/resume.
    now = time()
    last = _RECENT_FORWARDED.get(message_id)
    if last and now - last < _RECENT_FORWARD_WINDOW:
        _log("INFO", f"Skipping duplicate forward for message {message_id}")
        return
    _RECENT_FORWARDED[message_id] = now
    # prune old entries
    for mid, ts in list(_RECENT_FORWARDED.items()):
        if now - ts > _RECENT_FORWARD_WINDOW * 3:
            _RECENT_FORWARDED.pop(mid, None)

    # ── Referenced message (replies) ─────────────────────────────
    ref = msg_data.get("referenced_message")
    ref_line = ""
    if ref:
        ref_author = ref.get("author", {}).get("username", "?")
        ref_content = (ref.get("content") or "(no text)")[:200]
        ref_line = f"\n**↩️ Replying to** `{ref_author}`: {ref_content}"

    # ── Author info ──────────────────────────────────────────────
    author_name = author.get("username", "Unknown")
    author_global = author.get("global_name") or author_name
    author_id = author.get("id", "?")
    avatar_hash = author.get("avatar", "")
    if avatar_hash:
        avatar_url = f"https://cdn.discordapp.com/avatars/{author_id}/{avatar_hash}.png"
    else:
        idx = (int(author_id) >> 22) % 6 if author_id.isdigit() else 0
        avatar_url = f"https://cdn.discordapp.com/embed/avatars/{idx}.png"

    # ── Build embed ──────────────────────────────────────────────
    embed: dict = {
        "title": f"📨 Message Captured — {channel_label}",
        "color": 0xFF6B6B,
        "fields": [
            {
                "name": "👤 Author",
                "value": f"`{author_global}` (`{author_name}`)\nID: `{author_id}`",
                "inline": True,
            },
            {
                "name": "💬 Channel",
                "value": f"`{channel_label}`",
                "inline": True,
            },
            {
                "name": "🆔 Message ID",
                "value": f"`{message_id}`",
                "inline": True,
            },
        ],
        "timestamp": timestamp,
        "thumbnail": {"url": avatar_url},
        "footer": {"text": "Channel Monitor • Hacker Report Capture"},
    }

    # Content field
    body = (content or "(no text content)") + ref_line
    if len(body) > 1024:
        embed["description"] = body[:2000]
    else:
        embed["fields"].append({
            "name": "📝 Content",
            "value": body,
            "inline": False,
        })

    # Note embedded embeds from original message
    if embeds_in:
        embed["fields"].append({
            "name": "📎 Embeds",
            "value": f"{len(embeds_in)} embed(s) in original message",
            "inline": True,
        })

    # ── Download attachments ─────────────────────────────────────
    upload_batches: list[list[tuple[str, bytes, str]]] = []
    current_batch: list[tuple[str, bytes, str]] = []
    att_lines: list[str] = []
    forwarded_link_lines: list[str] = []
    upload_total = 0

    for att in attachments:
        att_url = att.get("url", "")
        att_name = att.get("filename", "unknown")
        att_size = att.get("size", 0)
        att_mime = att.get("content_type", "application/octet-stream")
        size_text = _format_mb(att_size)

        if att_size > MAX_ATTACHMENT_SIZE:
            att_lines.append(
                f"`{att_name}` ({size_text}) - forwarded as link, over {_format_mb(MAX_ATTACHMENT_SIZE)} upload limit"
            )
            forwarded_link_lines.append(_attachment_link_line(att_name, size_text, att_url))
            continue

        if upload_total + att_size > MAX_WEBHOOK_UPLOAD_SIZE:
            if current_batch:
                upload_batches.append(current_batch)
            current_batch = []
            upload_total = 0

        att_lines.append(f"`{att_name}` ({size_text}) - forwarded")

        if att_url and 0 < att_size:
            try:
                async with session.get(att_url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if current_batch and upload_total + len(data) > MAX_WEBHOOK_UPLOAD_SIZE:
                            upload_batches.append(current_batch)
                            current_batch = []
                            upload_total = 0
                        current_batch.append((att_name, data, att_mime))
                        upload_total += len(data)
                    else:
                        forwarded_link_lines.append(_attachment_link_line(att_name, size_text, att_url))
                        att_lines[-1] = f"`{att_name}` ({size_text}) - forwarded as link, download HTTP {resp.status}"
            except Exception as e:
                _log("WARN", f"Failed to download {att_name}: {e}")
                forwarded_link_lines.append(_attachment_link_line(att_name, size_text, att_url))
                att_lines[-1] = f"`{att_name}` ({size_text}) - forwarded as link, download failed"
        elif att_url:
            forwarded_link_lines.append(_attachment_link_line(att_name, size_text, att_url))
            att_lines[-1] = f"`{att_name}` ({size_text}) - forwarded as link"

    if current_batch:
        upload_batches.append(current_batch)

    if att_lines:
        attachment_summary = "\n".join(att_lines[:10])
        if len(att_lines) > 10:
            attachment_summary += f"\n... +{len(att_lines) - 10} more attachment(s)"
        if len(attachment_summary) > 1000:
            attachment_summary = attachment_summary[:970] + "\n... truncated"
        embed["fields"].append({
            "name": f"📎 Attachments ({len(attachments)})",
            "value": attachment_summary,
            "inline": False,
        })

    # If there's a single image attachment, show it inline in the first
    # webhook payload that carries the uploaded file.
    if (
        len(attachments) == 1
        and attachments[0].get("content_type", "").startswith("image/")
        and upload_batches
        and upload_batches[0]
    ):
        embed["image"] = {"url": f"attachment://{upload_batches[0][0][0]}"}

    # ── Send ─────────────────────────────────────────────────────
    payload = {
        "username": "Hacker Report Monitor",
        "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
        "embeds": [embed],
    }
    link_chunks = _split_lines(forwarded_link_lines)
    if link_chunks:
        payload["content"] = "Oversized or failed-download attachment links:\n" + link_chunks[0]

    first_batch = upload_batches[0] if upload_batches else None
    await _post_webhook(session, webhook_url, payload, files=first_batch)

    for chunk in link_chunks[1:]:
        await _post_webhook(
            session,
            webhook_url,
            {
                "username": "Hacker Report Monitor",
                "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
                "content": "More oversized/failed-download attachment links:\n" + chunk,
            },
        )

    for batch_number, batch in enumerate(upload_batches[1:], start=2):
        await _post_webhook(
            session,
            webhook_url,
            {
                "username": "Hacker Report Monitor",
                "avatar_url": "https://cdn.discordapp.com/embed/avatars/0.png",
                "content": f"Continued attachment upload for captured message `{message_id}` (batch {batch_number}/{len(upload_batches)}).",
            },
            files=batch,
        )


# ── Gateway loop ─────────────────────────────────────────────────

async def _run_gateway(token: str):
    """Connect to the Discord Gateway, maintain heartbeat, and dispatch events."""
    session_id: Optional[str] = None
    resume_url: Optional[str] = None
    sequence: Optional[int] = None
    backoff = 1.0

    async with aiohttp.ClientSession() as session:
        await _send_status(
            session,
            "🟡 Channel Monitor Starting",
            "Connecting to Discord Gateway…\n"
            + "\n".join(f"• {l}" for _, _, l, _ in MONITORED_CHANNELS),
            0xFEE75C,
        )

        while True:
            try:
                ws_url = resume_url or GATEWAY_URL
                _log("INFO", f"Connecting to Gateway…")

                async with session.ws_connect(
                    ws_url,
                    headers={"User-Agent": _USER_AGENT},
                    heartbeat=None,  # we handle heartbeat ourselves
                ) as ws:

                    # ── Step 1: HELLO ────────────────────────────
                    hello = await ws.receive_json()
                    if hello.get("op") != OP_HELLO:
                        _log("ERROR", f"Expected HELLO (op 10), got op {hello.get('op')}")
                        continue

                    hb_interval = hello["d"]["heartbeat_interval"] / 1000.0
                    _log("OK", f"HELLO — heartbeat every {hb_interval:.1f}s")

                    # ── Step 2: IDENTIFY or RESUME ───────────────
                    if session_id and sequence is not None:
                        _log("INFO", "Sending RESUME…")
                        await ws.send_json({
                            "op": OP_RESUME,
                            "d": {
                                "token": token,
                                "session_id": session_id,
                                "seq": sequence,
                            },
                        })
                    else:
                        _log("INFO", "Sending IDENTIFY…")
                        await ws.send_json({
                            "op": OP_IDENTIFY,
                            "d": {
                                "token": token,
                                "capabilities": 16381,
                                "properties": {
                                    "os": "Windows",
                                    "browser": "Chrome",
                                    "device": "",
                                    "system_locale": "en-US",
                                    "browser_user_agent": _USER_AGENT,
                                    "browser_version": "131.0.0.0",
                                    "os_version": "10",
                                    "referrer": "",
                                    "referring_domain": "",
                                    "referrer_current": "",
                                    "referring_domain_current": "",
                                    "release_channel": "stable",
                                    "client_build_number": 354000,
                                    "client_event_source": None,
                                },
                                "presence": {
                                    "status": "invisible",
                                    "since": 0,
                                    "activities": [],
                                    "afk": False,
                                },
                                "compress": False,
                                "client_state": {
                                    "guild_versions": {},
                                },
                            },
                        })

                    # ── Step 3: heartbeat task ───────────────────
                    hb_acked = asyncio.Event()
                    hb_acked.set()

                    async def _heartbeat():
                        await asyncio.sleep(hb_interval * random.random())
                        while True:
                            if not hb_acked.is_set():
                                _log("WARN", "Heartbeat not ACKed — closing")
                                await ws.close()
                                return
                            hb_acked.clear()
                            await ws.send_json({"op": OP_HEARTBEAT, "d": sequence})
                            await asyncio.sleep(hb_interval)

                    hb_task = asyncio.create_task(_heartbeat())

                    # ── Step 4: event loop ───────────────────────
                    try:
                        async for raw in ws:
                            if raw.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(raw.data)
                                op = data.get("op")

                                if data.get("s") is not None:
                                    sequence = data["s"]

                                # — DISPATCH (op 0) ───────────────
                                if op == OP_DISPATCH:
                                    t = data.get("t", "")
                                    d = data.get("d", {})

                                    if t == "READY":
                                        session_id = d.get("session_id")
                                        rurl = d.get("resume_gateway_url", "")
                                        if rurl:
                                            resume_url = f"{rurl}?v=10&encoding=json"
                                        user_obj = d.get("user", {})
                                        _log("OK",
                                             f"READY as {user_obj.get('username', '?')} "
                                             f"— session {session_id}")
                                        backoff = 1.0

                                        await _send_status(
                                            session,
                                            "🟢 Channel Monitor Connected",
                                            f"Logged in as `{user_obj.get('username', '?')}`.\n"
                                            f"Monitoring {len(MONITORED_CHANNELS)} channel(s):\n"
                                            + "\n".join(f"• {l}" for _, _, l, _ in MONITORED_CHANNELS),
                                            0x57F287,
                                        )

                                    elif t == "RESUMED":
                                        _log("OK", "RESUMED successfully")
                                        backoff = 1.0

                                    elif t == "MESSAGE_CREATE":
                                        channel_id = d.get("channel_id", "")
                                        if channel_id in _MONITORED_CHANNEL_IDS:
                                            label = _CHANNEL_LABELS.get(channel_id, "Unknown")
                                            wh_url = _CHANNEL_WEBHOOKS[channel_id]
                                            author_name = d.get("author", {}).get("username", "?")
                                            _log("INFO",
                                                 f"📨 Captured message from {author_name} "
                                                 f"in {label}")
                                            # Fire-and-forget so we don't block the event loop
                                            asyncio.create_task(
                                                _forward_message(session, d, label, wh_url)
                                            )

                                # — HEARTBEAT ACK (op 11) ─────────
                                elif op == OP_HEARTBEAT_ACK:
                                    hb_acked.set()

                                # — Server requests heartbeat ─────
                                elif op == OP_HEARTBEAT:
                                    await ws.send_json({"op": OP_HEARTBEAT, "d": sequence})

                                # — RECONNECT (op 7) ──────────────
                                elif op == OP_RECONNECT:
                                    _log("WARN", "Gateway sent RECONNECT")
                                    await ws.close()
                                    break

                                # — INVALID SESSION (op 9) ────────
                                elif op == OP_INVALID_SESSION:
                                    resumable = data.get("d", False)
                                    _log("WARN", f"INVALID_SESSION (resumable={resumable})")
                                    if not resumable:
                                        session_id = None
                                        sequence = None
                                        resume_url = None
                                    await asyncio.sleep(random.uniform(1, 5))
                                    break

                            elif raw.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                _log("WARN", f"WebSocket {raw.type}")
                                break

                    finally:
                        hb_task.cancel()
                        try:
                            await hb_task
                        except asyncio.CancelledError:
                            pass

            except asyncio.CancelledError:
                _log("INFO", "Monitor task cancelled — shutting down")
                try:
                    await _send_status(
                        session,
                        "🔴 Channel Monitor Stopped",
                        "Bot is shutting down.",
                        0xED4245,
                    )
                except Exception:
                    pass
                return

            except Exception as e:
                _log("ERROR", f"Gateway error: {e}")
                traceback.print_exc()

            _log("INFO", f"Reconnecting in {backoff:.1f}s…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ── Public entry point ───────────────────────────────────────────

async def start_monitor():
    """
    Validate config and start the Gateway listener.
    Called from bot.py as a background task. If the env var is missing
    the monitor silently disables itself so the bot still starts.
    """
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        _log("WARN", f"{TOKEN_ENV} not set — channel monitor disabled")
        return

    _log("INFO", "Starting channel monitor…")

    try:
        await _run_gateway(token)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        _log("ERROR", f"Monitor crashed: {e}")
        traceback.print_exc()
