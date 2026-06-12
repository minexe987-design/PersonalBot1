"""
Discover the staff role hierarchy in the moderation server, ranked by:
  1. Discord role position (higher = more senior)
  2. Cross-checked against which roles actually grant VIEW_CHANNEL on
     ticket categories (to filter out cosmetic / non-staff roles)

Also samples several real users at different role tiers to see how the
new classification would label each of them.
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

VIEW_CHANNEL = 1 << 10
HEADERS = {"Authorization": TOKEN}
API = "https://discord.com/api/v10"


def get(path):
    r = requests.get(f"{API}{path}", headers=HEADERS, timeout=15)
    if r.status_code == 429:
        time.sleep(min(float(r.headers.get("Retry-After", "1")), 5))
        r = requests.get(f"{API}{path}", headers=HEADERS, timeout=15)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {(r.text or '')[:120]}"
    return r.json(), None


print("Fetching all roles...")
roles, _ = get(f"/guilds/{GUILD}/roles")
print(f"  {len(roles)} roles total")

# Sort by position desc — Discord position represents hierarchy
roles_sorted = sorted(roles, key=lambda r: -int(r.get("position", 0)))

print("\n" + "=" * 80)
print("ALL ROLES BY POSITION (highest first)")
print("=" * 80)
print(f"{'Pos':>4}  {'ID':<20}  {'Name'}")
for r in roles_sorted:
    pos = r.get("position", 0)
    rid = r.get("id", "?")
    name = r.get("name", "?")
    members = r.get("tags", {}) or {}
    extra = ""
    if members.get("bot_id"):
        extra = " [bot role]"
    elif members.get("integration_id"):
        extra = " [integration]"
    elif "premium_subscriber" in members:
        extra = " [booster]"
    print(f"{pos:>4}  {rid:<20}  {name}{extra}")

# Fetch all channels to see which roles grant VIEW_CHANNEL on ticket-like categories
print("\n" + "=" * 80)
print("ROLES WITH VIEW_CHANNEL ALLOW ON TICKET CATEGORIES")
print("=" * 80)
channels, _ = get(f"/guilds/{GUILD}/channels")
TICKET_CAT_HINTS = ("ticket", "report", "ban", "appeal", "exploit", "case", "closet", "blatant", "held", "invalid", "misc", "priority")

role_grants = defaultdict(int)  # role_id -> count of categories where it grants VIEW
role_denies = defaultdict(int)
ticket_categories = []
for c in channels:
    if int(c.get("type", -1)) != 4:
        continue
    name = c.get("name", "")
    if not any(h in name.lower() for h in TICKET_CAT_HINTS):
        continue
    ticket_categories.append(name)
    for ow in c.get("permission_overwrites") or []:
        if ow.get("type") != 0:  # 0 = role, 1 = member
            continue
        rid = str(ow.get("id"))
        allow = int(ow.get("allow") or 0)
        deny = int(ow.get("deny") or 0)
        if allow & VIEW_CHANNEL:
            role_grants[rid] += 1
        if deny & VIEW_CHANNEL:
            role_denies[rid] += 1

print(f"\n  Found {len(ticket_categories)} ticket categories: {ticket_categories[:5]}...")

role_lookup = {str(r["id"]): r for r in roles}
print(f"\n  Roles with VIEW_CHANNEL grants on ticket categories (sorted by # of grants):")
print(f"  {'Grants':>6}  {'Pos':>4}  {'Role Name':<35}  Role ID")
candidate_staff = []
for rid, n in sorted(role_grants.items(), key=lambda x: -x[1]):
    r = role_lookup.get(rid, {})
    pos = r.get("position", 0)
    rname = r.get("name", "?")
    print(f"  {n:>6}  {pos:>4}  {rname:<35}  {rid}")
    if n >= 1 and pos > 0:  # only roles that actually grant view
        candidate_staff.append((pos, rname, rid))

candidate_staff.sort(reverse=True)
print(f"\n  --> {len(candidate_staff)} candidate STAFF roles identified.")

# Heuristic — flag the @everyone role and bot/integration roles
print("\n" + "=" * 80)
print("PROPOSED STAFF HIERARCHY (highest position first, after filtering)")
print("=" * 80)

# Filter out @everyone, bot roles, integrations, and roles that look cosmetic
STAFF_KEYWORDS = ("checker", "staff", "admin", "mod", "owner", "manager", "head", "senior", "lead", "verified", "reporter", "rc", "member")
COSMETIC_HINTS = ("booster", "premium", "color", "vc only", "dnd", "sleeping", "online")

staff_hierarchy = []
for pos, name, rid in candidate_staff:
    r = role_lookup.get(rid, {})
    tags = r.get("tags") or {}
    if rid == GUILD:  # @everyone
        continue
    if tags.get("bot_id") or tags.get("integration_id"):
        continue
    if any(h in name.lower() for h in COSMETIC_HINTS):
        continue
    staff_hierarchy.append((pos, name, rid))

print(f"\n  Hierarchy ({len(staff_hierarchy)} staff-like roles, top-->bottom):")
print(f"  {'Pos':>4}  {'Role Name':<40}  Role ID")
for pos, name, rid in staff_hierarchy:
    print(f"  {pos:>4}  {name:<40}  {rid}")

# Now classify several test users at different tiers to see how the
# proposed labelling would render
print("\n" + "=" * 80)
print("CLASSIFICATION TEST — sample real users")
print("=" * 80)

# Pull a member list (highest-position first ~ most senior staff first)
print("\n  Fetching server member list (capped at 1000)...")
members = []
after = "0"
for _ in range(10):
    page, _ = get(f"/guilds/{GUILD}/members?limit=1000&after={after}")
    if not page:
        break
    members.extend(page)
    if len(page) < 1000:
        break
    after = page[-1]["user"]["id"]
print(f"  Pulled {len(members)} members.")

staff_role_ids = {rid for _, _, rid in staff_hierarchy}


def classify(member_data) -> str:
    """Pick the highest-position staff role; fall back to 'Normal Reporter'."""
    if not member_data:
        return "Unknown (not in server)"
    user_role_ids = set(member_data.get("roles") or [])
    matched = [(pos, name) for (pos, name, rid) in staff_hierarchy if rid in user_role_ids]
    if matched:
        matched.sort(reverse=True)
        return matched[0][1]
    return "Normal Reporter"


# Pick representatives at different tiers
print("\n  TIER REPRESENTATIVES — pick one user per staff tier:\n")
seen_tiers: set[str] = set()
for m in members:
    user = m.get("user") or {}
    if user.get("bot"):
        continue
    label = classify(m)
    if label in seen_tiers:
        continue
    seen_tiers.add(label)
    uname = user.get("username") or user.get("id")
    uid = user.get("id")
    print(f"    [{label:30s}]  {uname} ({uid})")
    if len(seen_tiers) >= len(staff_hierarchy) + 2:
        break

# Specifically classify the user we already tested
test_uid = "1303509834616012851"
test_member, _ = get(f"/guilds/{GUILD}/members/{test_uid}")
print(f"\n  Specific test for {test_uid}:")
print(f"    classify() --> '{classify(test_member)}'")

# Distribution: how many members fall in each tier?
print("\n" + "=" * 80)
print("MEMBER DISTRIBUTION ACROSS TIERS")
print("=" * 80)
tier_count = Counter()
for m in members:
    if (m.get("user") or {}).get("bot"):
        continue
    tier_count[classify(m)] += 1
for tier, n in sorted(tier_count.items(), key=lambda x: -x[1]):
    print(f"  {n:>5}  {tier}")
print(f"\n  Total non-bot members sampled: {sum(tier_count.values())}")
