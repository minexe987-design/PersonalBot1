"""
List guilds visible to one Discord scanner token.

Usage:
  python scripts/list_discord_token_guilds.py MOD_DISCORD_USER_TOKEN_6

Reads .env by default. Never prints token values.
"""

from __future__ import annotations

import os
import sys
import time
from argparse import ArgumentParser
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import discord_pool


def _request(env_var: str, path: str) -> requests.Response:
    url = path if path.startswith("http") else f"{discord_pool.API}{path}"
    response = requests.get(url, headers=discord_pool._build_headers(env_var), timeout=20)
    if response.status_code == 429:
        try:
            retry = float(response.headers.get("Retry-After", "1"))
        except Exception:
            retry = 1.0
        time.sleep(min(max(retry, 1.0), 10.0))
        response = requests.get(url, headers=discord_pool._build_headers(env_var), timeout=20)
    return response


def main() -> int:
    parser = ArgumentParser(description="List guilds visible to a Discord user token env var.")
    parser.add_argument("env_var", help="Example: MOD_DISCORD_USER_TOKEN_6")
    parser.add_argument(
        "--env-file",
        default=str(ROOT / ".env"),
        help="Path to .env file. Defaults to project .env.",
    )
    args = parser.parse_args()

    env_file = Path(args.env_file).expanduser()
    if not env_file.is_absolute():
        env_file = (Path.cwd() / env_file).resolve()
    load_dotenv(env_file, override=True)

    env_var = args.env_var.strip()
    if not os.environ.get(env_var, "").strip():
        print(f"{env_var} is not visible to Python. Set it in this shell or put it in {env_file}.")
        return 1

    me = _request(env_var, "/users/@me")
    if me.status_code != 200:
        print(f"{env_var}: /users/@me failed with HTTP {me.status_code}")
        return 1
    me_data = me.json()
    print(f"{env_var}: {me_data.get('username')} ({me_data.get('id')})")

    response = _request(env_var, "/users/@me/guilds")
    if response.status_code != 200:
        print(f"/users/@me/guilds failed with HTTP {response.status_code}: {response.text[:200]}")
        return 1

    guilds = response.json()
    if not isinstance(guilds, list):
        print("Unexpected response shape.")
        return 1

    print(f"Guilds visible: {len(guilds)}")
    for guild in sorted(guilds, key=lambda g: (g.get("name") or "").lower()):
        print(f'{guild.get("id")}  {guild.get("name")}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
