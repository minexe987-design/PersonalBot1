"""
Check which scanner tokens can access each /in-cheating-servers watched guild.

Reads MOD_DISCORD_USER_TOKEN_1 through MOD_DISCORD_USER_TOKEN_5 from .env or the
current environment. It never prints token values.
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

from commands.cheating_servers_cmd import CHEATING_SERVERS
from core import discord_pool


SCANNER_ENVS = list(discord_pool.TOKEN_SERVER_RANGES.keys())


def _env_file_keys(path: Path) -> list[str]:
    if not path.exists():
        return []

    keys: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith("MOD_DISCORD_USER_TOKEN"):
            keys.append(key)
    return keys


def _load_environment(env_file: Path) -> None:
    load_dotenv(env_file, override=True)


def _print_env_diagnostics(env_file: Path) -> None:
    print(f"Working directory: {Path.cwd()}")
    print(f"Env file checked:  {env_file}")
    print(f"Env file exists:   {'yes' if env_file.exists() else 'no'}")

    file_keys = _env_file_keys(env_file)
    if file_keys:
        print(f"Token keys in env file: {', '.join(file_keys)}")
    else:
        print("Token keys in env file: none")

    visible = [env for env in SCANNER_ENVS if discord_pool._resolve_token_value(env)]
    print(f"Token keys visible to Python: {len(visible)}/{len(SCANNER_ENVS)}")
    print()


def _request(env_var: str, method: str, path: str, *, timeout: float = 15.0) -> requests.Response:
    url = path if path.startswith("http") else f"{discord_pool.API}{path}"
    response = requests.request(
        method,
        url,
        headers=discord_pool._build_headers(env_var),
        timeout=timeout,
    )
    if response.status_code == 429:
        try:
            retry = float(response.headers.get("Retry-After", "1"))
        except Exception:
            retry = 1.0
        time.sleep(min(max(retry, 1.0), 10.0))
        response = requests.request(
            method,
            url,
            headers=discord_pool._build_headers(env_var),
            timeout=timeout,
        )
    return response


def _get_me(env_var: str) -> tuple[str | None, str]:
    if not discord_pool._resolve_token_value(env_var):
        return None, "not set"

    try:
        response = _request(env_var, "GET", "/users/@me")
    except Exception as exc:
        return None, f"network error: {exc}"

    if response.status_code != 200:
        return None, f"HTTP {response.status_code}"

    data = response.json()
    username = data.get("username") or "?"
    user_id = str(data.get("id") or "")
    return user_id, f"{username} ({user_id})"


def _can_access(env_var: str, guild_id: str, user_id: str) -> tuple[bool, str]:
    try:
        response = _request(env_var, "GET", f"/guilds/{guild_id}/members/{user_id}")
    except Exception as exc:
        return False, f"network error: {exc}"

    if response.status_code == 200:
        return True, "ok"
    if response.status_code in (403, 404):
        return False, f"HTTP {response.status_code}"
    return False, f"HTTP {response.status_code}"


def _format_range(indices: list[int]) -> str:
    if not indices:
        return "[]"
    return "[" + ", ".join(str(i) for i in indices) + "]"


def main() -> int:
    parser = ArgumentParser(description="Check scanner token access to watched guilds.")
    parser.add_argument(
        "--env-file",
        default=str(ROOT / ".env"),
        help="Path to an env file containing MOD_DISCORD_USER_TOKEN_1..6.",
    )
    args = parser.parse_args()
    env_file = Path(args.env_file).expanduser()
    if not env_file.is_absolute():
        env_file = (Path.cwd() / env_file).resolve()

    _load_environment(env_file)
    _print_env_diagnostics(env_file)

    visible_count = sum(1 for env in SCANNER_ENVS if discord_pool._resolve_token_value(env))
    if visible_count == 0:
        print("No scanner tokens are visible to this Python process.")
        print("If you set them in Railway, that only affects Railway, not this local terminal.")
        print("For a local test, put them in .env or set them in this PowerShell window:")
        print('  $env:MOD_DISCORD_USER_TOKEN_1="your_token_here"')
        print("Then run this script again from the same terminal.")
        print()

    print(f"Watching {len(CHEATING_SERVERS)} guilds with {len(SCANNER_ENVS)} scanner env vars.")
    print("This can take around 1-2 minutes because it checks real Discord access.\n")

    access_by_env: dict[str, list[int]] = {env: [] for env in SCANNER_ENVS}
    missing_by_env: dict[str, list[int]] = {env: [] for env in SCANNER_ENVS}
    token_labels: dict[str, str] = {}

    for env_var in SCANNER_ENVS:
        user_id, label = _get_me(env_var)
        token_labels[env_var] = label
        print("=" * 90)
        print(f"{env_var}: {label}")

        if not user_id:
            missing_by_env[env_var] = list(range(1, len(CHEATING_SERVERS) + 1))
            continue

        for index, (guild_id, guild_name) in enumerate(CHEATING_SERVERS, start=1):
            ok, reason = _can_access(env_var, guild_id, user_id)
            if ok:
                access_by_env[env_var].append(index)
                print(f"  YES {index:>2}  {guild_name}")
            else:
                missing_by_env[env_var].append(index)
            time.sleep(0.35)

        print(f"  Accessible: {_format_range(access_by_env[env_var])}")

    print("\n" + "=" * 90)
    print("Suggested disjoint assignment")
    print("=" * 90)

    assigned: dict[str, list[int]] = {env: [] for env in SCANNER_ENVS}
    uncovered: list[tuple[int, str, str]] = []

    for index, (guild_id, guild_name) in enumerate(CHEATING_SERVERS, start=1):
        candidates = [env for env in SCANNER_ENVS if index in access_by_env[env]]
        if not candidates:
            uncovered.append((index, guild_id, guild_name))
            continue

        current_owner = next(
            (
                env
                for env, indices in discord_pool.TOKEN_SERVER_RANGES.items()
                if index in indices and env in candidates
            ),
            None,
        )
        owner = current_owner or min(candidates, key=lambda env: len(assigned[env]))
        assigned[owner].append(index)

    print("TOKEN_SERVER_RANGES: dict[str, list[int]] = {")
    for env_var, indices in assigned.items():
        print(f'    "{env_var}": {_format_range(indices)},')
    print("}")

    print("\nTEMP_UNCOVERED_GUILD_IDS: set[str] = {")
    for index, guild_id, guild_name in uncovered:
        print(f'    "{guild_id}",  # {index} - {guild_name}')
    print("}")

    print("\nCoverage summary:")
    active = len(CHEATING_SERVERS) - len(uncovered)
    print(f"  Covered guilds:   {active}/{len(CHEATING_SERVERS)}")
    print(f"  Uncovered guilds: {len(uncovered)}")
    if uncovered:
        for index, _guild_id, guild_name in uncovered:
            print(f"    {index:>2}  {guild_name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
