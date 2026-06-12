"""
One-shot diagnostic: inspect permission_overwrites on a sample of mod-server
channels to settle whether closed tickets:
  (a) have the reporter REMOVED from permission_overwrites entirely, or
  (b) have the reporter STILL THERE but with allow/deny zeroed.

This determines whether the current /reportercheck logic
(_has_member_overwrite checks presence only, not allow bits) is
accidentally correct or has a false-positive case.

Usage:
    python scripts/inspect_ticket_close_logic.py

Reads MOD_DISCORD_USER_TOKEN and MOD_SERVER_GUILD_ID from environment / .env.
Read-only. Does not modify anything in Discord.
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
    print("ERROR: MOD_DISCORD_USER_TOKEN and MOD_SERVER_GUILD_ID must be set.")
    print("If they only exist in Railway, copy them into a local .env temporarily.")
    sys.exit(1)

VIEW_CHANNEL = 1 << 10
HEADERS = {"Authorization": TOKEN}
API = "https://discord.com/api/v10"


def get(path):
    r = requests.get(f"{API}{path}", headers=HEADERS, timeout=15)
    if r.status_code == 429:
        retry = float(r.headers.get("Retry-After", "1"))
        time.sleep(min(retry, 5))
        r = requests.get(f"{API}{path}", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


print("Fetching guild meta...")
guild = get(f"/guilds/{GUILD}")
print(f"  Guild: {guild.get('name')} (id {GUILD})")

print("Fetching channels...")
channels = get(f"/guilds/{GUILD}/channels")
print(f"  {len(channels)} channels visible to this user token.")
print()

# Categorize by parent (category) name
cats = {str(c["id"]): c.get("name", "?") for c in channels if int(c.get("type", -1)) == 4}
by_category = defaultdict(list)
for c in channels:
    if int(c.get("type", -1)) == 4:
        continue  # skip the categories themselves
    parent = cats.get(str(c.get("parent_id") or ""), "(no category)")
    by_category[parent].append(c)

print("=" * 70)
print("CATEGORIES (channel counts):")
print("=" * 70)
for cat_name, items in sorted(by_category.items(), key=lambda x: -len(x[1])):
    print(f"  {len(items):4d}  {cat_name}")
print()

# Heuristic: ticket channels usually live in categories whose names contain
# 'ticket', 'report', 'ban', 'closed', 'open', etc. Also exploit/dispute keywords.
TICKET_CAT_HINTS = ("ticket", "report", "ban", "closed", "open", "appeal", "exploit", "log", "case")
ticket_cats = [
    name for name in by_category
    if any(h in name.lower() for h in TICKET_CAT_HINTS)
]
print("=" * 70)
print("CATEGORIES MATCHING TICKET HINTS:", ticket_cats)
print("=" * 70)
print()


def member_overwrites(c):
    """Return list of (member_id, allow, deny) for type=1 overwrites only."""
    out = []
    for ow in c.get("permission_overwrites") or []:
        if ow.get("type") == 1:  # member, not role
            out.append((
                str(ow.get("id")),
                int(ow.get("allow") or 0),
                int(ow.get("deny") or 0),
            ))
    return out


# Walk every channel in ticket-looking categories. Bucket by (category-keyword,
# whether the channel has any member-overwrites, and whether VIEW_CHANNEL is
# in allow vs deny vs neutral).
buckets = Counter()
samples_by_bucket = defaultdict(list)

for cat_name, items in by_category.items():
    if not any(h in cat_name.lower() for h in TICKET_CAT_HINTS):
        continue
    for c in items:
        ows = member_overwrites(c)
        if not ows:
            label = (cat_name, "no_member_overwrites")
            buckets[label] += 1
            if len(samples_by_bucket[label]) < 2:
                samples_by_bucket[label].append({
                    "channel": c.get("name"),
                    "id": c.get("id"),
                })
            continue
        for member_id, allow, deny in ows:
            view_state = (
                "allow_VIEW" if (allow & VIEW_CHANNEL)
                else "deny_VIEW" if (deny & VIEW_CHANNEL)
                else "neutral_VIEW"
            )
            label = (cat_name, view_state)
            buckets[label] += 1
            if len(samples_by_bucket[label]) < 3:
                samples_by_bucket[label].append({
                    "channel": c.get("name"),
                    "id": c.get("id"),
                    "member_id": member_id,
                    "allow": allow,
                    "deny": deny,
                })

print("=" * 70)
print("BUCKET COUNTS — (category, member-overwrite VIEW_CHANNEL state):")
print("=" * 70)
for (cat, state), n in sorted(buckets.items(), key=lambda x: (-x[1],)):
    print(f"  {n:4d}  {state:25s}  {cat}")
print()

print("=" * 70)
print("SAMPLE CHANNELS PER BUCKET (up to 3 each):")
print("=" * 70)
for (cat, state), samples in sorted(samples_by_bucket.items()):
    print(f"\n  [{cat}] — {state}")
    for s in samples:
        print(f"    {s}")
print()

print("=" * 70)
print("INTERPRETATION GUIDE")
print("=" * 70)
print("""
  If 'CLOSED'-named categories show MOSTLY 'no_member_overwrites':
    → ticket bot REMOVES the overwrite when the ticket closes
    → your friend's stated logic is wrong, but your code happens to be correct
      because _has_member_overwrite returns False (no overwrite present)

  If 'CLOSED'-named categories show MOSTLY 'neutral_VIEW' or 'deny_VIEW':
    → ticket bot LEAVES the overwrite but zeros / denies VIEW_CHANNEL
    → your friend's stated logic is exactly right
    → your current _has_member_overwrite is BUGGY: it would falsely report
      these closed tickets as 'active reports'
    → fix would be: check allow & VIEW_CHANNEL before counting as match

  If 'OPEN'-named categories show MOSTLY 'allow_VIEW':
    → confirms reporter gets explicit VIEW_CHANNEL allow on open tickets

  If categories aren't named with open/closed split (one mixed bucket):
    → look at whether the same category has both 'allow_VIEW' and
      'no_member_overwrites' rows. The mix tells you the close-mechanism.
""")
