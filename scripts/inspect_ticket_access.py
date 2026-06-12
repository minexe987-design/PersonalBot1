import argparse
import os
from typing import Any, Optional

import requests
from dotenv import load_dotenv


API_BASE = "https://discord.com/api/v10"
VIEW_CHANNEL = 1 << 10


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": token}


def _get_json(path: str, token: str) -> tuple[Optional[Any], Optional[str]]:
    try:
        response = requests.get(f"{API_BASE}{path}", headers=_headers(token), timeout=15)
    except Exception as exc:
        return None, f"request error: {exc}"

    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {(response.text or '')[:200]}"

    try:
        return response.json(), None
    except Exception as exc:
        return None, f"json error: {exc}"


def _display_member(member: dict) -> str:
    user = member.get("user") or {}
    nick = member.get("nick")
    global_name = user.get("global_name")
    username = user.get("username")
    user_id = user.get("id")
    label = nick or global_name or username or user_id or "unknown-user"
    return f"{label} ({user_id})" if user_id else str(label)


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Inspect matched ticket channel permission overwrites without changing the bot.",
    )
    parser.add_argument("username", help="Username substring to search for in ticket/channel names.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum matched channels to inspect.")
    args = parser.parse_args()

    token = os.environ.get("MOD_DISCORD_USER_TOKEN", "").strip()
    guild_id = os.environ.get("MOD_SERVER_GUILD_ID", "").strip()
    if not token or not guild_id:
        print("Missing MOD_DISCORD_USER_TOKEN or MOD_SERVER_GUILD_ID.")
        return 1

    channels, err = _get_json(f"/guilds/{guild_id}/channels", token)
    if err:
        print(f"Could not fetch channels: {err}")
        return 1
    if not isinstance(channels, list):
        print("Unexpected channels response shape.")
        return 1

    roles_data, roles_err = _get_json(f"/guilds/{guild_id}/roles", token)
    roles_by_id = {}
    if isinstance(roles_data, list):
        roles_by_id = {str(role.get("id")): role.get("name") for role in roles_data}
    elif roles_err:
        print(f"Could not fetch roles for names: {roles_err}")

    needle = args.username.lower()
    matches = [
        channel for channel in channels
        if needle in str(channel.get("name") or "").lower()
    ]

    print(f"Matched {len(matches)} channel(s) for {args.username!r}. Inspecting {min(len(matches), args.limit)}.")
    print()

    for index, channel in enumerate(matches[:args.limit], start=1):
        channel_id = str(channel.get("id") or "")
        channel_name = str(channel.get("name") or "?")
        overwrites = channel.get("permission_overwrites") or []
        if not isinstance(overwrites, list):
            overwrites = []

        print(f"{index}. #{channel_name}")
        print(f"   URL: https://discord.com/channels/{guild_id}/{channel_id}")
        print(f"   Overwrites: {len(overwrites)}")

        for overwrite in overwrites:
            target_id = str(overwrite.get("id") or "")
            target_type = overwrite.get("type")
            try:
                allow = int(overwrite.get("allow") or 0)
                deny = int(overwrite.get("deny") or 0)
            except Exception:
                allow = 0
                deny = 0

            view_state = "allow_view" if allow & VIEW_CHANNEL else "no_view_allow"
            if deny & VIEW_CHANNEL:
                view_state = "deny_view"

            if target_type == 0:
                role_name = roles_by_id.get(target_id) or "unknown-role"
                print(f"   ROLE   {role_name} ({target_id}) [{view_state}]")
                continue

            if target_type == 1:
                member, member_err = _get_json(f"/guilds/{guild_id}/members/{target_id}", token)
                if isinstance(member, dict):
                    member_roles = [
                        roles_by_id.get(str(role_id), str(role_id))
                        for role_id in member.get("roles", [])
                    ]
                    roles_text = ", ".join(member_roles) if member_roles else "no roles"
                    print(f"   MEMBER {_display_member(member)} [{view_state}]")
                    print(f"          roles: {roles_text}")
                else:
                    print(f"   MEMBER {target_id} [{view_state}] (member lookup failed: {member_err})")
                continue

            print(f"   TYPE {target_type} {target_id} [{view_state}]")

        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
