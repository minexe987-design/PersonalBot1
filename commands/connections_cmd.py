import asyncio
import io
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import discord
import discord
from discord.ext import commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from core.tracking import track_command, track_discord_user
from core.utils import make_roblox_session, roblox_get, roblox_post, sanitize_cookie


ROBLOX_USER_LOOKUP_URL = "https://users.roblox.com/v1/usernames/users"
ROBLOX_USERS_BY_IDS_URL = "https://users.roblox.com/v1/users"
ROBLOX_FRIENDS_URL = "https://friends.roblox.com/v1/users/{user_id}/friends"
ROBLOX_FRIENDS_FIND_URL = "https://friends.roblox.com/v1/users/{user_id}/friends/find"
ROBLOX_FRIENDS_COUNT_URL = "https://friends.roblox.com/v1/users/{user_id}/friends/count"
ROBLOX_ARE_FRIENDS_URL = "https://friends.roblox.com/v1/user/{user_id}/multiget-are-friends"
ROBLOX_THUMBNAILS_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"
FRIEND_CHECK_COOKIE_ENV = "ROBLOX_FRIEND_CHECK_COOKIE"

PAGE_SIZE = 10
CACHE_TTL_SECONDS = 600
FRIEND_IMAGE_WIDTH = 960
FRIEND_AVATAR_SIZE = 108

BG = (24, 25, 31)
PANEL = (33, 35, 43)
TEXT = (244, 245, 247)
MUTED = (174, 179, 188)
GREEN = (55, 205, 109)
RED = (239, 83, 80)
AMBER = (245, 181, 72)
LINE = (88, 95, 110)
AVATAR_BG = (43, 45, 54)

_cache_lock = threading.Lock()
_user_cache: dict[str, tuple[float, "RobloxUser"]] = {}
_user_id_cache: dict[int, tuple[float, "RobloxUser"]] = {}
_friends_cache: dict[int, tuple[float, list["RobloxUser"]]] = {}
_friend_count_cache: dict[int, tuple[float, int]] = {}
_avatar_cache: dict[int, tuple[float, bytes]] = {}
_friend_check_cookie_lock = threading.Lock()
_friend_check_cookie_value: Optional[str] = None


class RobloxApiError(RuntimeError):
    def __init__(self, status_code: int, url: str, message: str = ""):
        self.status_code = status_code
        self.url = url
        detail = message or f"Roblox API returned HTTP {status_code}."
        super().__init__(detail)


@dataclass(frozen=True)
class RobloxUser:
    user_id: int
    username: str
    display_name: str


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "arialbd.ttf" if bold else "arial.ttf",
        "segoeuib.ttf" if bold else "segoeui.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


FONT_TITLE = _font(36, bold=True)
FONT_NAME = _font(27, bold=True)
FONT_BODY = _font(23)
FONT_SMALL = _font(20)


def _cache_get(cache: dict, key):
    now = time.time()
    with _cache_lock:
        item = cache.get(key)
        if not item:
            return None
        fetched_at, value = item
        if now - fetched_at > CACHE_TTL_SECONDS:
            cache.pop(key, None)
            return None
        return value


def _cache_set(cache: dict, key, value) -> None:
    with _cache_lock:
        cache[key] = (time.time(), value)


def _request_json(method: str, url: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", "LogsBot/1.0")
    for attempt in range(3):
        if method.upper() == "POST":
            response = roblox_post(url, headers=headers, timeout=15, **kwargs)
        else:
            response = roblox_get(url, headers=headers, timeout=15, **kwargs)

        if response.status_code != 429:
            if response.status_code >= 400:
                raise RobloxApiError(response.status_code, url)
            return response.json()

        if attempt == 2:
            raise RobloxApiError(429, url, "Roblox is rate-limiting requests right now. Try again shortly.")
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 1.5 + attempt
        except Exception:
            delay = 1.5 + attempt
        time.sleep(min(max(delay, 0.5), 5.0))

    raise RuntimeError("Roblox API request failed.")


def _resolve_users(usernames: list[str]) -> dict[str, RobloxUser]:
    wanted = []
    seen = set()
    results: dict[str, RobloxUser] = {}

    for username in usernames:
        cleaned = username.strip()
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        cached = _cache_get(_user_cache, key)
        if cached:
            results[key] = cached
        else:
            wanted.append(cleaned)

    if wanted:
        data = _request_json(
            "POST",
            ROBLOX_USER_LOOKUP_URL,
            json={"usernames": wanted, "excludeBannedUsers": False},
        )
        for item in data.get("data", []):
            user = RobloxUser(
                user_id=int(item["id"]),
                username=str(item.get("name") or item["id"]),
                display_name=str(item.get("displayName") or item.get("name") or item["id"]),
            )
            keys = {
                user.username.lower(),
                str(item.get("requestedUsername") or "").lower(),
            }
            for key in keys:
                if key:
                    _cache_set(_user_cache, key, user)
                    results[key] = user
            _cache_set(_user_id_cache, user.user_id, user)

    return results


def _resolve_user_ids(user_ids: list[int]) -> dict[int, RobloxUser]:
    wanted = []
    seen = set()
    results: dict[int, RobloxUser] = {}

    for user_id in user_ids:
        if user_id in seen:
            continue
        seen.add(user_id)
        cached = _cache_get(_user_id_cache, user_id)
        if cached:
            results[user_id] = cached
        else:
            wanted.append(user_id)

    for start in range(0, len(wanted), 100):
        chunk = wanted[start:start + 100]
        if not chunk:
            continue
        try:
            data = _request_json(
                "POST",
                ROBLOX_USERS_BY_IDS_URL,
                json={"userIds": chunk, "excludeBannedUsers": False},
            )
        except RobloxApiError:
            continue
        for item in data.get("data", []):
            user = RobloxUser(
                user_id=int(item["id"]),
                username=str(item.get("name") or item["id"]),
                display_name=str(item.get("displayName") or item.get("name") or item["id"]),
            )
            results[user.user_id] = user
            _cache_set(_user_id_cache, user.user_id, user)
            _cache_set(_user_cache, user.username.lower(), user)

    return results


def _resolve_user(username: str) -> RobloxUser:
    users = _resolve_users([username])
    user = users.get(username.strip().lower())
    if not user:
        raise ValueError(f"Roblox user not found: {username}")
    return user


def _fetch_friends(user_id: int) -> list[RobloxUser]:
    cached = _cache_get(_friends_cache, user_id)
    if cached is not None:
        return cached

    friend_ids: list[int] = []
    unavailable_count = 0
    try:
        cursor = None
        for _ in range(25):
            params = {}
            if cursor:
                params["cursor"] = cursor
            data = _request_json("GET", ROBLOX_FRIENDS_FIND_URL.format(user_id=user_id), params=params)
            items = data.get("PageItems") or data.get("pageItems") or []
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                friend_id = int(item["id"])
                if friend_id <= 0:
                    unavailable_count += 1
                elif friend_id not in friend_ids:
                    friend_ids.append(friend_id)
            cursor = data.get("NextCursor") or data.get("nextPageCursor") or data.get("nextCursor")
            if not cursor:
                break
    except RobloxApiError as exc:
        if exc.status_code == 403:
            raise ValueError(f"Roblox user {user_id}'s friends list is private or unavailable.") from exc
        raise

    if friend_ids or unavailable_count:
        hydrated = _resolve_user_ids(friend_ids)
        friends = [
            hydrated.get(friend_id, RobloxUser(user_id=friend_id, username=str(friend_id), display_name=str(friend_id)))
            for friend_id in friend_ids
        ]
        friends.extend(
            RobloxUser(user_id=-1, username="Unavailable friend", display_name="Private/deleted user")
            for _ in range(unavailable_count)
        )
        _cache_set(_friends_cache, user_id, friends)
        return friends

    data = _request_json("GET", ROBLOX_FRIENDS_URL.format(user_id=user_id))
    friends = []
    for item in data.get("data", []):
        if not item.get("id"):
            continue
        friend_id = int(item["id"])
        if friend_id <= 0:
            continue
        name = str(item.get("name") or "")
        display_name = str(item.get("displayName") or "")
        if name:
            user = RobloxUser(
                user_id=friend_id,
                username=name,
                display_name=display_name or name,
            )
            friends.append(user)
            _cache_set(_user_id_cache, user.user_id, user)
            _cache_set(_user_cache, user.username.lower(), user)
        else:
            friends.append(RobloxUser(user_id=friend_id, username=str(friend_id), display_name=str(friend_id)))

    _cache_set(_friends_cache, user_id, friends)
    return friends


def _fetch_friends_safe(user_id: int) -> list[RobloxUser]:
    try:
        return _fetch_friends(user_id)
    except Exception:
        return []


def _fetch_friend_ids_optional(user_id: int) -> Optional[set[int]]:
    try:
        return {friend.user_id for friend in _fetch_friends(user_id)}
    except Exception:
        return None


def _fetch_friend_count_optional(user_id: int) -> Optional[int]:
    cached = _cache_get(_friend_count_cache, user_id)
    if cached is not None:
        return cached

    try:
        data = _request_json("GET", ROBLOX_FRIENDS_COUNT_URL.format(user_id=user_id))
        count = int(data.get("count", 0))
    except Exception:
        return None

    _cache_set(_friend_count_cache, user_id, count)
    return count


def _fetch_friend_ids_state(user_id: int) -> tuple[Optional[set[int]], bool]:
    try:
        friends = _fetch_friends(user_id)
    except Exception:
        return None, False

    count = _fetch_friend_count_optional(user_id)
    is_capped = count is not None and count > len(friends)
    return {friend.user_id for friend in friends}, is_capped


def _get_friend_check_cookie() -> str:
    global _friend_check_cookie_value
    with _friend_check_cookie_lock:
        if _friend_check_cookie_value is None:
            value = os.getenv(FRIEND_CHECK_COOKIE_ENV, "")
            if not value:
                for key, candidate in os.environ.items():
                    if key.strip() == FRIEND_CHECK_COOKIE_ENV:
                        value = candidate
                        break
            _friend_check_cookie_value = sanitize_cookie(value)
        return _friend_check_cookie_value


def _set_friend_check_cookie(cookie: str) -> None:
    global _friend_check_cookie_value
    if not cookie:
        return
    with _friend_check_cookie_lock:
        _friend_check_cookie_value = cookie


def _authenticated_connection_status(left_id: int, right_id: int) -> Optional[bool]:
    cookie = _get_friend_check_cookie()
    if not cookie:
        return None

    session = make_roblox_session(cookie)
    url = ROBLOX_ARE_FRIENDS_URL.format(user_id=left_id)
    payload = {"targetUserIds": [right_id]}

    try:
        response = session.post(url, json=payload, timeout=15)
        if response.status_code == 403 and response.headers.get("x-csrf-token"):
            session.headers["x-csrf-token"] = response.headers["x-csrf-token"]
            response = session.post(url, json=payload, timeout=15)

        fresh_cookie = session.cookies.get(".ROBLOSECURITY")
        if fresh_cookie and fresh_cookie != cookie:
            _set_friend_check_cookie(fresh_cookie)

        if response.status_code >= 400:
            return None

        data = response.json()
    except Exception:
        return None

    friend_ids = data.get("friendsId")
    if not isinstance(friend_ids, list):
        return None

    return right_id in {int(friend_id) for friend_id in friend_ids if str(friend_id).isdigit()}


def _hydrate_page_users(users: list[RobloxUser]) -> list[RobloxUser]:
    needs_lookup = [
        user.user_id
        for user in users
        if user.user_id > 0 and user.username == str(user.user_id)
    ]
    if not needs_lookup:
        return users

    hydrated = _resolve_user_ids(needs_lookup)
    return [hydrated.get(user.user_id, user) for user in users]


def _is_friend(user_id: int, other_id: int) -> bool:
    return any(friend.user_id == other_id for friend in _fetch_friends(user_id))


def _connection_status(left_id: int, right_id: int) -> Optional[bool]:
    auth_status = _authenticated_connection_status(left_id, right_id)
    if auth_status is not None:
        return auth_status

    auth_status = _authenticated_connection_status(right_id, left_id)
    if auth_status is not None:
        return auth_status

    left_friend_ids, left_capped = _fetch_friend_ids_state(left_id)
    if left_friend_ids is not None and right_id in left_friend_ids:
        return True

    right_friend_ids, right_capped = _fetch_friend_ids_state(right_id)
    if right_friend_ids is not None and left_id in right_friend_ids:
        return True

    if left_friend_ids is None or right_friend_ids is None:
        return None

    if left_capped or right_capped:
        return None

    return False


def _avatar_bytes(user_id: int) -> Optional[bytes]:
    cached = _cache_get(_avatar_cache, user_id)
    if cached is not None:
        return cached

    data = _request_json(
        "GET",
        ROBLOX_THUMBNAILS_URL,
        params={
            "userIds": str(user_id),
            "size": "150x150",
            "format": "Png",
            "isCircular": "false",
        },
    )
    image_url = None
    items = data.get("data") or []
    if items:
        image_url = items[0].get("imageUrl")
    if not image_url:
        return None

    response = roblox_get(image_url, timeout=15, headers={"User-Agent": "LogsBot/1.0"})
    if response.status_code >= 400:
        return None
    content = response.content
    _cache_set(_avatar_cache, user_id, content)
    return content


def _circle_avatar(user: RobloxUser, size: int) -> Image.Image:
    avatar = Image.new("RGBA", (size, size), AVATAR_BG + (255,))
    try:
        content = _avatar_bytes(user.user_id)
        if content:
            raw = Image.open(io.BytesIO(content)).convert("RGBA")
            raw.thumbnail((size, size), Image.Resampling.LANCZOS)
            avatar.paste(raw, ((size - raw.width) // 2, (size - raw.height) // 2), raw)
    except Exception:
        pass

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(avatar, (0, 0), mask)

    border = ImageDraw.Draw(out)
    border.ellipse((1, 1, size - 2, size - 2), outline=(64, 68, 79, 255), width=2)
    return out


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "..."
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return (text + ellipsis) if text else ellipsis


def _draw_user(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    user: RobloxUser,
    x: int,
    y: int,
    *,
    align: str,
    size: int = 82,
    max_width: int = 170,
) -> None:
    image.alpha_composite(_circle_avatar(user, size), (x, y))
    display = _fit_text(draw, user.display_name, FONT_NAME, max_width)
    name = _fit_text(draw, user.username, FONT_SMALL, max_width)
    text_x = x + size + 14 if align == "left" else x - 14
    if align == "right":
        display_w = draw.textlength(display, font=FONT_NAME)
        name_w = draw.textlength(name, font=FONT_SMALL)
        draw.text((text_x - display_w, y + 18), display, font=FONT_NAME, fill=TEXT)
        draw.text((text_x - name_w, y + 46), name, font=FONT_SMALL, fill=MUTED)
    else:
        draw.text((text_x, y + 18), display, font=FONT_NAME, fill=TEXT)
        draw.text((text_x, y + 46), name, font=FONT_SMALL, fill=MUTED)


def _draw_arrow(draw: ImageDraw.ImageDraw, x1: int, y: int, x2: int, *, ok: Optional[bool], label: str) -> None:
    color = GREEN if ok is True else RED if ok is False else AMBER
    draw.line((x1, y, x2, y), fill=LINE, width=3)
    draw.polygon([(x2, y), (x2 - 13, y - 8), (x2 - 13, y + 8)], fill=LINE)
    label_w = draw.textlength(label, font=FONT_BODY)
    draw.rounded_rectangle(
        (x1 + (x2 - x1 - label_w) / 2 - 18, y - 34, x1 + (x2 - x1 + label_w) / 2 + 18, y - 6),
        radius=10,
        fill=PANEL,
        outline=color,
        width=2,
    )
    draw.text((x1 + (x2 - x1 - label_w) / 2, y - 31), label, font=FONT_BODY, fill=color)


def _image_bytes(image: Image.Image) -> io.BytesIO:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


def _connections_file(image: io.BytesIO, *, page: int = 0) -> discord.File:
    image.seek(0)
    return discord.File(image, filename=f"roblox-connections-page-{page + 1}.png")


def _render_friend_page(main_user: RobloxUser, friends: list[RobloxUser], page: int) -> io.BytesIO:
    total_pages = max(1, math.ceil(len(friends) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    shown = _hydrate_page_users(friends[page * PAGE_SIZE:(page + 1) * PAGE_SIZE])

    width = FRIEND_IMAGE_WIDTH
    row_h = 138
    height = 190 + max(1, len(shown)) * row_h
    image = Image.new("RGBA", (width, height), BG + (255,))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((26, 26, width - 26, height - 26), radius=20, fill=PANEL)
    draw.text((64, 48), f"{main_user.username}'s friends", font=FONT_TITLE, fill=TEXT)
    draw.text((64, 96), f"{len(friends)} total friends - page {page + 1}/{total_pages}", font=FONT_BODY, fill=MUTED)

    if not shown:
        draw.text((64, 160), "No friends found.", font=FONT_BODY, fill=MUTED)
    for index, friend in enumerate(shown, start=page * PAGE_SIZE + 1):
        row_y = 150 + (index - page * PAGE_SIZE - 1) * row_h
        draw.text((64, row_y + 40), f"{index}.", font=FONT_NAME, fill=MUTED)
        _draw_user(
            draw,
            image,
            friend,
            128,
            row_y,
            align="left",
            size=FRIEND_AVATAR_SIZE,
            max_width=620,
        )

    return _image_bytes(image)


def _render_friend_unavailable_page(main_user: RobloxUser) -> io.BytesIO:
    width = FRIEND_IMAGE_WIDTH
    height = 330
    image = Image.new("RGBA", (width, height), BG + (255,))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((26, 26, width - 26, height - 26), radius=20, fill=PANEL)
    draw.text((64, 48), f"{main_user.username}'s friends", font=FONT_TITLE, fill=TEXT)
    draw.text((64, 96), "Friend list unavailable", font=FONT_BODY, fill=AMBER)
    draw.text((64, 136), "Roblox did not expose this user's full friend list.", font=FONT_BODY, fill=MUTED)
    draw.text((64, 172), "Add another username to check a direct connection.", font=FONT_BODY, fill=MUTED)
    _draw_user(
        draw,
        image,
        main_user,
        64,
        222,
        align="left",
        size=70,
        max_width=640,
    )
    return _image_bytes(image)


def _render_graph(main_user: RobloxUser, others: list[RobloxUser], main_edges: list[tuple[RobloxUser, Optional[bool]]], peer_edges: list[tuple[RobloxUser, RobloxUser]]) -> io.BytesIO:
    row_h = 132
    peer_h = max(0, len(peer_edges)) * 118
    width = 1180
    height = 170 + len(main_edges) * row_h + (120 if peer_edges else 0) + peer_h
    image = Image.new("RGBA", (width, height), BG + (255,))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, width - 24, height - 24), radius=18, fill=PANEL)
    draw.text((52, 44), "Roblox connections", font=FONT_TITLE, fill=TEXT)
    draw.text((52, 80), f"Main user: {main_user.username}", font=FONT_BODY, fill=MUTED)

    y = 132
    for other, ok in main_edges:
        _draw_user(draw, image, main_user, 62, y, align="left")
        _draw_user(draw, image, other, width - 270, y, align="left")
        label = "friended" if ok is True else "not friended" if ok is False else "unknown"
        _draw_arrow(draw, 382, y + 42, width - 318, ok=ok, label=label)
        y += row_h

    if peer_edges:
        draw.line((52, y + 14, width - 52, y + 14), fill=LINE, width=1)
        draw.text((52, y + 44), "Connections between added users", font=FONT_NAME, fill=TEXT)
        y += 92
        for left, right in peer_edges:
            _draw_user(draw, image, left, 62, y, align="left")
            _draw_user(draw, image, right, width - 270, y, align="left")
            _draw_arrow(draw, 382, y + 42, width - 318, ok=True, label="friended")
            y += 118

    if len(others) > 1 and not peer_edges:
        draw.text((52, y + 10), "No friend connections found among the added users.", font=FONT_BODY, fill=MUTED)

    return _image_bytes(image)


def _build_friend_page(username: str, page: int) -> tuple[RobloxUser, list[RobloxUser], io.BytesIO]:
    main_user = _resolve_user(username)
    try:
        friends = _fetch_friends(main_user.user_id)
    except ValueError:
        return main_user, [], _render_friend_unavailable_page(main_user)
    return main_user, friends, _render_friend_page(main_user, friends, page)


def _build_graph(main_username: str, other_usernames: list[str]) -> tuple[RobloxUser, list[RobloxUser], list[str], io.BytesIO]:
    all_users = _resolve_users([main_username, *other_usernames])
    main_user = all_users.get(main_username.strip().lower())
    if not main_user:
        raise ValueError(f"Roblox user not found: {main_username}")

    others: list[RobloxUser] = []
    invalid: list[str] = []
    for username in other_usernames:
        user = all_users.get(username.strip().lower())
        if not user:
            invalid.append(username)
            continue
        if user.user_id != main_user.user_id and user.user_id not in {u.user_id for u in others}:
            others.append(user)

    if not others:
        invalid_message = ", ".join(invalid) if invalid else "No valid compare users."
        raise ValueError(f"No valid compare users found. Invalid: {invalid_message}")

    main_edges = [
        (other, _connection_status(main_user.user_id, other.user_id))
        for other in others
    ]

    peer_edges: list[tuple[RobloxUser, RobloxUser]] = []
    for i, left in enumerate(others):
        for right in others[i + 1:]:
            if _connection_status(left.user_id, right.user_id) is True:
                peer_edges.append((left, right))

    return main_user, others, invalid, _render_graph(main_user, others, main_edges, peer_edges)


def _split_extra_users(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    pieces = raw.replace(",", " ").replace(";", " ").split()
    return [piece.strip() for piece in pieces if piece.strip()]


def _collect_other_users(values: list[Optional[str]]) -> list[str]:
    others: list[str] = []
    seen: set[str] = set()
    for value in values:
        for username in _split_extra_users(value):
            key = username.lower()
            if key not in seen:
                seen.add(key)
                others.append(username)
    return others


class FriendListView(discord.ui.View):
    def __init__(self, *, owner_id: int, username: str, main_user: RobloxUser, friends: list[RobloxUser], page: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.username = username
        self.main_user = main_user
        self.friends = friends
        self.page = page
        self.total_pages = max(1, math.ceil(len(friends) / PAGE_SIZE))
        self._busy = False
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous.disabled = self._busy or self.page <= 0
        self.next.disabled = self._busy or self.page >= self.total_pages - 1

    async def _turn_page(self, interaction: discord.Interaction, delta: int) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.respond("Only the command user can turn these pages.", ephemeral=True)
            return

        if self._busy:
            await interaction.response.defer()
            return

        next_page = max(0, min(self.page + delta, self.total_pages - 1))
        if next_page == self.page:
            await interaction.response.defer()
            return

        self._busy = True
        self._sync_buttons()
        await interaction.response.defer()

        try:
            image = await asyncio.to_thread(_render_friend_page, self.main_user, self.friends, next_page)
        except Exception:
            self._busy = False
            self._sync_buttons()
            await interaction.edit_original_response(view=self)
            raise

        self.page = next_page
        self._busy = False
        self._sync_buttons()
        await interaction.edit_original_response(file=_connections_file(image, page=self.page), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="connections:previous")
    async def previous(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn_page(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="connections:next")
    async def next(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._turn_page(interaction, 1)


class ExpiredConnectionsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _expired(self, interaction: discord.Interaction):
        await interaction.respond(
            "This friends page expired during a bot restart. Run `/connections` again to rebuild it.",
            ephemeral=True,
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, custom_id="connections:previous")
    async def previous(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, custom_id="connections:next")
    async def next(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._expired(interaction)


class ConnectionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @discord.slash_command(name="connections", description="Show Roblox friends or a connection graph between users.",
        contexts={discord.InteractionContextType.guild, discord.InteractionContextType.bot_dm, discord.InteractionContextType.private_channel},
        integration_types={discord.IntegrationType.guild_install, discord.IntegrationType.user_install}
    )
    @discord.option("main_user", description="Main Roblox username")
    @discord.option("users", description="Optional: up to 4 usernames separated by spaces or commas")
    @discord.option("user_b", description="Optional Roblox username to compare")
    @discord.option("user_c", description="Optional Roblox username to compare")
    @discord.option("user_d", description="Optional Roblox username to compare")
    @discord.option("user_e", description="Optional Roblox username to compare")
    async def connections(
        self,
        ctx: discord.ApplicationContext,
        main_user: str,
        users: Optional[str] = None,
        user_b: Optional[str] = None,
        user_c: Optional[str] = None,
        user_d: Optional[str] = None,
        user_e: Optional[str] = None,
    ):
        await ctx.defer()

        from core.logging import log_command, log_inputs, log_result, log_user_first_use
        log_user_first_use(ctx, "connections")
        log_command(ctx, "connections")
        log_inputs(ctx, "connections", {
            "main_user": main_user,
            "users": users,
            "user_b": user_b,
            "user_c": user_c,
            "user_d": user_d,
            "user_e": user_e,
        })

        track_discord_user(
            ctx.author.id,
            username=str(ctx.author),
            avatar_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
        )

        others = _collect_other_users([users, user_b, user_c, user_d, user_e])
        if len(others) > 4:
            await ctx.respond("Use at most 4 usernames to compare.", ephemeral=True)
            log_result(ctx, "connections", False, "Too many compare users")
            track_command(ctx.author.id, "connections", success=False, summary="too many compare users")
            return

        try:
            if others:
                resolved_main, resolved_others, invalid_users, image = await asyncio.to_thread(_build_graph, main_user, others)
                file = _connections_file(image)
                content = None
                if invalid_users:
                    skipped = ", ".join(f"`{username}`" for username in invalid_users)
                    content = f"Skipped invalid Roblox user(s): {skipped}"
                await ctx.respond(content=content, file=file)
                summary = f"{resolved_main.username} graph with {len(resolved_others)} user(s)"
            else:
                resolved_main, friends, image = await asyncio.to_thread(_build_friend_page, main_user, 0)
                view = FriendListView(
                    owner_id=ctx.author.id,
                    username=resolved_main.username,
                    main_user=resolved_main,
                    friends=friends,
                    page=0,
                )
                file = _connections_file(image)
                await ctx.respond(file=file, view=view if view.total_pages > 1 else None)
                summary = f"{resolved_main.username} friend list - {len(friends)} friend(s)"

            log_result(ctx, "connections", True, summary)
            track_command(ctx.author.id, "connections", success=True, summary=summary)
        except Exception as exc:
            message = str(exc) or "Could not build Roblox connections."
            await ctx.respond(f"Could not build Roblox connections: {message}", ephemeral=True)
            log_result(ctx, "connections", False, message)
            track_command(ctx.author.id, "connections", success=False, summary=message)


def setup(bot):
    bot.add_view(ExpiredConnectionsView())
    bot.add_cog(ConnectionsCog(bot))
