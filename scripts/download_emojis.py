# ──────────────────────────────────────────────────────────────────
# Emoji Rescue Script
# Pulls every custom emoji referenced by the bot off Discord's CDN
# and saves it locally so it can be re-uploaded to a new bot /
# application / server. No auth required — the CDN serves these
# publicly as long as the emoji ID still exists.
# ──────────────────────────────────────────────────────────────────

import os
import sys
import time
import requests

# name -> (id, animated?)  — add/remove freely
EMOJIS: dict[str, tuple[str, bool]] = {
    # From CLAUDE.md (confirmed working)
    "arrow":       ("1497344031238127686", True),   # animated blue arrow
    "clipboard":   ("1497344037294702762", False),
    "check":       ("1497344035696672959", False),
    "x":           ("1497344061592436737", False),
    "warning":     ("1497344059017003079", False),
    "mag":         ("1497344052709036125", True),
    "cart":        ("1497344033553514627", False),
    "gamepass":    ("1497344044811030548", False),
    "moneybag":    ("1497344054990733535", True),
    "crown":       ("1497344039584923778", True),
    "lock":        ("1497344050078941344", False),
    "email":       ("1497344042076344350", False),

    # From the earlier emoji-server screenshot — may or may not work
    "anipinkarrow": ("1497344028004581386", True),
    "greencheck":   ("1497344048267137144", False),
    "redcheck":     ("1497344057041752235", False),
}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "emojis")
OUT_DIR = os.path.normpath(OUT_DIR)


def fetch(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "emoji-rescue/1.0"})
        if r.status_code == 200 and len(r.content) > 0:
            return r.content
        return None
    except Exception:
        return None


def download(name: str, eid: str, animated: bool) -> str:
    # Try the expected format first, then fall back.
    primary_ext = "gif" if animated else "png"
    fallback_ext = "png" if animated else "gif"

    for ext in (primary_ext, fallback_ext):
        url = f"https://cdn.discordapp.com/emojis/{eid}.{ext}?size=128&quality=lossless"
        data = fetch(url)
        if data:
            path = os.path.join(OUT_DIR, f"{name}.{ext}")
            with open(path, "wb") as f:
                f.write(data)
            return f"  ✅ {name:<14} → {name}.{ext}  ({len(data):,} bytes)"

    return f"  ❌ {name:<14} → FAILED (emoji deleted or ID invalid)"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"\n📂 Saving to: {OUT_DIR}\n")
    print(f"Pulling {len(EMOJIS)} emoji(s) from Discord's CDN...\n")

    succ = 0
    fail = 0
    for name, (eid, animated) in EMOJIS.items():
        result = download(name, eid, animated)
        print(result)
        if "✅" in result:
            succ += 1
        else:
            fail += 1
        time.sleep(0.1)  # be gentle

    print(f"\n── Done ── {succ} saved, {fail} failed\n")
    if succ > 0:
        print(f"📤 Next: open https://discord.com/developers/applications")
        print(f"   → pick your new bot → Emojis tab → drag-upload everything from:")
        print(f"   {OUT_DIR}\n")
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
