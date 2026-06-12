import os
import re
import glob

for filepath in glob.glob('commands/*.py'):
    if 'voice_record_cmd.py' in filepath or 'help_cmd.py' in filepath or 'feedback_cmd.py' in filepath or 'refresh_cmd.py' in filepath:
        continue

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        
    # Replace imports
    content = content.replace('from discord import app_commands, ui', 'import discord\nfrom discord import ui')
    content = content.replace('from discord import app_commands', 'import discord\nfrom discord.ext import commands')
    
    # Replace decorators
    content = re.sub(
        r'@app_commands\.command\((.*?)\)', 
        r'@discord.slash_command(\1,\n        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel},\n        integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}\n    )', 
        content
    )
    
    # Remove old contexts/installs
    content = re.sub(r'\s*@app_commands\.allowed_installs\([^\)]*\)', '', content)
    content = re.sub(r'\s*@app_commands\.allowed_contexts\([^\)]*\)', '', content)
    
    # Replace describe with option
    content = re.sub(r'@app_commands\.describe\(\s*([a-zA-Z0-9_]+)\s*=\s*(.*?)\s*\)', r'@discord.option("\1", description=\2)', content)
    
    # setup function
    content = re.sub(r'async def setup\(bot: commands\.Bot\):', 'def setup(bot):', content)
    content = re.sub(r'await bot\.add_cog\((.*?)\)', r'bot.add_cog(\1)', content)
    
    # Replace interaction with ctx in def lines (for slash commands)
    content = re.sub(r'(async def [a-zA-Z0-9_]+\(.*?)interaction:\s*discord\.Interaction(.*?)\):', r'\1ctx: discord.ApplicationContext\2):', content)
    
    # Replace response methods
    content = content.replace('interaction.response.send_message', 'ctx.respond')
    content = content.replace('interaction.followup.send', 'ctx.send')
    content = content.replace('interaction.response.defer()', 'ctx.defer()')
    content = content.replace('interaction.response.defer', 'ctx.defer')
    
    # Replace interaction.user with ctx.author IF it's ctx now.
    # We will just replace `interaction.` with `ctx.` everywhere, BUT only if we also fix UI components manually later.
    content = content.replace('interaction.user', 'ctx.author')
    content = content.replace('interaction.client', 'ctx.bot')
    content = content.replace('interaction.guild', 'ctx.guild')
    content = content.replace('log_user_first_use(interaction', 'log_user_first_use(ctx')
    content = content.replace('log_command(interaction', 'log_command(ctx')
    content = content.replace('log_inputs(\n            interaction', 'log_inputs(\n            ctx')
    content = content.replace('log_result(interaction', 'log_result(ctx')
    content = content.replace('log_result(\n                interaction', 'log_result(\n                ctx')
    
    # Groups
    content = re.sub(r'app_commands\.Group\(', r'discord.SlashCommandGroup(', content)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f'Migrated {filepath}')
