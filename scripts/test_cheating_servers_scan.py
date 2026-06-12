"""
Run the real sharded /in-cheating-servers scan against a few target IDs.

By default this tests scanner token 1's own account plus two older sample IDs.
Set CHEATING_SCAN_TEST_TARGETS to a comma-separated list of Discord user IDs to
override the sample targets.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from commands.cheating_servers_cmd import CHEATING_SERVERS, _scan_user_sharded
from core import discord_pool


def _targets() -> list[tuple[str, str]]:
    raw = os.environ.get("CHEATING_SCAN_TEST_TARGETS", "").strip()
    if raw:
        return [(part.strip(), part.strip()) for part in raw.split(",") if part.strip().isdigit()]

    targets: list[tuple[str, str]] = []
    me = discord_pool.safe_check_token("MOD_DISCORD_USER_TOKEN_1")
    if me.get("alive") is True and me.get("id"):
        targets.append((str(me["id"]), f"SELF / {me.get('username', '?')}"))

    targets.extend([
        ("1303509834616012851", "coolguyking778 (sample)"),
        ("891425133732982805", "yhpro1230 (sample)"),
    ])
    return targets


async def main() -> int:
    uncovered = discord_pool.temporarily_uncovered_servers(CHEATING_SERVERS)
    print(f"Watch list size: {len(CHEATING_SERVERS)} servers")
    print(f"Currently covered: {len(CHEATING_SERVERS) - len(uncovered)} servers")
    print(f"Temporarily paused: {len(uncovered)} servers")
    print()

    for uid, label in _targets():
        print("=" * 70)
        print(f"SCANNING: {label}  uid={uid}")
        print("=" * 70)

        t0 = time.time()
        result = await _scan_user_sharded(uid)
        elapsed = time.time() - t0

        print(f"  Elapsed: {elapsed:.1f}s")
        print(
            "  In: "
            f"{len(result['in_servers'])}, "
            f"Not in: {len(result['not_in'])}, "
            f"Message evidence: {len(result.get('historical_hits') or [])}, "
            f"Errors: {len(result['errors'])}, "
            f"Paused: {len(result.get('uncovered_servers') or [])}"
        )
        print()

        if result["in_servers"]:
            print("  Hits:")
            for hit in result["in_servers"]:
                print(f"    [{hit['guild_name']}]  joined: {hit['joined_at']}")
        if result.get("historical_hits"):
            print("\n  Message history evidence:")
            for hit in result["historical_hits"]:
                print(
                    f"    [{hit['guild_name']}] "
                    f"messages={hit.get('total_messages')} "
                    f"last_message={hit.get('last_message_at')}"
                )
        if result["errors"]:
            print("\n  Errors:")
            for err in result["errors"]:
                print(f"    {err}")
        if result.get("history_errors"):
            print("\n  History search errors:")
            for err in result["history_errors"]:
                print(f"    {err}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
