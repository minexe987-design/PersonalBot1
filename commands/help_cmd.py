# Discord Command: /help
# Lists every command the bot exposes and what it does.

import discord
from discord.ext import commands

COMMANDS = [
    (
        "/connections",
        "<a:mag:1497344052709036125>",
        "Show a Roblox user's friend list or generate a visual friend-connection graph.",
    ),
    (
        "/feedback",
        "<:clipboard:1497344037294702762>",
        "Send feedback, suggestions, bug reports, or improvement ideas to the bot creators.",
    ),
    (
        "/bancheckv2",
        "<a:mag:1497344052709036125>",
        "Search the moderation server's ticket channels for a Roblox username.",
    ),
    (
        "/reportercheck",
        "<a:mag:1497344052709036125>",
        "Check which active ticket / report channels a given Discord user has access to.",
    ),
    (
        "/in-cheating-servers",
        "<a:exploiters:1498648559623344158>",
        "Check if a Discord user is in any known Roblox cheating / exploit servers (with join dates).",
    ),
    (
        "/help",
        "?",
        "Show this list of commands.",
    ),
]


class HelpCog(commands.Cog):
    """Displays a directory of the bot's commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.slash_command(
        name="help",
        description="Show all commands the bot supports",
        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel},
        integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}
    )
    async def help(self, ctx: discord.ApplicationContext):
        from core.logging import log_user_first_use

        log_user_first_use(ctx, "help")

        embed = discord.Embed(
            title="<a:arrow:1497344031238127686> Logs Bot - Command Help",
            description="Quick guide to the bot's tools.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="How It Works",
            value="Use slash commands directly for the action you need.",
            inline=False,
        )
        embed.add_field(
            name="Visibility",
            value="All command output is public in the channel where it was run.",
            inline=False,
        )

        for name, emoji, desc in COMMANDS:
            embed.add_field(name=f"{emoji} `{name}`", value=desc, inline=False)

        await ctx.respond(embed=embed)


def setup(bot):
    bot.add_cog(HelpCog(bot))
