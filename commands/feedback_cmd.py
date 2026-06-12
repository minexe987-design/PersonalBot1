import discord
from discord.ext import commands

from core.tracking import get_feedback_submission, save_feedback_submission


FEEDBACK_CHANNEL_ID = 1498418436210954322
FEEDBACK_FOOTER_PREFIX = "Feedback ID:"


def _clean(value: str) -> str:
    value = (value or "").strip()
    return value if value else "No response."


def _clip(value: str, limit: int = 1024) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _summary_from(fields: dict[str, str]) -> str:
    lines = []
    for label, value in fields.items():
        text = _clean(value)
        if text != "No response.":
            lines.append(f"**{label}:** {_clip(text, 180)}")
    return "\n".join(lines) if lines else "No written details were provided."


def _build_summary_embed(record: dict) -> discord.Embed:
    fields = record.get("fields") or {}
    submitter = f"{record.get('discord_username') or 'Unknown'} ({record.get('discord_id')})"
    embed = discord.Embed(
        title="New Feedback",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="From", value=submitter, inline=False)
    embed.add_field(name="Summary", value=_clip(_summary_from(fields)), inline=False)
    embed.set_footer(text=f"{FEEDBACK_FOOTER_PREFIX} {record.get('id')}")
    return embed


def _build_full_embed(record: dict) -> discord.Embed:
    fields = record.get("fields") or {}
    submitter = f"{record.get('discord_username') or 'Unknown'} ({record.get('discord_id')})"
    embed = discord.Embed(
        title="Full Feedback Form",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="From", value=submitter, inline=False)
    for label, value in fields.items():
        embed.add_field(name=label, value=_clip(_clean(str(value))), inline=False)
    embed.set_footer(text=f"{FEEDBACK_FOOTER_PREFIX} {record.get('id')}")
    return embed


def _feedback_id_from_message(message: discord.Message) -> str | None:
    if not message.embeds:
        return None
    footer = message.embeds[0].footer.text or ""
    if not footer.startswith(FEEDBACK_FOOTER_PREFIX):
        return None
    feedback_id = footer[len(FEEDBACK_FOOTER_PREFIX):].strip()
    return feedback_id if feedback_id.isdigit() else None


class FeedbackDetailView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Read Full Form",
        style=discord.ButtonStyle.secondary,
        custom_id="feedback:toggle_full_form",
    )
    async def read_full_form(self, button: discord.ui.Button, interaction: discord.Interaction):
        feedback_id = _feedback_id_from_message(interaction.message)
        record = get_feedback_submission(feedback_id) if feedback_id else None
        if not record:
            await interaction.respond(
                "I couldn't find this feedback record. Older feedback from before the database change cannot be reopened.",
                ephemeral=True,
            )
            return

        showing_full = bool(interaction.message.embeds and interaction.message.embeds[0].title == "Full Feedback Form")
        if showing_full:
            button.label = "Read Full Form"
            await interaction.response.edit_message(embed=_build_summary_embed(record), view=self)
            return

        button.label = "Show Summary"
        await interaction.response.edit_message(embed=_build_full_embed(record), view=self)


class FeedbackModal(discord.ui.Modal):
    feedback = discord.ui.TextInput(
        label="Feedback",
        style=discord.InputTextStyle.paragraph,
        required=False,
        max_length=1000,
    )
    suggestions = discord.ui.TextInput(
        label="Suggestions",
        style=discord.InputTextStyle.paragraph,
        required=False,
        max_length=1000,
    )
    bug_report = discord.ui.TextInput(
        label="Bug Report",
        style=discord.InputTextStyle.paragraph,
        required=False,
        max_length=1000,
    )
    improvements = discord.ui.TextInput(
        label="Improvements",
        style=discord.InputTextStyle.paragraph,
        required=False,
        max_length=1000,
    )
    other = discord.ui.TextInput(
        label="Other Details",
        style=discord.InputTextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, bot: commands.Bot):
        super().__init__(title="Bot Feedback")
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        fields = {
            "Feedback": str(self.feedback),
            "Suggestions": str(self.suggestions),
            "Bug Report": str(self.bug_report),
            "Improvements": str(self.improvements),
            "Other Details": str(self.other),
        }

        feedback_id = save_feedback_submission(interaction.user.id, str(interaction.user), fields)
        record = get_feedback_submission(feedback_id) or {
            "id": feedback_id,
            "discord_id": str(interaction.user.id),
            "discord_username": str(interaction.user),
            "fields": fields,
        }
        summary_embed = _build_summary_embed(record)

        channel = self.bot.get_channel(FEEDBACK_CHANNEL_ID)
        if channel is None:
            channel = await self.bot.fetch_channel(FEEDBACK_CHANNEL_ID)

        await channel.send(
            embed=summary_embed,
            view=FeedbackDetailView(),
        )
        await ctx.respond("Feedback submitted. Thank you.")


class FeedbackCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.slash_command(
        name="feedback",
        description="Give feedback to the creators to improve the bot.",
        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel},
        integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}
    )
    async def feedback(self, ctx: discord.ApplicationContext):
        await ctx.send_modal(FeedbackModal(self.bot))


def setup(bot):
    bot.add_view(FeedbackDetailView())
    bot.add_cog(FeedbackCog(bot))
