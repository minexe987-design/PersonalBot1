"""
Resolve invite codes to guild IDs + names so we can build the cheating-
server watch list as a hardcoded constant.

Uses the public /invites/{code} endpoint — does NOT require the spy
account to even be in the server, but our token is in all of them
already.
"""

import os
import sys
import time
import re
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("MOD_DISCORD_USER_TOKEN", "").strip()
HEADERS = {"Authorization": TOKEN}
API = "https://discord.com/api/v10"

INVITES = """
https://discord.gg/aUHkbKqs
https://discord.gg/9WHvkxEN
https://discord.gg/voltbz
https://discord.com/invite/6zSphHQXcF
https://discord.gg/XuAcnvvr
https://discord.gg/olemad
https://discord.gg/velocityide
https://discord.gg/xe-no
https://discord.gg/ronixstudios
https://discord.gg/Vg6xq28aw
https://discord.gg/US73RJwcce
https://discord.gg/macsploit
https://discord.gg/opiumware
https://discord.gg/deltax
https://discord.gg/Q5df7WJRDh
https://discord.gg/vegasupport
https://discord.gg/mg33ywhN
https://discord.gg/XDNbtZ2A
https://discord.gg/JfWVRVaQ
https://discord.gg/DzXQZ9pV
https://discord.gg/EvbbZZH5
https://discord.gg/matchalattewin
https://discord.com/invite/Sunss2YYwB
https://discord.com/invite/vro
https://discord.com/invite/coi
https://discord.gg/cheatsmarket
https://discord.gg/alchemyhub
https://discord.gg/ilya
https://discord.gg/bloxproducts
https://discord.gg/kicia
https://discord.gg/catvape
https://discord.gg/voidware
https://discord.gg/bunni-fun
https://discord.gg/wearedevs
https://discord.gg/D5PqaxNpvp
https://discord.gg/bestscript
https://discord.gg/fluxus
https://discord.gg/getfalcon
https://discord.gg/xRaswkXTy
https://discord.gg/UBxxUfYp
https://discord.com/invite/GNHbGPbah2
https://discord.com/invite/uNCJBEj9aH
https://discord.com/invite/wQKjUYf99A
https://discord.com/invite/7Es9WeTF4K
https://discord.gg/getcosmic
https://discord.com/invite/wyv
""".strip().splitlines()


def code_from_url(url: str) -> str:
    # https://discord.gg/CODE  OR  https://discord.com/invite/CODE
    m = re.search(r"(?:discord\.gg/|discord\.com/invite/)([A-Za-z0-9-]+)", url.strip())
    return m.group(1) if m else ""


print(f"Resolving {len(INVITES)} invite(s)...\n")
print(f"{'Code':<20}  {'Guild ID':<22}  Guild Name")
print("-" * 90)

resolved: list[tuple[str, str]] = []  # (guild_id, guild_name)
seen_ids: set[str] = set()
failures: list[tuple[str, str]] = []

for url in INVITES:
    code = code_from_url(url)
    if not code:
        print(f"{'(unparsable)':<20}  -                       {url}")
        continue
    r = requests.get(f"{API}/invites/{code}?with_counts=true", headers=HEADERS, timeout=15)
    if r.status_code == 429:
        time.sleep(min(float(r.headers.get("Retry-After", "1")), 5))
        r = requests.get(f"{API}/invites/{code}?with_counts=true", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"{code:<20}  -                       FAILED HTTP {r.status_code}")
        failures.append((code, f"HTTP {r.status_code}"))
        continue
    data = r.json()
    guild = data.get("guild") or {}
    gid = str(guild.get("id") or "")
    gname = guild.get("name") or "?"
    member_count = data.get("approximate_member_count") or "?"
    if gid in seen_ids:
        print(f"{code:<20}  {gid:<22}  {gname}  [DUPE]")
        continue
    seen_ids.add(gid)
    resolved.append((gid, gname))
    print(f"{code:<20}  {gid:<22}  {gname} ({member_count} members)")
    time.sleep(0.3)

print()
print("=" * 90)
print(f"Resolved {len(resolved)} unique guild(s).")
if failures:
    print(f"Failures: {failures}")

print()
print("=" * 90)
print("READY-TO-PASTE CONSTANT for commands/bancheck_cmd.py:")
print("=" * 90)
print()
print("CHEATING_SERVERS: list[tuple[str, str]] = [")
for gid, gname in resolved:
    safe_name = gname.replace('"', '\\"')
    print(f'    ("{gid}", "{safe_name}"),')
print("]")
