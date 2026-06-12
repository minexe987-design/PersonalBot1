# ──────────────────────────────────────────────────────────────────
# Tracking / dashboard datastore.
#
# Backs the /userinfo command. Stores:
#   - Discord users we've seen (id, username, avatar)
#   - Roblox accounts we've ever resolved a cookie for (deduped by
#     username, latest cookie wins globally)
#   - Many-to-many links between Discord users and Roblox accounts
#   - Every command invocation for usage stats + "most recent"
#   - Gamepass creates / purchases ("items")
#
# Storage: SQLite on a Railway persistent volume mounted at /data
# (override with TRACKING_DB_PATH env var for local dev).
# ──────────────────────────────────────────────────────────────────

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional

DB_PATH = os.environ.get(
    "TRACKING_DB_PATH",
    "/data/tracking.db" if os.name != "nt" else "tracking.db",
)

# SQLite is safe for concurrent reads under WAL, but we still serialize
# writes through a process-level lock to avoid writer contention errors.
_WRITE_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS discord_users (
    discord_id TEXT PRIMARY KEY,
    username   TEXT,
    avatar_url TEXT,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS roblox_accounts (
    username           TEXT PRIMARY KEY COLLATE NOCASE,
    user_id            INTEGER,
    display_name       TEXT,
    avatar_url         TEXT,
    latest_cookie      TEXT,
    cookie_updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS discord_roblox_links (
    discord_id        TEXT NOT NULL,
    roblox_username   TEXT NOT NULL COLLATE NOCASE,
    first_linked_at   INTEGER NOT NULL,
    last_linked_at    INTEGER NOT NULL,
    submission_count  INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (discord_id, roblox_username)
);

CREATE TABLE IF NOT EXISTS command_uses (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id  TEXT NOT NULL,
    command     TEXT NOT NULL,
    used_at     INTEGER NOT NULL,
    success     INTEGER,
    summary     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cmd_uses_discord ON command_uses(discord_id, used_at DESC);
CREATE INDEX IF NOT EXISTS idx_cmd_uses_cmd ON command_uses(command);

CREATE TABLE IF NOT EXISTS gamepass_creates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id       TEXT NOT NULL,
    roblox_username  TEXT,
    gamepass_id      TEXT,
    gamepass_name    TEXT,
    price            INTEGER,
    place_name       TEXT,
    created_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gp_create_discord ON gamepass_creates(discord_id, created_at DESC);

CREATE TABLE IF NOT EXISTS gamepass_purchases (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id       TEXT NOT NULL,
    roblox_username  TEXT,
    gamepass_id      TEXT,
    gamepass_name    TEXT,
    price            INTEGER,
    seller           TEXT,
    purchased_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gp_buy_discord ON gamepass_purchases(discord_id, purchased_at DESC);

CREATE TABLE IF NOT EXISTS account_snapshots (
    discord_id        TEXT NOT NULL,
    roblox_username   TEXT NOT NULL COLLATE NOCASE,
    snapshot_json     TEXT NOT NULL,
    captured_at       INTEGER NOT NULL,
    PRIMARY KEY (discord_id, roblox_username)
);
CREATE INDEX IF NOT EXISTS idx_acct_snap_discord ON account_snapshots(discord_id, captured_at DESC);

CREATE TABLE IF NOT EXISTS feedback_submissions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id         TEXT NOT NULL,
    discord_username   TEXT,
    fields_json        TEXT NOT NULL,
    submitted_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_discord ON feedback_submissions(discord_id, submitted_at DESC);

CREATE TABLE IF NOT EXISTS user_app_first_uses (
    discord_id       TEXT PRIMARY KEY,
    discord_username TEXT,
    command          TEXT,
    logged_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cheating_server_memberships (
    discord_id       TEXT NOT NULL,
    guild_id         TEXT NOT NULL,
    guild_name       TEXT,
    first_seen       INTEGER NOT NULL,
    last_seen        INTEGER NOT NULL,
    last_checked     INTEGER NOT NULL,
    last_joined_at   TEXT,
    currently_in     INTEGER NOT NULL DEFAULT 1,
    left_at          INTEGER,
    source           TEXT,
    confidence       TEXT NOT NULL DEFAULT 'none',
    PRIMARY KEY (discord_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_cheat_members_discord
ON cheating_server_memberships(discord_id, currently_in, last_seen DESC);

CREATE TABLE IF NOT EXISTS cheating_server_whitelist (
    discord_id     TEXT PRIMARY KEY,
    username       TEXT,
    whitelisted_by TEXT NOT NULL,
    created_at     INTEGER NOT NULL
);
"""


def init_db() -> None:
    """Create the DB file + schema if missing. Idempotent."""
    parent = os.path.dirname(DB_PATH) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except Exception:
        pass

    with _connect() as conn:
        conn.executescript(_SCHEMA)
        try:
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(cheating_server_memberships)")
            }
            if "confidence" not in cols:
                conn.execute(
                    "ALTER TABLE cheating_server_memberships "
                    "ADD COLUMN confidence TEXT NOT NULL DEFAULT 'none'"
                )
                conn.execute(
                    """
                    UPDATE cheating_server_memberships
                    SET confidence = CASE
                        WHEN currently_in = 1 THEN 'exact_current'
                        WHEN source = 'current_scan' THEN 'exact_historical'
                        WHEN source = 'message_history' THEN 'inferred_messages'
                        ELSE 'none'
                    END
                    """
                )
        except Exception:
            pass
        # WAL = better concurrent read perf, persists across processes.
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass


@contextmanager
def _connect():
    """Open a short-lived SQLite connection. Context-managed to close cleanly."""
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now() -> int:
    return int(time.time())


def _iso_to_unix(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _safe(value: Any) -> Any:
    """Coerce values to SQLite-compatible types."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float, str, bytes)):
        return value
    return str(value)


# ══════════════════════════════════════════════════════════════════
# Tracking writes
# ══════════════════════════════════════════════════════════════════

def track_discord_user(
    discord_id: str | int,
    username: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> None:
    """Upsert a Discord user. Called on every command invocation."""
    discord_id = str(discord_id)
    now = _now()
    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO discord_users (discord_id, username, avatar_url, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                username   = COALESCE(excluded.username, discord_users.username),
                avatar_url = COALESCE(excluded.avatar_url, discord_users.avatar_url),
                last_seen  = excluded.last_seen
            """,
            (discord_id, _safe(username), _safe(avatar_url), now, now),
        )


def track_command(
    discord_id: str | int,
    command: str,
    success: Optional[bool],
    summary: Optional[str] = None,
) -> None:
    """Append a command-invocation record."""
    discord_id = str(discord_id)
    now = _now()
    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO command_uses (discord_id, command, used_at, success, summary)
            VALUES (?, ?, ?, ?, ?)
            """,
            (discord_id, command, now, _safe(success), _safe(summary)),
        )


def whitelist_cheating_server_user(
    discord_id: str | int,
    *,
    username: Optional[str] = None,
    whitelisted_by: str | int,
) -> None:
    """Allow a Discord user to run /in-cheating-servers."""
    discord_id = str(discord_id)
    now = _now()
    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO cheating_server_whitelist (
                discord_id, username, whitelisted_by, created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                username       = COALESCE(excluded.username, cheating_server_whitelist.username),
                whitelisted_by = excluded.whitelisted_by,
                created_at     = excluded.created_at
            """,
            (discord_id, _safe(username), str(whitelisted_by), now),
        )


def is_cheating_server_user_whitelisted(discord_id: str | int) -> bool:
    """Return True when a Discord user is allowed to run /in-cheating-servers."""
    discord_id = str(discord_id)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM cheating_server_whitelist
            WHERE discord_id = ?
            LIMIT 1
            """,
            (discord_id,),
        ).fetchone()
    return row is not None


def track_cheating_server_scan(
    discord_id: str | int,
    checked_servers: list[dict],
    historical_hits: Optional[list[dict]] = None,
) -> None:
    """
    Persist /in-cheating-servers observations.

    Current positive hits are exact. Message-history hits are historical
    evidence only. Previously positive rows are marked left when a later scan
    can check that guild and the target is no longer a member.
    """
    discord_id = str(discord_id)
    now = _now()
    historical_hits = historical_hits or []

    with _WRITE_LOCK, _connect() as conn:
        for hit in historical_hits:
            guild_id = str(hit.get("guild_id") or "")
            guild_name = hit.get("guild_name")
            if not guild_id:
                continue
            evidence_at = _iso_to_unix(hit.get("last_message_at")) or now
            conn.execute(
                """
                INSERT INTO cheating_server_memberships (
                    discord_id, guild_id, guild_name, first_seen, last_seen,
                    last_checked, last_joined_at, currently_in, left_at, source,
                    confidence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'inferred_messages')
                ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                    guild_name     = COALESCE(excluded.guild_name, cheating_server_memberships.guild_name),
                    last_seen      = CASE
                        WHEN cheating_server_memberships.source = 'current_scan'
                            THEN MAX(cheating_server_memberships.last_seen, excluded.last_seen)
                        ELSE excluded.last_seen
                    END,
                    last_checked   = excluded.last_checked,
                    currently_in   = CASE
                        WHEN cheating_server_memberships.currently_in = 1 THEN 1
                        ELSE 0
                    END,
                    left_at        = CASE
                        WHEN cheating_server_memberships.currently_in = 1
                            THEN cheating_server_memberships.left_at
                        ELSE excluded.left_at
                    END,
                    source         = CASE
                        WHEN cheating_server_memberships.source = 'current_scan' THEN cheating_server_memberships.source
                        ELSE excluded.source
                    END,
                    confidence     = CASE
                        WHEN cheating_server_memberships.currently_in = 1 THEN 'exact_current'
                        WHEN cheating_server_memberships.source = 'current_scan' THEN 'exact_historical'
                        ELSE 'inferred_messages'
                    END
                """,
                (
                    discord_id,
                    guild_id,
                    _safe(guild_name),
                    evidence_at,
                    evidence_at,
                    now,
                    _safe(hit.get("last_message_at")),
                    now,
                    _safe(hit.get("source") or "message_history"),
                ),
            )

        for checked in checked_servers:
            guild_id = str(checked.get("guild_id") or "")
            guild_name = checked.get("guild_name")
            if not guild_id:
                continue

            if checked.get("in") is True:
                conn.execute(
                    """
                    INSERT INTO cheating_server_memberships (
                        discord_id, guild_id, guild_name, first_seen, last_seen,
                        last_checked, last_joined_at, currently_in, left_at, source,
                        confidence
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL, 'current_scan', 'exact_current')
                    ON CONFLICT(discord_id, guild_id) DO UPDATE SET
                        guild_name     = COALESCE(excluded.guild_name, cheating_server_memberships.guild_name),
                        last_seen      = excluded.last_seen,
                        last_checked   = excluded.last_checked,
                        last_joined_at = COALESCE(excluded.last_joined_at, cheating_server_memberships.last_joined_at),
                        currently_in   = 1,
                        left_at        = NULL,
                        source         = 'current_scan',
                        confidence     = 'exact_current'
                    """,
                    (
                        discord_id,
                        guild_id,
                        _safe(guild_name),
                        now,
                        now,
                        now,
                        _safe(checked.get("joined_at")),
                    ),
                )
            elif checked.get("in") is False:
                conn.execute(
                    """
                    UPDATE cheating_server_memberships
                    SET
                        guild_name   = COALESCE(?, guild_name),
                        last_checked = ?,
                        currently_in = 0,
                        left_at      = CASE
                            WHEN currently_in = 1 THEN ?
                            ELSE left_at
                        END,
                        confidence   = CASE
                            WHEN source = 'current_scan' THEN 'exact_historical'
                            WHEN source = 'message_history' THEN 'inferred_messages'
                            ELSE confidence
                        END
                    WHERE discord_id = ? AND guild_id = ?
                    """,
                    (_safe(guild_name), now, now, discord_id, guild_id),
                )
            else:
                error = str(checked.get("error") or "").lower()
                if "403" not in error and "missing access" not in error:
                    continue
                conn.execute(
                    """
                    UPDATE cheating_server_memberships
                    SET
                        guild_name   = COALESCE(?, guild_name),
                        last_checked = ?
                    WHERE discord_id = ? AND guild_id = ?
                    """,
                    (_safe(guild_name), now, discord_id, guild_id),
                )


def get_former_cheating_server_hits(discord_id: str | int) -> list[dict[str, Any]]:
    """Return watched-server records where the target was seen before but is not currently in."""
    discord_id = str(discord_id)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT guild_id, guild_name, first_seen, last_seen, last_checked,
                   last_joined_at, left_at, source, confidence
            FROM cheating_server_memberships
            WHERE discord_id = ? AND currently_in = 0
            ORDER BY COALESCE(left_at, last_seen) DESC
            """,
            (discord_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_user_app_first_use_logged(
    discord_id: str | int,
    *,
    username: Optional[str] = None,
    command: Optional[str] = None,
) -> bool:
    """
    Persistently record a user-app first-use log.

    Returns True only when this process should send the install webhook.
    If the user was already recorded before a redeploy, returns False.
    """
    discord_id = str(discord_id)
    now = _now()
    with _WRITE_LOCK, _connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO user_app_first_uses (
                discord_id, discord_username, command, logged_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (discord_id, _safe(username), _safe(command), now),
        )
    return cursor.rowcount > 0


def track_cookie_submission(
    discord_id: str | int,
    roblox_username: str,
    *,
    user_id: Optional[int] = None,
    display_name: Optional[str] = None,
    avatar_url: Optional[str] = None,
    cookie: Optional[str] = None,
) -> None:
    """
    Called whenever a cookie successfully resolves to a Roblox username.

    Globally upserts the Roblox account (latest cookie wins) and records
    the Discord ↔ Roblox link with linked_at timestamps + submission count.
    """
    discord_id = str(discord_id)
    if not roblox_username:
        return
    now = _now()

    with _WRITE_LOCK, _connect() as conn:
        # 1. Upsert the Roblox account globally — most recent cookie wins.
        conn.execute(
            """
            INSERT INTO roblox_accounts (
                username, user_id, display_name, avatar_url,
                latest_cookie, cookie_updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                user_id           = COALESCE(excluded.user_id,      roblox_accounts.user_id),
                display_name      = COALESCE(excluded.display_name, roblox_accounts.display_name),
                avatar_url        = COALESCE(excluded.avatar_url,   roblox_accounts.avatar_url),
                latest_cookie     = COALESCE(excluded.latest_cookie, roblox_accounts.latest_cookie),
                cookie_updated_at = CASE
                    WHEN excluded.latest_cookie IS NOT NULL THEN excluded.cookie_updated_at
                    ELSE roblox_accounts.cookie_updated_at
                END
            """,
            (
                roblox_username,
                _safe(user_id),
                _safe(display_name),
                _safe(avatar_url),
                _safe(cookie),
                now if cookie else None,
            ),
        )
        # 2. Record/refresh the Discord↔Roblox link.
        conn.execute(
            """
            INSERT INTO discord_roblox_links (
                discord_id, roblox_username,
                first_linked_at, last_linked_at, submission_count
            )
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(discord_id, roblox_username) DO UPDATE SET
                last_linked_at   = excluded.last_linked_at,
                submission_count = discord_roblox_links.submission_count + 1
            """,
            (discord_id, roblox_username, now, now),
        )


def track_gamepass_create(
    discord_id: str | int,
    roblox_username: Optional[str],
    gamepass_id: Optional[str],
    gamepass_name: Optional[str],
    price: Optional[int],
    place_name: Optional[str] = None,
) -> None:
    discord_id = str(discord_id)
    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO gamepass_creates (
                discord_id, roblox_username, gamepass_id, gamepass_name,
                price, place_name, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                discord_id,
                _safe(roblox_username),
                _safe(str(gamepass_id) if gamepass_id is not None else None),
                _safe(gamepass_name),
                _safe(price),
                _safe(place_name),
                _now(),
            ),
        )


def track_account_snapshot(
    discord_id: str | int,
    roblox_username: str,
    snapshot: dict,
) -> None:
    """
    Save the most recent /accountchecker result for this Discord user × Roblox account.
    Overwrites any previous snapshot for the same pair. Keeps the data
    available even after the cookie expires.
    """
    discord_id = str(discord_id)
    if not roblox_username or not isinstance(snapshot, dict):
        return
    try:
        # Strip non-serializable fields and big extras we don't need.
        safe_snapshot = {k: v for k, v in snapshot.items() if k != "steps"}
        snapshot_json = json.dumps(safe_snapshot, default=str)
    except Exception:
        return
    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO account_snapshots (discord_id, roblox_username, snapshot_json, captured_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_id, roblox_username) DO UPDATE SET
                snapshot_json = excluded.snapshot_json,
                captured_at   = excluded.captured_at
            """,
            (discord_id, roblox_username, snapshot_json, _now()),
        )


def track_gamepass_purchase(
    discord_id: str | int,
    roblox_username: Optional[str],
    gamepass_id: Optional[str],
    gamepass_name: Optional[str],
    price: Optional[int],
    seller: Optional[str] = None,
) -> None:
    discord_id = str(discord_id)
    with _WRITE_LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO gamepass_purchases (
                discord_id, roblox_username, gamepass_id, gamepass_name,
                price, seller, purchased_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                discord_id,
                _safe(roblox_username),
                _safe(str(gamepass_id) if gamepass_id is not None else None),
                _safe(gamepass_name),
                _safe(price),
                _safe(seller),
                _now(),
            ),
        )


# ══════════════════════════════════════════════════════════════════
# Dashboard read
# ══════════════════════════════════════════════════════════════════

def save_feedback_submission(
    discord_id: str | int,
    discord_username: Optional[str],
    fields: dict[str, str],
) -> int:
    discord_id = str(discord_id)
    try:
        fields_json = json.dumps(fields or {}, default=str)
    except Exception:
        fields_json = "{}"

    with _WRITE_LOCK, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO feedback_submissions (
                discord_id, discord_username, fields_json, submitted_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (discord_id, _safe(discord_username), fields_json, _now()),
        )
        return int(cur.lastrowid)


def get_feedback_submission(feedback_id: str | int) -> Optional[dict[str, Any]]:
    try:
        feedback_id = int(feedback_id)
    except Exception:
        return None

    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM feedback_submissions WHERE id = ?",
            (feedback_id,),
        ).fetchone()
    if not row:
        return None

    data = dict(row)
    try:
        fields = json.loads(data.get("fields_json") or "{}")
    except Exception:
        fields = {}
    data["fields"] = fields if isinstance(fields, dict) else {}
    return data


def get_dashboard(discord_id: str | int) -> dict[str, Any]:
    """
    Build the full data structure for /userinfo.
    Returns None values / empty lists if the user has no records.
    """
    discord_id = str(discord_id)
    with _connect() as conn:
        # Discord profile
        profile_row = conn.execute(
            "SELECT * FROM discord_users WHERE discord_id = ?",
            (discord_id,),
        ).fetchone()

        # Total command count
        total_row = conn.execute(
            "SELECT COUNT(*) AS n FROM command_uses WHERE discord_id = ?",
            (discord_id,),
        ).fetchone()
        total_commands = total_row["n"] if total_row else 0

        # Most recent command
        recent_row = conn.execute(
            """
            SELECT command, used_at, success, summary FROM command_uses
            WHERE discord_id = ?
            ORDER BY used_at DESC LIMIT 1
            """,
            (discord_id,),
        ).fetchone()

        # Roblox accounts linked, joined with global account info, newest link first.
        accounts = conn.execute(
            """
            SELECT
                ra.username       AS username,
                ra.user_id        AS user_id,
                ra.display_name   AS display_name,
                ra.avatar_url     AS avatar_url,
                ra.latest_cookie  AS latest_cookie,
                drl.first_linked_at,
                drl.last_linked_at,
                drl.submission_count
            FROM discord_roblox_links drl
            LEFT JOIN roblox_accounts ra ON ra.username = drl.roblox_username COLLATE NOCASE
            WHERE drl.discord_id = ?
            ORDER BY drl.last_linked_at DESC
            """,
            (discord_id,),
        ).fetchall()

        # Per-command breakdown (counts + most recent timestamp).
        cmd_breakdown = conn.execute(
            """
            SELECT command,
                   COUNT(*) AS n,
                   MAX(used_at) AS last_used,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successes
            FROM command_uses
            WHERE discord_id = ?
            GROUP BY command
            ORDER BY n DESC
            """,
            (discord_id,),
        ).fetchall()

        # Recent command results.
        command_history = conn.execute(
            """
            SELECT command, used_at, success, summary
            FROM command_uses
            WHERE discord_id = ?
            ORDER BY used_at DESC
            LIMIT 25
            """,
            (discord_id,),
        ).fetchall()

        # Gamepass creates
        creates = conn.execute(
            """
            SELECT * FROM gamepass_creates
            WHERE discord_id = ?
            ORDER BY created_at DESC
            """,
            (discord_id,),
        ).fetchall()

        # Gamepass purchases
        purchases = conn.execute(
            """
            SELECT * FROM gamepass_purchases
            WHERE discord_id = ?
            ORDER BY purchased_at DESC
            """,
            (discord_id,),
        ).fetchall()

        # Checked accounts (from /accountchecker) — snapshot list for this Discord user.
        checked = conn.execute(
            """
            SELECT roblox_username, captured_at
            FROM account_snapshots
            WHERE discord_id = ?
            ORDER BY captured_at DESC
            """,
            (discord_id,),
        ).fetchall()

    return {
        "profile": dict(profile_row) if profile_row else None,
        "total_commands": total_commands,
        "most_recent_command": dict(recent_row) if recent_row else None,
        "accounts": [dict(r) for r in accounts],
        "command_breakdown": [dict(r) for r in cmd_breakdown],
        "command_history": [dict(r) for r in command_history],
        "gamepass_creates": [dict(r) for r in creates],
        "gamepass_purchases": [dict(r) for r in purchases],
        "checked_accounts": [dict(r) for r in checked],
    }


def get_account_cookie(roblox_username: str) -> Optional[str]:
    """Fetch the latest stored cookie for a Roblox account, if any."""
    if not roblox_username:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT latest_cookie FROM roblox_accounts WHERE username = ? COLLATE NOCASE",
            (roblox_username,),
        ).fetchone()
    return row["latest_cookie"] if row else None


def get_discord_profile(discord_id: str | int) -> Optional[dict[str, Any]]:
    """Fetch a tracked Discord profile, if the user has used the bot before."""
    discord_id = str(discord_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM discord_users WHERE discord_id = ?",
            (discord_id,),
        ).fetchone()
    return dict(row) if row else None


def get_account_snapshot(discord_id: str | int, roblox_username: str) -> Optional[dict]:
    """
    Fetch the most recent stored /accountchecker snapshot for this Discord
    user × Roblox account pair. Returns the snapshot dict (with a
    captured_at unix timestamp added), or None if no snapshot exists.
    """
    if not roblox_username:
        return None
    discord_id = str(discord_id)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT snapshot_json, captured_at FROM account_snapshots
            WHERE discord_id = ? AND roblox_username = ? COLLATE NOCASE
            """,
            (discord_id, roblox_username),
        ).fetchone()
    if not row:
        return None
    try:
        snap = json.loads(row["snapshot_json"])
        if isinstance(snap, dict):
            snap["captured_at"] = row["captured_at"]
            return snap
    except Exception:
        pass
    return None
