# ──────────────────────────────────────────────────────────────────
# ?accountstatus — owner-only prefix command that shows the health
# of every Discord user-account token the bot uses.
#
# Two modes:
#   ?accountstatus       — fast, uses cached state only (no API calls)
#   ?accountstatus live  — probes each token with GET /users/@me
#                          (the same endpoint real clients hit on
#                          every page load). 3-5s random jitter
#                          between probes to stay under the radar.
#
# Live mode has a 5-minute cooldown to prevent accidental spam.
# ──────────────────────────────────────────────────────────────────

import asyncio
import os
import random
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

from core.command_control import OWNER_ID, is_owner
from core import discord_pool

ACCOUNT_STATUS_ALLOWED_IDS = {
    OWNER_ID,
    930861591350624286,
    1338186029194154087,
}

# 5-minute cooldown on live checks (unix timestamp of last run).
_last_live_check: float = 0.0
LIVE_COOLDOWN_SECONDS = 1500

# Friendly labels for each token env var.
_TOKEN_LABELS: dict[str, str] = {
    "MOD_DISCORD_USER_TOKEN_1": "Scanner 1 (bobthelozer111)",
    "MOD_DISCORD_USER_TOKEN_2": "Scanner 2",
    "MOD_DISCORD_USER_TOKEN_3": "Scanner 3",
    "MOD_DISCORD_USER_TOKEN_4": "Scanner 4",
    "MOD_DISCORD_USER_TOKEN_5": "Scanner 5",
    "MOD_DISCORD_USER_TOKEN_6": "Scanner 6",
    "MOD_DISCORD_USER_TOKEN_BANCHECK": "Bancheck",
    "MOD_DISCORD_USER_TOKEN_MONITOR": "Channel Monitor (howareyou4you2day)",
}

# Server ranges for display.
_TOKEN_SERVERS: dict[str, str] = {
    "MOD_DISCORD_USER_TOKEN_1": "servers 1-8",
    "MOD_DISCORD_USER_TOKEN_2": "servers 9-14",
    "MOD_DISCORD_USER_TOKEN_3": "servers 17-20, 22-23",
    "MOD_DISCORD_USER_TOKEN_4": "servers 24-29",
    "MOD_DISCORD_USER_TOKEN_5": "servers 30-32, 35, 41-43",
    "MOD_DISCORD_USER_TOKEN_6": "servers 36-37, 39, 44-46",
    "MOD_DISCORD_USER_TOKEN_BANCHECK": "bancheck / reportercheck",
    "MOD_DISCORD_USER_TOKEN_MONITOR": "channel monitor (Gateway)",
}


def _status_emoji(alive) -> str:
    if alive is True:
        return "🟢"
    if alive is False:
        return "🔴"
    return "🟡"  # unknown / couldn't determine


class AccountStatusCog(commands.Cog):
    """Owner-only: ?accountstatus — show health of all Discord tokens."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="accountstatus", aliases=["tokenstatus", "ts"])
    async def account_status(self, ctx: commands.Context, mode: str = "cached"):
        """Show the status of all scanner / bancheck / monitor tokens."""
        if not (is_owner(ctx.author.id) or ctx.author.id in ACCOUNT_STATUS_ALLOWED_IDS):
            return  # silently ignore non-owners

        live = mode.lower() in ("live", "probe", "check")

        # ── Cooldown for live mode ───────────────────────────────
        global _last_live_check
        if live:
            elapsed = time.time() - _last_live_check
            if elapsed < LIVE_COOLDOWN_SECONDS:
                remaining = int(LIVE_COOLDOWN_SECONDS - elapsed)
                await ctx.send(
                    f"⏳ Live check on cooldown — wait **{remaining}s** before running again."
                )
                return
            _last_live_check = time.time()

        # ── Gather token env vars to check ───────────────────────
        all_envs = list(discord_pool.ALL_TOKEN_ENV_VARS)
        # Add monitor token if set.
        monitor_env = "MOD_DISCORD_USER_TOKEN_MONITOR"
        if os.environ.get(monitor_env, "").strip():
            all_envs.append(monitor_env)

        if live:
            msg = await ctx.send(
                f"🔍 Running live probe on **{len(all_envs)}** tokens "
                f"(~{len(all_envs) * 4}s with jitter)…"
            )

        # ── Collect results ──────────────────────────────────────
        results: list[tuple[str, dict]] = []

        for env_var in all_envs:
            if live:
                # Run the probe in a thread so we don't block the bot.
                result = await asyncio.to_thread(
                    discord_pool.safe_check_token, env_var,
                )
                results.append((env_var, result))

                # Jittered delay between probes — real users don't
                # fire requests at fixed intervals.
                await asyncio.sleep(random.uniform(3.0, 5.0))
            else:
                # Cached-only: use the in-memory state from discord_pool.
                token_val = os.environ.get(env_var, "").strip()
                if not token_val:
                    # For bancheck, also check the fallback env var.
                    if env_var == discord_pool.BANCHECK_TOKEN_ENV:
                        token_val = os.environ.get("MOD_DISCORD_USER_TOKEN", "").strip()

                if not token_val:
                    results.append((env_var, {"alive": False, "reason": "not set"}))
                else:
                    state = discord_pool._STATE.get(env_var)
                    if state and state.dead:
                        results.append((env_var, {
                            "alive": False,
                            "reason": state.dead_reason or "unknown",
                        }))
                    else:
                        results.append((env_var, {
                            "alive": True,
                            "reason": "cached ok (no errors since boot)",
                        }))

        # ── Build embed ──────────────────────────────────────────
        alive_count = sum(1 for _, r in results if r.get("alive") is True)
        dead_count = sum(1 for _, r in results if r.get("alive") is False)
        unknown_count = sum(1 for _, r in results if r.get("alive") is None)

        color = (
            discord.Color.green() if dead_count == 0
            else discord.Color.red() if alive_count == 0
            else discord.Color.gold()
        )

        embed = discord.Embed(
            title="🔐 Account Token Status",
            description=(
                f"**Mode:** `{'🔴 LIVE probe' if live else '📋 Cached state'}`\n"
                f"**Tokens:** {alive_count} 🟢 alive  •  {dead_count} 🔴 dead  •  {unknown_count} 🟡 unknown\n"
            ),
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        for env_var, result in results:
            label = _TOKEN_LABELS.get(env_var, env_var)
            servers = _TOKEN_SERVERS.get(env_var, "")
            emoji = _status_emoji(result.get("alive"))

            if result.get("alive") is True:
                username = result.get("username", "")
                user_id = result.get("id", "")
                detail = f"`{username}` (ID: `{user_id}`)" if username else "operational"
            else:
                detail = result.get("reason", "?")

            value = f"{emoji} {detail}"
            if servers:
                value += f"\n📡 {servers}"

            embed.add_field(
                name=f"**{label}**",
                value=value,
                inline=False,
            )

        embed.set_footer(text="Use '?accountstatus live' for a real-time probe (25min cooldown)")

        if live:
            await msg.edit(content=None, embed=embed)
        else:
            await ctx.send(embed=embed)


def setup(bot):
    bot.add_cog(AccountStatusCog(bot))
