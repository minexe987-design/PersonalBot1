"""
Test whether Discord's message search endpoint works with our mod-server
user token. If it does, we can use it to find every ticket a user has
participated in — not just ones where they appear in permission_overwrites.

Search endpoint:
    GET /guilds/{guild_id}/messages/search?author_id={user_id}

Test with:
    1. yhpro1230 (Senior Report Checker, currently shows 2 matches)
    2. deimp12 (Report Checker, currently shows 1 match)
    3. coolguyking778 (Normal Reporter, currently shows 13 matches)
"""

import os
import sys
import time
import requests
from dotenv import load_dotenv
from collections import Counter

load_dotenv()

TOKEN = os.environ.get("MOD_DISCORD_USER_TOKEN", "").strip()
GUILD = os.environ.get("MOD_SERVER_GUILD_ID", "").strip()
HEADERS = {"Authorization": TOKEN}
API = "https://discord.com/api/v10"


def search(user_id: str, offset: int = 0):
    """One search request — returns (total_results, list_of_message_hits)."""
    url = f"{API}/guilds/{GUILD}/messages/search?author_id={user_id}&offset={offset}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    return r


def search_all_channels(user_id: str, max_results: int = 1000):
    """Page through search results, collecting unique channel IDs."""
    channel_ids: set[str] = set()
    sample_messages: list[dict] = []
    total = None
    offset = 0
    requests_made = 0

    while True:
        r = search(user_id, offset=offset)
        requests_made += 1
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", "1"))
            print(f"    [rate-limited] waiting {wait:.1f}s")
            time.sleep(min(wait, 5))
            continue
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {(r.text or '')[:200]}", requests_made
        data = r.json()
        if total is None:
            total = data.get("total_results", 0)

        hits = data.get("messages") or []
        if not hits:
            break

        for hit_group in hits:
            # `messages` is a list of lists — each inner list is a message
            # plus context messages around it. The 0th is the actual hit.
            if not hit_group:
                continue
            msg = hit_group[0] if isinstance(hit_group, list) else hit_group
            channel_id = str(msg.get("channel_id"))
            channel_ids.add(channel_id)
            if len(sample_messages) < 5:
                sample_messages.append({
                    "channel_id": channel_id,
                    "content_preview": (msg.get("content") or "")[:80],
                    "ts": msg.get("timestamp"),
                })

        offset += 25  # Discord search returns 25 per page
        if offset >= total or offset >= max_results:
            break
        time.sleep(0.4)  # be nice to the API

    return {
        "total_results": total,
        "unique_channels": channel_ids,
        "sample": sample_messages,
        "requests": requests_made,
    }, None, requests_made


# Pre-fetch channels so we can identify which results are in ticket categories
print("Pre-fetching all guild channels for category lookup...")
channels_resp = requests.get(f"{API}/guilds/{GUILD}/channels", headers=HEADERS, timeout=15)
all_channels = channels_resp.json()
channel_lookup = {str(c["id"]): c for c in all_channels}
cat_lookup = {str(c["id"]): c.get("name", "?") for c in all_channels if int(c.get("type", -1)) == 4}
print(f"  {len(all_channels)} channels, {len(cat_lookup)} categories")
print()

TEST_USERS = [
    ("1249316104656916556", "deimp12 (Report Checker)"),
    ("891425133732982805", "yhpro1230 (Senior Report Checker)"),
    ("1303509834616012851", "coolguyking778 (Normal Reporter)"),
]

for uid, label in TEST_USERS:
    print("=" * 70)
    print(f"TEST: {label}  uid={uid}")
    print("=" * 70)

    t0 = time.time()
    result, err, n_req = search_all_channels(uid, max_results=2000)
    elapsed = time.time() - t0

    if err:
        print(f"  ERROR: {err}")
        print(f"  ({n_req} request(s) made, {elapsed:.1f}s)")
        continue

    print(f"  Total messages by user (server-wide): {result['total_results']}")
    print(f"  Unique channels they posted in:        {len(result['unique_channels'])}")
    print(f"  Requests made: {n_req}, elapsed: {elapsed:.1f}s")

    # Categorize the channels
    by_category = Counter()
    sample_channels: dict[str, list] = {}
    for cid in result['unique_channels']:
        c = channel_lookup.get(cid)
        if not c:
            by_category["(unknown channel)"] += 1
            continue
        parent = cat_lookup.get(str(c.get("parent_id") or ""), "(no category)")
        by_category[parent] += 1
        sample_channels.setdefault(parent, []).append(c.get("name", "?"))

    print(f"\n  Channels they posted in, grouped by category:")
    for cat, n in by_category.most_common():
        samples = sample_channels.get(cat, [])[:3]
        sample_str = ", ".join(samples)
        if len(sample_channels.get(cat, [])) > 3:
            sample_str += f", +{len(sample_channels[cat]) - 3} more"
        print(f"    {n:4d}  {cat}")
        print(f"         e.g.: {sample_str}")

    print()
    print(f"  Sample messages:")
    for s in result['sample']:
        ch = channel_lookup.get(s['channel_id'], {}).get('name', '?')
        print(f"    [{ch}] {s['ts'][:10]}: {s['content_preview']}")
    print()
