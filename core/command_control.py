import json
import os
import threading
from typing import Any

# Primary owner account (current). Legacy + co-owner IDs stay in DEFAULT_OWNER_IDS.
OWNER_ID = 1338186029194154087

DEFAULT_OWNER_IDS = {
    OWNER_ID,
    1331949475467493448,
    930861591350624286,
}

_OWNER_IDS: set[int] | None = None

CONTROL_PATH = os.environ.get(
    "COMMAND_CONTROL_PATH",
    "/data/command_control.json" if os.name != "nt" else "command_control.json",
)

OWNER_COMMANDS = {
    "owner-commanddisabler",
    "owner-dmuser",
}

_LOCK = threading.Lock()
_STATE: dict[str, Any] | None = None


def _default_state() -> dict[str, Any]:
    return {"disabled_commands": {}}


def _load() -> dict[str, Any]:
    global _STATE
    if _STATE is not None:
        return _STATE

    try:
        with open(CONTROL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = _default_state()
    except Exception:
        data = _default_state()

    disabled = data.get("disabled_commands")
    if not isinstance(disabled, dict):
        data["disabled_commands"] = {}

    _STATE = data
    return _STATE


def _save(state: dict[str, Any]) -> None:
    parent = os.path.dirname(CONTROL_PATH) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_path = CONTROL_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, CONTROL_PATH)


def normalize_command_name(name: str) -> str:
    return (name or "").strip().lstrip("/").lower()


def _load_owner_ids() -> set[int]:
    global _OWNER_IDS
    if _OWNER_IDS is not None:
        return _OWNER_IDS

    raw = os.environ.get("BOT_OWNER_IDS", "")
    if not raw.strip():
        _OWNER_IDS = set(DEFAULT_OWNER_IDS)
        return _OWNER_IDS

    parsed: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            parsed.add(int(chunk))
    _OWNER_IDS = parsed or set(DEFAULT_OWNER_IDS)
    return _OWNER_IDS


def is_owner(user_id: int | str) -> bool:
    try:
        return int(user_id) in _load_owner_ids()
    except Exception:
        return False


def is_command_disabled(command_name: str) -> bool:
    command_name = normalize_command_name(command_name)
    if command_name in OWNER_COMMANDS:
        return False
    with _LOCK:
        state = _load()
        return command_name in state["disabled_commands"]


def set_command_disabled(command_name: str, disabled: bool, *, changed_by: int | str) -> tuple[bool, str]:
    command_name = normalize_command_name(command_name)
    if not command_name:
        return False, "Command name is required."
    if command_name in OWNER_COMMANDS:
        return False, "Owner control commands cannot be disabled."

    with _LOCK:
        state = _load()
        disabled_commands = state["disabled_commands"]
        if disabled:
            disabled_commands[command_name] = {
                "changed_by": str(changed_by),
            }
        else:
            disabled_commands.pop(command_name, None)
        _save(state)

    return True, command_name


def list_disabled_commands() -> list[str]:
    with _LOCK:
        state = _load()
        return sorted(state["disabled_commands"].keys())
