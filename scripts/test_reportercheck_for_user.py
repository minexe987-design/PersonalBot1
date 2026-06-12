"""
Diagnostic: simulate /reportercheck for a specific user_id, dump the
data the embed would have access to so we can design the new layout.

Usage:
    railway run python scripts/test_reportercheck_for_user.py <user_id>
"""

import os
import sys
import time
from collections import Counter, defaultdict

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("MOD_DISCORD_USER_TOKEN", "").strip()
GUILD = os.environ.get("MOD_SERVER_GUILD_ID", "").strip()

if not TOKEN or not GUILD:
    print("ERROR: env vars missing")
    sys.exit(1)

if len(sys.argv) < 2:
    print("Usage: test_reportercheck_for_user.py <user_id>")
    sys.exit(1)

USER_ID = sys.argv[1].strip()
VIEW_CHANNEL = 1 << 10
HEADERS = {"Authorization": TOKEN}
API = "https://discord.com/api/v10"


def get(path):
    r = requests.get(f"{API}{path}", headers=HEADERS, timeout=15)
    if r.status_code == 429:
        time.sleep(min(float(r.headers.get("Retry-After", "1")), 5))
        r = requests.get(f"{API}{path}", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    return r.json(), None


print(f"Looking up user {USER_ID} in guild {GUILD}...")

# Fetch member data (their roles)
member, err = get(f"/guilds/{GUILD}/members/{USER_ID}")
if err or not member:
    print(f"  Member fetch failed: {err}")
    sys.exit(1)

user = member.get("user", {})
print(f"  User: {user.get('username')} ({user.get('global_name') or '-'})")
member_role_ids = member.get("roles", [])
print(f"  Has {len(member_role_ids)} guild role(s)")

# Fetch all guild roles for name lookup
roles, _ = get(f"/guilds/{GUILD}/roles")
role_name = {str(r["id"]): r.get("name", "?") for r in (roles or [])}
my_role_names = [role_name.get(rid, rid) for rid in member_role_ids]
print(f"  Roles: {my_role_names}")

# Fetch all channels and find ones where this user has a member overwrite
print("\nFetching channels...")
channels, _ = get(f"/guilds/{GUILD}/channels")
print(f"  {len(channels or [])} channels")

cats = {str(c["id"]): c.get("name", "?") for c in channels if int(c.get("type", -1)) == 4}

matches = []
for c in channels:
    if int(c.get("type", -1)) == 4:
        continue
    for ow in c.get("permission_overwrites") or []:
        if ow.get("type") == 1 and str(ow.get("id") or "") == USER_ID:
            allow = int(ow.get("allow") or 0)
            deny = int(ow.get("deny") or 0)
            matches.append({
                "channel": c.get("name"),
                "id": c.get("id"),
                "category": cats.get(str(c.get("parent_id") or ""), "(none)"),
                "allow": allow,
                "deny": deny,
                "view_state": (
                    "OPEN (allow_VIEW)" if (allow & VIEW_CHANNEL)
                    else "BLOCKED (deny_VIEW)" if (deny & VIEW_CHANNEL)
                    else "CLOSED (neutral)"
                ),
                "all_member_overwrites": [
                    {
                        "id": str(o.get("id")),
                        "allow": int(o.get("allow") or 0),
                        "deny": int(o.get("deny") or 0),
                    }
                    for o in c.get("permission_overwrites") or []
                    if o.get("type") == 1
                ],
            })

print(f"\nFound {len(matches)} channel(s) with this user in permission_overwrites.\n")

# Bucket by view_state
state_counts = Counter(m["view_state"] for m in matches)
print("Status breakdown:")
for state, n in state_counts.most_common():
    print(f"  {n:4d}  {state}")

# Now look at "co-occupants" — who else has overwrites in each ticket the
# user is in? This is what the friend's classification idea uses.
print("\n" + "=" * 70)
print("CO-OCCUPANT ANALYSIS (other members in same tickets)")
print("=" * 70)

co_occupant_ids = Counter()
for m in matches:
    for o in m["all_member_overwrites"]:
        if o["id"] != USER_ID:
            co_occupant_ids[o["id"]] += 1

print(f"\nTop co-occupants (people who appear with {USER_ID} in tickets):")
for uid, n in co_occupant_ids.most_common(10):
    print(f"  {n:4d}  {uid}")
print(f"\n  (id 557628352828014614 = TicketTool bot — present in every ticket)")

# Pull a few sample tickets to inspect roles of co-occupants
print("\n" + "=" * 70)
print("SAMPLE TICKETS — co-occupant roles")
print("=" * 70)
shown = 0
for m in matches[:30]:
    if shown >= 8:
        break
    others = [o for o in m["all_member_overwrites"] if o["id"] != USER_ID and o["id"] != "557628352828014614"]
    if not others:
        continue
    print(f"\n  [{m['view_state']}] {m['category']} / {m['channel']}")
    print(f"     Looked-up user (this user): allow={m['allow']} deny={m['deny']}")
    for o in others:
        # Fetch role names for this co-occupant
        co_member, _ = get(f"/guilds/{GUILD}/members/{o['id']}")
        if co_member:
            co_user = co_member.get("user", {})
            co_roles = [role_name.get(rid, rid) for rid in co_member.get("roles", [])]
        else:
            co_user = {"username": "?"}
            co_roles = []
        print(f"     Co-occupant {co_user.get('username')} ({o['id']})")
        print(f"        allow={o['allow']} deny={o['deny']}")
        print(f"        roles: {co_roles}")
    shown += 1

print("\n" + "=" * 70)
print("FIRST 20 MATCHES (raw)")
print("=" * 70)
for m in matches[:20]:
    print(f"  [{m['view_state']}] {m['category']:30s} / {m['channel']}")
