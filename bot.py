# ──────────────────────────────────────────────────────────────────
# Personal Roblox Account Management Discord Bot
# ──────────────────────────────────────────────────────────────────

import os
import sys
import asyncio
import time
import signal
import tempfile
import contextlib
from pathlib import Path

import discord
from dotenv import load_dotenv

from core.channel_monitor import start_monitor
from core.command_control import is_command_disabled
from core.logging import (
    configure_webhook_emojis,
    log_bot_stats,
    log_guild_install,
    log_guild_uninstall,
)
from core.tracking import init_db as init_tracking_db

# Force UTF-8 + unbuffered so emojis + ANSI colors work on Windows
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print_error("BOT_TOKEN is missing!")
    sys.exit(1)

from discord.ext import commands
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=["!", "?"], intents=intents)

_SUPPRESS_GUILD_REMOVE_LOGS = True
_MONITOR_TASK = None
_COGS_LOADED = False
_PREFIX_COMMAND_DEDUPE_SECONDS = 300.0
_RECENT_PREFIX_COMMAND_IDS: dict[int, float] = {}
_RECENT_APPLICATION_COMMAND_IDS: dict[int, float] = {}
_INSTANCE_LOCK_HANDLE = None
_SHUTTING_DOWN = False

GREEN = "\033[92m"
RED = "\033[91m"
BLUE = "\033[94m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_banner():
    print(f"""
{BLUE}{BOLD}Logs Bot - starting{RESET}
""")


def print_info(message):
    print(f"  {BLUE}[INFO]{RESET}    {message}")


def print_success(message):
    print(f"  {GREEN}[SUCCESS]{RESET} {message}")


def print_error(message):
    print(f"  {RED}[ERROR]{RESET}   {message}")


def print_loaded(message):
    print(f"  {CYAN}[LOADED]{RESET}  {message}")


def _lock_path() -> Path:
    if os.name == "nt":
        return Path(tempfile.gettempdir()) / "logs-bot-instance.lock"
    return Path("/data/logs-bot-instance.lock")


def acquire_instance_lock(timeout: float = 30.0) -> bool:
    """Prevent overlapping Railway containers from running the same bot token."""
    global _INSTANCE_LOCK_HANDLE
    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    handle = open(path, "a+")
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            _INSTANCE_LOCK_HANDLE = handle
            return True
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                return False
            time.sleep(0.5)


async def shutdown_bot(reason: str):
    global _SHUTTING_DOWN, _MONITOR_TASK
    if _SHUTTING_DOWN:
        return
    _SHUTTING_DOWN = True
    print_info(f"Shutting down: {reason}")
    if _MONITOR_TASK is not None and not _MONITOR_TASK.done():
        _MONITOR_TASK.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(_MONITOR_TASK, timeout=5)
    await bot.close()

COGS = [
    "commands.help_cmd",
    "commands.userinfo_cmd",
    "commands.feedback_cmd",
    "commands.bancheck_cmd",
    "commands.connections_cmd",
    "commands.cheating_servers_cmd",
    "commands.owner_tools_cmd",
    "commands.account_status_cmd",
]


@bot.event
async def on_guild_join(guild: discord.Guild):
    print(f"  {GREEN}[JOIN]{RESET}    ➕ {guild.name} ({guild.member_count or '?'} members)")
    try:
        log_guild_install(guild)
    except Exception as e:
        print_error(f"Failed to log guild install: {e}")

@bot.event
async def on_guild_remove(guild: discord.Guild):
    guild_name = getattr(guild, "name", None)
    if (
        _SUPPRESS_GUILD_REMOVE_LOGS
        or getattr(guild, "unavailable", False)
        or not guild_name
        or guild_name == "?"
    ):
        print_info(f"Skipped guild remove log during disconnect/cache update: {getattr(guild, 'id', '?')}")
        return
    print(f"  {RED}[LEAVE]{RESET}   ➖ {guild.name}")
    try:
        log_guild_uninstall(guild)
    except Exception as e:
        print_error(f"Failed to log guild uninstall: {e}")

async def disabled_command_check(ctx: discord.ApplicationContext) -> bool:
    now = time.monotonic()
    if isinstance(ctx, commands.Context):
        message_id = getattr(getattr(ctx, "message", None), "id", None)
        if message_id is not None:
            for mid, seen_at in list(_RECENT_PREFIX_COMMAND_IDS.items()):
                if now - seen_at > _PREFIX_COMMAND_DEDUPE_SECONDS:
                    _RECENT_PREFIX_COMMAND_IDS.pop(mid, None)
            if message_id in _RECENT_PREFIX_COMMAND_IDS:
                return False
            _RECENT_PREFIX_COMMAND_IDS[message_id] = now
    else:
        interaction_id = getattr(getattr(ctx, "interaction", None), "id", None)
        if interaction_id is not None:
            for iid, seen_at in list(_RECENT_APPLICATION_COMMAND_IDS.items()):
                if now - seen_at > _PREFIX_COMMAND_DEDUPE_SECONDS:
                    _RECENT_APPLICATION_COMMAND_IDS.pop(iid, None)
            if interaction_id in _RECENT_APPLICATION_COMMAND_IDS:
                return False
            _RECENT_APPLICATION_COMMAND_IDS[interaction_id] = now

    command_name = ctx.command.name if ctx.command else ""
    if not command_name or not is_command_disabled(command_name):
        return True

    message = f"`/{command_name}` is disabled rn."
    await ctx.respond(message, ephemeral=True)
    return False

bot.check(disabled_command_check)


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: Exception):
    command_name = ctx.command.name if getattr(ctx, "command", None) else "unknown"
    original = getattr(error, "original", error)
    print_error(f"Slash command /{command_name} failed: {original!r}")

    try:
        await ctx.respond(f"`/{command_name}` failed: `{original}`")
    except Exception as respond_error:
        print_error(f"Failed to send command error response: {respond_error!r}")


def load_cogs_once():
    global _COGS_LOADED
    if _COGS_LOADED:
        return
    for cog in COGS:
        try:
            bot.load_extension(cog)
            print_loaded(f"pkg {cog}")
        except Exception as e:
            print_error(f"Failed to load {cog}: {e!r}")
    _COGS_LOADED = True


@bot.event
async def on_ready():
    global _SUPPRESS_GUILD_REMOVE_LOGS, _MONITOR_TASK
    _SUPPRESS_GUILD_REMOVE_LOGS = False
    load_cogs_once()
    try:
        await bot.sync_commands(delete_existing=True)
        print_success("Slash commands synced")
    except Exception as e:
        print_error(f"Slash command sync failed: {e!r}")
    print_success(f"Logged in as {bot.user.name} ({bot.user.id})")
    configure_webhook_emojis(bot)
    log_bot_stats(bot)
    if _MONITOR_TASK is None or _MONITOR_TASK.done():
        _MONITOR_TASK = bot.loop.create_task(start_monitor())
        print_loaded("channel monitor task queued")


async def main_async():
    print_banner()
    if not acquire_instance_lock():
        print_error("Another Logs Bot instance is still running; exiting to avoid duplicate Discord events.")
        raise SystemExit(75)

    try:
        init_tracking_db()
        print_loaded("tracking DB ready")
    except Exception as e:
        print_error(f"Tracking DB init failed: {e}")

    print()
    print_info("Connecting to Discord...")
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig,
                lambda sig=sig: asyncio.create_task(shutdown_bot(sig.name)),
            )
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(
                    sig,
                    lambda _signum, _frame, sig=sig: asyncio.create_task(shutdown_bot(sig.name)),
                )
            except Exception:
                pass

    try:
        async with bot:
            await bot.start(BOT_TOKEN)
    finally:
        await shutdown_bot("main exit")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
