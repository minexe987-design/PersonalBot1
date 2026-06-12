"""
Debug inferred message-history detection for specific watched server indexes.

Example:
  python scripts/debug_cheating_message_history.py 1338186029194154087 11 31 32
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from commands.cheating_servers_cmd import CHEATING_SERVERS
from core import discord_pool


def _env_for_index(index: int) -> str | None:
    for env_var, indices in discord_pool.TOKEN_SERVER_RANGES.items():
        if index in indices:
            return env_var
    return None


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python scripts/debug_cheating_message_history.py USER_ID INDEX [INDEX...]")
        return 2

    user_id = sys.argv[1].strip()
    indexes = [int(arg) for arg in sys.argv[2:]]

    for index in indexes:
        guild_id, guild_name = CHEATING_SERVERS[index - 1]
        env_var = _env_for_index(index)
        print("=" * 90)
        print(f"{index}. {guild_name}")
        print(f"Token: {env_var or 'unassigned'}")
        if not env_var:
            continue

        current = discord_pool.check_membership(env_var, guild_id, user_id)
        print(f"Current membership: {current}")

        search = discord_pool.search_user_messages_in_guild_deep(env_var, guild_id, user_id)
        print(
            "Search: "
            f"found={search.get('found')} "
            f"total={search.get('total_results')} "
            f"scope={search.get('search_scope')} "
            f"error={search.get('error')}"
        )
        if search.get("channel_lookup_error"):
            print(f"Channel lookup error: {search['channel_lookup_error']}")
        if search.get("channel_errors"):
            print("Channel errors:")
            for err in search["channel_errors"]:
                print(f"  {err}")

        for msg in (search.get("messages") or [])[:10]:
            print(
                f"  [{msg.get('timestamp')}] "
                f"#{msg.get('channel_name') or msg.get('channel_id')}: "
                f"{(msg.get('content') or '(no text)')[:120]}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
