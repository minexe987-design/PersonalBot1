from typing import Optional

import discord
import discord
from discord.ext import commands
from discord.ext import commands

from core.command_control import (
    is_owner,
    list_disabled_commands,
    set_command_disabled,
)
from core.tracking import get_discord_profile, track_command


def _clean_user_id(value: str) -> Optional[int]:
    cleaned = (value or "").strip().strip("<@!>").strip(">")
    if not cleaned.isdigit():
        return None
    return int(cleaned)


class OwnerToolsCog(commands.Cog):
    """Owner-only bot controls."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _owner_only(self, ctx: discord.ApplicationContext) -> bool:
        if is_owner(ctx.author.id):
            return True
        await ctx.respond("This command is restricted.")
        return False

    @discord.slash_command(contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel}, integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}, 
        name="owner-commanddisabler",
        description="Owner-only: disable, enable, or list bot slash commands.",
    )
    @discord.option(
        "action",
        description="Action to perform",
        choices=[
            discord.OptionChoice(name="disable", value="disable"),
            discord.OptionChoice(name="enable", value="enable"),
            discord.OptionChoice(name="list", value="list"),
        ]
    )
    @discord.option("command", description="Command name, like accountchecker or /in-cheating-servers.", required=False)
    async def owner_commanddisabler(
        self,
        ctx: discord.ApplicationContext,
        action: str,
        command: Optional[str] = None,
    ):
        if not await self._owner_only(ctx):
            return

        action_value = action
        if action_value == "list":
            disabled = list_disabled_commands()
            body = "\n".join(f"`/{name}`" for name in disabled) if disabled else "No commands are disabled."
            await ctx.respond(body)
            return

        if not command:
            await ctx.respond("Give me a command name to enable or disable.")
            return

        ok, result = set_command_disabled(
            command,
            action_value == "disable",
            changed_by=ctx.author.id,
        )
        if not ok:
            await ctx.respond(result)
            return

        verb = "disabled" if action_value == "disable" else "enabled"
        await ctx.respond(f"`/{result}` is now {verb}.")
        try:
            track_command(
                ctx.author.id,
                "owner-commanddisabler",
                success=True,
                summary=f"{verb} /{result}",
            )
        except Exception:
            pass

    @discord.slash_command(contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel}, integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}, 
        name="owner-dmuser",
        description="Owner-only: DM a tracked bot user.",
    )
    @discord.option("message", description="Message to send through the bot.")
    @discord.option("user", description="Tracked Discord user.", required=False)
    @discord.option("user_id", description="OR tracked Discord user ID.", required=False)
    async def owner_dmuser(
        self,
        ctx: discord.ApplicationContext,
        message: str,
        user: Optional[discord.User] = None,
        user_id: Optional[str] = None,
    ):
        if not await self._owner_only(ctx):
            return

        target_id = user.id if user is not None else (_clean_user_id(user_id or "") if user_id else None)
        if target_id is None:
            await ctx.respond("Provide either a user picker target or a numeric user_id.")
            return

        profile = get_discord_profile(target_id)
        if not profile:
            await ctx.respond("That user is not verified/tracked with the bot yet.")
            return

        if len(message) > 1900:
            await ctx.respond("Message is too long. Keep it under 1900 characters.")
            return

        await ctx.defer()
        try:
            target = user or await self.bot.fetch_user(target_id)
            await target.send(message)
        except discord.Forbidden:
            await ctx.respond("Couldn't DM them. Their DMs are closed or they blocked the bot.")
            return
        except Exception as e:
            await ctx.respond(f"Couldn't DM them: {e}")
            return

        await ctx.respond(f"Sent DM to <@{target_id}>.")
        try:
            track_command(
                ctx.author.id,
                "owner-dmuser",
                success=True,
                summary=f"DM sent to {target_id}",
            )
        except Exception:
            pass


def setup(bot):
    bot.add_cog(OwnerToolsCog(bot))
