import glob
import re

for filepath in glob.glob('commands/*.py'):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Fix multiline @app_commands.command(
    content = re.sub(
        r'@app_commands\.command\(\s*name=[\'\"]([^\'\"]+)[\'\"]\s*,\s*description=[\'\"]([^\'\"]+)[\'\"]\s*\)', 
        r'@discord.slash_command(\n        name="\1",\n        description="\2",\n        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel},\n        integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}\n    )', 
        content
    )
    
    # Also handle some missing options from multiline describe
    content = re.sub(r'@app_commands\.describe\(\s*([a-zA-Z0-9_]+)=[\'\"]([^\'\"]+)[\'\"]\s*\)', r'@discord.option("\1", description="\2")', content)

    # Convert multiline interaction signatures for userinfo_cmd, monitor_cmd, etc
    content = re.sub(r'interaction:\s*discord\.Interaction,', r'ctx: discord.ApplicationContext,', content)

    # Some methods still have 'interaction' but were missing from regex
    content = content.replace('interaction.response.send_message', 'ctx.respond')
    content = content.replace('interaction.followup.send', 'ctx.send')
    content = content.replace('interaction.response.defer()', 'ctx.defer()')
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
