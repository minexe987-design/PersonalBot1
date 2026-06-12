"""
Validate the new _classify_user_role() against real users.

Picks several users we know about and prints what label they'd get.
"""

import os
import sys
import time
import requests
from dotenv import load_dotenv

load_dotenv()

# Reach into the bot module to use the real function
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from commands.bancheck_cmd import _classify_user_role, STAFF_ROLE_HIERARCHY

TOKEN = os.environ.get("MOD_DISCORD_USER_TOKEN", "").strip()
GUILD = os.environ.get("MOD_SERVER_GUILD_ID", "").strip()
HEADERS = {"Authorization": TOKEN}


def get_member(uid: str):
    r = requests.get(
        f"https://discord.com/api/v10/guilds/{GUILD}/members/{uid}",
        headers=HEADERS, timeout=10,
    )
    if r.status_code == 429:
        time.sleep(min(float(r.headers.get("Retry-After", "1")), 5))
        r = requests.get(
            f"https://discord.com/api/v10/guilds/{GUILD}/members/{uid}",
            headers=HEADERS, timeout=10,
        )
    return r.json() if r.status_code == 200 else None


# Fetch role names for nice display
roles_resp = requests.get(
    f"https://discord.com/api/v10/guilds/{GUILD}/roles",
    headers=HEADERS, timeout=10,
).json()
role_name = {str(r["id"]): r["name"] for r in roles_resp}


print("Staff hierarchy in code:")
for rid, name in STAFF_ROLE_HIERARCHY:
    print(f"  {name:30s}  {rid}")
print()


# Try the user we already tested (known to be Normal Reporter)
test_users = [
    "1303509834616012851",  # coolguyking778 / BaconPro - confirmed Normal Reporter
]

# Add a user from each staff role we can find via channels with member overwrites.
# Easiest way: scan all guild channels for member overwrites where the user
# happens to also have a known staff role. But user-token can't list members.
# Instead, just test the user provided + sniff overwrites for a few staff IDs
# that I noticed during the inspection.

# Pull channel data quickly to find users with various roles.
channels = requests.get(
    f"https://discord.com/api/v10/guilds/{GUILD}/channels",
    headers=HEADERS, timeout=15,
).json()

# Look for any user that has overwrites in channels and check their roles
# until we find one of each staff tier.
print("=" * 70)
print("SAMPLING REAL USERS FROM CHANNEL OVERWRITES")
print("=" * 70)

seen_uids = set()
seen_labels = {}
checked = 0
for c in channels:
    if checked > 80:
        break
    for ow in c.get("permission_overwrites") or []:
        if ow.get("type") != 1:
            continue
        uid = str(ow.get("id"))
        if uid in seen_uids or uid == "557628352828014614":  # skip bot
            continue
        seen_uids.add(uid)
        member = get_member(uid)
        if not member:
            continue
        label = _classify_user_role(member)
        checked += 1
        if label not in seen_labels:
            seen_labels[label] = []
        if len(seen_labels[label]) < 3:
            user = member.get("user", {})
            held_role_names = [role_name.get(str(rid), str(rid)) for rid in member.get("roles", [])]
            seen_labels[label].append({
                "id": uid,
                "username": user.get("username") or "?",
                "roles_held": held_role_names,
            })


print(f"\n  Checked {checked} unique members from channel overwrites.\n")
for label in sorted(seen_labels.keys()):
    samples = seen_labels[label]
    print(f"  [{label}]  ({len(samples)} sample{'s' if len(samples) != 1 else ''})")
    for s in samples:
        print(f"    {s['username']:25s} ({s['id']})")
        roles_short = ", ".join(s['roles_held'][:5])
        if len(s['roles_held']) > 5:
            roles_short += f", +{len(s['roles_held']) - 5} more"
        print(f"       roles: {roles_short}")
    print()


# Specific test for the user the human asked about
print("=" * 70)
print(f"SPECIFIC TEST — user 1303509834616012851")
print("=" * 70)
m = get_member("1303509834616012851")
if m:
    label = _classify_user_role(m)
    user = m.get("user", {})
    held = [role_name.get(str(rid), str(rid)) for rid in m.get("roles", [])]
    print(f"  Username:    {user.get('username')}")
    print(f"  Roles held:  {held}")
    print(f"  --> Label:   {label}")
else:
    print("  Member fetch failed.")
