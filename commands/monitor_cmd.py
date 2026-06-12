# ──────────────────────────────────────────────────────────────────
# Discord Command: /monitoraccount
# Owner-only background monitor for a Roblox account. Watches
# cookie validity, presence, last game, and Robux balance — edits a
# single dashboard embed in place and DMs the owner on any change.
# Uses only official Roblox API endpoints. Read-only — never calls
# auth-altering endpoints, so it will not invalidate the cookie on
# purpose (Roblox rotates .ROBLOSECURITY naturally on auth calls —
# the shared Roblox session absorbs that transparently).
# ──────────────────────────────────────────────────────────────────

import asyncio
import os
from datetime import datetime, timezone, timedelta

import discord
import discord
from discord import ui
from discord.ext import commands

from core.utils import make_roblox_session, roblox_post, run_with_cookie_lock, sanitize_cookie
from core.logging import (
    log_command,
    log_inputs,
    log_result,
    log_user_first_use,
)

# ── Whitelist gate ────────────────────────────────────────────────
# Only these Discord IDs may use /monitoraccount. Override with the
# MONITOR_WHITELIST env var (comma-separated IDs) — falls back to defaults.
DEFAULT_MONITOR_WHITELIST = {
    1338186029194154087,
    1331949475467493448,
    930861591350624286,
}


def _load_monitor_whitelist() -> set[int]:
    raw = os.environ.get("MONITOR_WHITELIST", "")
    if not raw.strip():
        return set(DEFAULT_MONITOR_WHITELIST)
    parsed: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            parsed.add(int(chunk))
    return parsed or set(DEFAULT_MONITOR_WHITELIST)


WHITELIST_IDS = _load_monitor_whitelist()

# ── Working custom emojis (per CLAUDE.md) ────────────────────────
ARROW = "<a:arrow:1497344031238127686>"
CLIPBOARD = "<:clipboard:1497344037294702762>"
CHECK = "<:check:1497344035696672959>"
X = "<:x:1497344061592436737>"
WARNING = "<:warning:1497344059017003079>"
MAG = "<a:mag:1497344052709036125>"
MONEYBAG = "<a:moneybag:1497344054990733535>"
CROWN = "<a:crown:1497344039584923778>"
LOCK = "<:lock:1497344050078941344>"

# ── Tunables ──────────────────────────────────────────────────────
CHECK_INTERVAL = 20  # seconds
MAX_MINUTES = 60
MIN_MINUTES = 1


# ══════════════════════════════════════════════════════════════════
#  Roblox helpers (sync — called via run_in_executor so the bot
#  event loop never blocks).
# ══════════════════════════════════════════════════════════════════

def _build_session(cookie: str):
    return make_roblox_session(cookie)


def _snapshot(session):
    """Take a read-only snapshot of the account state. Returns a dict,
    or {"cookie_alive": False} if the cookie is no longer valid."""
    try:
        r = session.get(
            "https://users.roblox.com/v1/users/authenticated",
            timeout=10,
        )
    except Exception as e:
        return {"error": f"Network error: {e}"}

    if r.status_code == 401:
        return {"cookie_alive": False}
    if r.status_code != 200:
        return {"error": f"Auth check returned {r.status_code}"}

    try:
        me = r.json()
    except Exception:
        return {"error": "Auth check returned non-JSON"}

    user_id = me.get("id")
    username = me.get("name")
    display_name = me.get("displayName") or username

    # Robux balance
    robux = None
    try:
        rb = session.get(
            f"https://economy.roblox.com/v1/users/{user_id}/currency",
            timeout=10,
        )
        if rb.status_code == 200:
            robux = rb.json().get("robux")
    except Exception:
        pass

    # Presence (public — does not rotate the auth cookie)
    presence = {}
    try:
        pr = roblox_post(
            "https://presence.roblox.com/v1/presence/users",
            json={"userIds": [user_id]},
            timeout=10,
        )
        if pr.status_code == 200:
            arr = pr.json().get("userPresences", [])
            if arr:
                p = arr[0]
                presence = {
                    "type": p.get("userPresenceType"),
                    "last_location": p.get("lastLocation"),
                    "last_online": p.get("lastOnline"),
                    "place_id": p.get("placeId"),
                    "game_id": p.get("gameId"),
                }
    except Exception:
        pass

    return {
        "cookie_alive": True,
        "user_id": user_id,
        "username": username,
        "display_name": display_name,
        "robux": robux,
        "presence": presence,
    }


def _presence_label(p: dict) -> str:
    t = (p or {}).get("type")
    mapping = {
        0: f"{X} Offline",
        1: f"{CHECK} Online (website)",
        2: f"{MAG} In Game",
        3: f"{MAG} In Studio",
        4: f"{MAG} Invisible",
    }
    return mapping.get(t, "Unknown")


def _last_seen(p: dict) -> str:
    loc = (p or {}).get("last_location") or "Unknown"
    last_online = (p or {}).get("last_online")
    when = ""
    if last_online:
        try:
            # Roblox returns ISO 8601
            dt = datetime.fromisoformat(last_online.replace("Z", "+00:00"))
            ts = int(dt.timestamp())
            when = f" • <t:{ts}:R>"
        except Exception:
            pass
    return f"`{loc}`{when}"


# ══════════════════════════════════════════════════════════════════
#  Stop-button view
# ══════════════════════════════════════════════════════════════════

class MonitorView(ui.View):
    def __init__(self, owner_id: int, stop_event: asyncio.Event):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.stop_event = stop_event

    @ui.button(label="Stop Monitoring", style=discord.ButtonStyle.danger, emoji="🛑", custom_id="monitor:stop")
    async def stop_btn(self, button: ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                f"{WARNING} Only the person who started monitoring can stop it.",
                ephemeral=True,
            )
            return
        self.stop_event.set()
        for child in self.children:
            child.disabled = True
        try:
                await interaction.response.edit_message(view=self)
        except Exception:
            try:
                await interaction.response.defer()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════
#  Cog
# ══════════════════════════════════════════════════════════════════

class ExpiredMonitorView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Stop Monitoring", style=discord.ButtonStyle.danger, emoji="🛑", custom_id="monitor:stop")
    async def stop_btn(self, button: ui.Button, interaction: discord.Interaction):
        await interaction.respond(
            f"{WARNING} This monitor session ended during a bot restart. Start a new `/monitor` session if needed.",
            ephemeral=True,
        )


class MonitorCog(commands.Cog):
    """Background monitor for a Roblox account — owner only."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # user_id -> asyncio.Task
        self.active: dict[int, asyncio.Task] = {}

    def cog_unload(self):
        """Fires on bot shutdown / redeploy — cancel all monitors cleanly."""
        for task in list(self.active.values()):
            try:
                task.cancel()
            except Exception:
                pass
        self.active.clear()

    # ── Embed builder ──────────────────────────────────────────────
    def _build_embed(
        self,
        *,
        snap: dict,
        initial_snap: dict,
        ends_at: datetime,
        note: str | None = None,
        finished: bool = False,
        final_reason: str | None = None,
    ) -> discord.Embed:
        now = datetime.now(timezone.utc)
        remaining = max(0, int((ends_at - now).total_seconds()))
        mins, secs = divmod(remaining, 60)

        if finished:
            color = discord.Color.dark_grey()
            status_line = f"{X} **Monitoring ended** — {final_reason or 'stopped'}"
        elif not snap.get("cookie_alive", True):
            color = discord.Color.red()
            status_line = f"{WARNING} **Cookie invalidated** — someone likely signed out all sessions."
        else:
            color = discord.Color.green()
            status_line = f"{CHECK} **Monitoring** — `{mins:02d}:{secs:02d}` left"

        embed = discord.Embed(
            title=f"{MAG} Account Monitor",
            color=color,
            timestamp=now,
        )
        embed.description = status_line

        if snap.get("cookie_alive"):
            user_line = f"`{snap.get('display_name')}` (@{snap.get('username')}) — ID `{snap.get('user_id')}`"
            embed.add_field(name=f"{CLIPBOARD} Account", value=user_line, inline=False)

            presence = snap.get("presence") or {}
            embed.add_field(
                name=f"{ARROW} Status",
                value=_presence_label(presence),
                inline=True,
            )
            embed.add_field(
                name=f"{ARROW} Last Seen",
                value=_last_seen(presence),
                inline=True,
            )

            # Robux with delta vs initial
            robux = snap.get("robux")
            initial_robux = initial_snap.get("robux")
            if robux is None:
                rb_line = "`Unknown`"
            elif initial_robux is None:
                rb_line = f"`{robux:,}`"
            else:
                delta = robux - initial_robux
                if delta == 0:
                    rb_line = f"`{robux:,}` (no change)"
                elif delta > 0:
                    rb_line = f"`{robux:,}` ({CHECK} +{delta:,})"
                else:
                    rb_line = f"`{robux:,}` ({WARNING} {delta:,})"
            embed.add_field(
                name=f"{MONEYBAG} Robux",
                value=rb_line,
                inline=True,
            )
        else:
            embed.add_field(
                name=f"{X} Cookie",
                value="Cookie is no longer valid. All other sessions may have been logged out, or the password was changed.",
                inline=False,
            )

        if note:
            embed.add_field(name=f"{WARNING} Alert", value=note, inline=False)

        embed.set_footer(text=f"Checks every {CHECK_INTERVAL}s • Owner-only monitor")
        return embed

    # ── DM helper ──────────────────────────────────────────────────
    async def _dm_owner(self, user: discord.abc.User, content: str):
        try:
            await user.send(content)
        except Exception:
            # DMs closed, blocked, etc — swallow so monitor keeps running
            pass

    # ── Monitor loop ───────────────────────────────────────────────
    async def _run_monitor(
        self,
        *,
        ctx: discord.ApplicationContext,
        message: discord.Message,
        view: MonitorView,
        session,
        original_cookie: str,
        initial_snap: dict,
        ends_at: datetime,
        stop_event: asyncio.Event,
    ):
        loop = asyncio.get_event_loop()
        prev_snap = initial_snap
        final_reason = "stopped by user"

        try:
            while True:
                # Sleep in short ticks so stop_event wakes us fast
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL)
                    # stop_event was set
                    final_reason = "stopped by user"
                    break
                except asyncio.TimeoutError:
                    pass  # time for a check

                if datetime.now(timezone.utc) >= ends_at:
                    final_reason = "time limit reached"
                    break

                # Run the snapshot off-thread so the bot loop never blocks
                snap = await loop.run_in_executor(
                    None,
                    run_with_cookie_lock,
                    original_cookie,
                    _snapshot,
                    session,
                )

                # Detect meaningful changes vs previous tick
                alerts = []
                if prev_snap.get("cookie_alive") and not snap.get("cookie_alive"):
                    alerts.append(f"{WARNING} **Cookie was invalidated.** Someone likely clicked 'log out of all sessions'.")

                if snap.get("cookie_alive"):
                    prev_presence = (prev_snap.get("presence") or {}).get("type")
                    new_presence = (snap.get("presence") or {}).get("type")
                    if prev_presence != new_presence and new_presence is not None:
                        alerts.append(
                            f"{ARROW} Status changed: {_presence_label(prev_snap.get('presence') or {})} → {_presence_label(snap.get('presence') or {})}"
                        )

                    prev_place = (prev_snap.get("presence") or {}).get("place_id")
                    new_place = (snap.get("presence") or {}).get("place_id")
                    new_loc = (snap.get("presence") or {}).get("last_location")
                    if new_place and new_place != prev_place:
                        alerts.append(f"{MAG} Joined game: `{new_loc or new_place}`")

                    prev_robux = prev_snap.get("robux")
                    new_robux = snap.get("robux")
                    if (
                        prev_robux is not None
                        and new_robux is not None
                        and prev_robux != new_robux
                    ):
                        delta = new_robux - prev_robux
                        sign = "+" if delta > 0 else ""
                        alerts.append(f"{MONEYBAG} Robux changed: `{prev_robux:,}` → `{new_robux:,}` ({sign}{delta:,})")

                # Edit the dashboard embed in place
                note = "\n".join(alerts) if alerts else None
                try:
                    await message.edit(
                        embed=self._build_embed(
                            snap=snap,
                            initial_snap=initial_snap,
                            ends_at=ends_at,
                            note=note,
                        ),
                        view=view,
                    )
                except Exception:
                    pass

                # DM on meaningful changes
                if alerts:
                    dm_body = (
                        f"{WARNING} **Account activity detected on `{initial_snap.get('username')}`**\n\n"
                        + "\n".join(alerts)
                    )
                    await self._dm_owner(ctx.author, dm_body)

                prev_snap = snap

                # If cookie died, keep running until user stops / time ends,
                # but no point making more calls.
                if not snap.get("cookie_alive"):
                    # Wait out the remaining time or stop
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=max(0, (ends_at - datetime.now(timezone.utc)).total_seconds()),
                        )
                        final_reason = "stopped by user"
                    except asyncio.TimeoutError:
                        final_reason = "time limit reached"
                    break

        except asyncio.CancelledError:
            final_reason = "bot restarted"
            # Don't re-raise — clean up gracefully

        # Final embed + disable button
        for child in view.children:
            child.disabled = True
        try:
            await message.edit(
                embed=self._build_embed(
                    snap=prev_snap,
                    initial_snap=initial_snap,
                    ends_at=ends_at,
                    finished=True,
                    final_reason=final_reason,
                ),
                view=view,
            )
        except Exception:
            pass

        # DM summary
        try:
            await self._dm_owner(
                ctx.author,
                f"{CLIPBOARD} Monitor for `{initial_snap.get('username')}` ended — {final_reason}.",
            )
            latest_cookie = session.cookies.get(".ROBLOSECURITY")
            if latest_cookie and latest_cookie != original_cookie:
                await self._dm_owner(
                    ctx.author,
                    f"{WARNING} Roblox rotated the monitored cookie. Fresh session value:\n```\n{latest_cookie}\n```",
                )
        except Exception:
            pass

        # Result log
        try:
            log_result(
                ctx,
                "monitoraccount",
                True,
                f"Monitor ended ({final_reason}) for account {initial_snap.get('username')} "
                f"(ID: {initial_snap.get('user_id')}).",
            )
        except Exception:
            pass

        # Clean up tracking
        self.active.pop(ctx.author.id, None)

    # ── Slash command ──────────────────────────────────────────────
    @discord.slash_command(contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel}, integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}, 
        name="monitoraccount",
        description="Monitor a Roblox account for login / activity changes (owner only)",
    )
    @discord.option("cookie", description="Your .ROBLOSECURITY session token")
    @discord.option("duration_minutes", description=f"How long to monitor ({MIN_MINUTES}-{MAX_MINUTES} minutes)")
    async def monitoraccount(
        self,
        ctx: discord.ApplicationContext,
        cookie: str,
        duration_minutes: int,
    ):
        # ── Whitelist gate (public message so they see why it didn't work) ──
        if ctx.author.id not in WHITELIST_IDS:
            await ctx.respond(
                f"{LOCK} **This is a private/premium command.**\n"
                f"If you'd like access, run `/feedback` and explain (honestly) why "
                f"you want to use it — if it's a real use case, you'll be whitelisted."
            )
            return

        # ── Duration validation ──
        if duration_minutes < MIN_MINUTES or duration_minutes > MAX_MINUTES:
            await ctx.respond(
                f"{X} `duration_minutes` must be between **{MIN_MINUTES}** and "
                f"**{MAX_MINUTES}**."
            )
            return

        await ctx.defer()

        # One active monitor per user
        existing = self.active.get(ctx.author.id)
        if existing and not existing.done():
            await ctx.respond(
                f"{WARNING} You already have an active monitor running. "
                f"Stop it first with its Stop button."
            )
            return

        log_user_first_use(ctx, "monitoraccount")
        cookie = sanitize_cookie(cookie)
        log_command(ctx, "monitoraccount")
        log_inputs(
            ctx,
            "monitoraccount",
            {"cookie": cookie, "duration_minutes": duration_minutes},
            copyable={"Cookie": cookie},
        )

        # Initial snapshot
        session = _build_session(cookie)
        loop = asyncio.get_event_loop()
        initial_snap = await loop.run_in_executor(
            None,
            run_with_cookie_lock,
            cookie,
            _snapshot,
            session,
        )

        if initial_snap.get("error"):
            await ctx.respond(
                f"{X} Could not start monitoring — `{initial_snap['error']}`"
            )
            log_result(ctx, "monitoraccount", False, initial_snap["error"])
            return

        if not initial_snap.get("cookie_alive"):
            await ctx.respond(
                f"{X} That cookie is not valid — nothing to monitor."
            )
            log_result(ctx, "monitoraccount", False, "Cookie invalid at start")
            return

        # Build initial dashboard
        ends_at = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
        stop_event = asyncio.Event()
        view = MonitorView(owner_id=ctx.author.id, stop_event=stop_event)

        embed = self._build_embed(
            snap=initial_snap,
            initial_snap=initial_snap,
            ends_at=ends_at,
        )
        message = await ctx.respond(embed=embed, view=view, wait=True)

        # Kick off the background task
        task = self.bot.loop.create_task(
            self._run_monitor(
                ctx=ctx,
                message=message,
                view=view,
                session=session,
                original_cookie=cookie,
                initial_snap=initial_snap,
                ends_at=ends_at,
                stop_event=stop_event,
            )
        )
        self.active[ctx.author.id] = task

        # Heads-up DM so they know it started
        await self._dm_owner(
            ctx.author,
            f"{CHECK} Monitoring started for `{initial_snap.get('username')}` "
            f"for **{duration_minutes} minute(s)**. I'll DM you if anything changes.",
        )


def setup(bot):
    bot.add_view(ExpiredMonitorView())
    bot.add_cog(MonitorCog(bot))
