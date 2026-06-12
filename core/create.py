"""
Personal Roblox Gamepass Creation Utility.

Uses Roblox's current universe-scoped Game Pass APIs.
"""

import io
import struct
import time
import zlib

from curl_cffi import CurlMime

from core.utils import make_roblox_session, record_rotated_cookie, roblox_get, sanitize_cookie


def create_gamepass(cookie: str, price: int, name: str = None, place_id: str = None) -> dict:
    """
    Creates a new gamepass on the authenticated user's Roblox game.

    Workflow:
        1. Validate session token and identify the owner
        2. Load the owner's published games (or use a specific place ID)
        3. Obtain CSRF token for write operations
        4. Create the gamepass with the new universe-scoped API
        5. Patch sale settings if Roblox did not apply them on create
        6. Verify the public listing without sending the cookie again

    Returns a dict with:
        success (bool), steps (list of str), error (str or None),
        gamepass_id (int or None), gamepass_name (str or None),
        price (int or None), place_name (str or None), games (list or None)
    """
    result = {
        "success": False,
        "steps": [],
        "error": None,
        "gamepass_id": None,
        "gamepass_name": None,
        "price": None,
        "place_name": None,
        "games": None,
        "owner_username": None,
        "owner_user_id": None,
        "cookie_was_rotated": False,
        "rotated_cookie": None,
    }

    cookie = sanitize_cookie(cookie)

    if not cookie:
        result["error"] = "No cookie provided."
        return result
    if price < 1:
        result["error"] = "Price must be at least 1 Robux."
        return result

    session = make_roblox_session(cookie)

    def _return_result():
        record_rotated_cookie(
            result,
            session,
            cookie,
            "<:warning:1497344059017003079> Roblox rotated this cookie during gamepass creation. Use the fresh cookie sent privately.",
        )
        return result

    try:
        auth_resp = session.get("https://users.roblox.com/v1/users/authenticated", timeout=10)
        if auth_resp.status_code == 200:
            owner = auth_resp.json()
            owner_id = owner.get("id")
            owner_name = owner.get("name")
            result["owner_username"] = owner_name
            result["owner_user_id"] = owner_id
            result["steps"].append(f"<:check:1497344035696672959> Owner: `{owner_name}` (ID: `{owner_id}`)")
        else:
            result["error"] = f"Cookie is invalid (HTTP {auth_resp.status_code})"
            return _return_result()
    except Exception as e:
        result["error"] = f"Failed to verify cookie: {e}"
        return _return_result()

    universe_id = None

    if place_id:
        place_id = place_id.strip()
        try:
            uni_resp = session.get(
                f"https://apis.roblox.com/universes/v1/places/{place_id}/universe",
                timeout=10,
            )
            if uni_resp.status_code == 200:
                universe_id = uni_resp.json().get("universeId")
                result["place_name"] = f"Place {place_id}"
                result["steps"].append(
                    f"<:check:1497344035696672959> Using place `{place_id}` (Universe: `{universe_id}`)"
                )
            else:
                result["error"] = f"Could not resolve place ID {place_id} (HTTP {uni_resp.status_code})"
                return _return_result()
        except Exception as e:
            result["error"] = f"Failed to resolve place: {e}"
            return _return_result()
    else:
        # Auto-pick a game. We try multiple endpoints because:
        #   - games.roblox.com/v2/users/{id}/games only returns publicly
        #     visible games — default/unpublished places don't appear.
        #   - apis.roblox.com/universes/v1/search (CreatorType=User) is what
        #     the create.roblox.com dashboard uses — includes unpublished.
        #   - groups.roblox.com fallback covers group-owned games.
        # Each attempt logs its status + count so failures are diagnosable.
        usable_games = []

        def _collect_games(label, url, place_field="rootPlaceId"):
            try:
                r = session.get(url, timeout=10)
                added = 0
                if r.status_code == 200:
                    for game in r.json().get("data", []):
                        uni = game.get("id")
                        # apis.roblox.com/universes/v1/search returns
                        # rootPlaceId as a flat field; if a future endpoint
                        # nests it under rootPlace, fall back to that.
                        place = game.get(place_field)
                        if not place:
                            rp = game.get("rootPlace")
                            if isinstance(rp, dict):
                                place = rp.get("id")
                        if uni and place:
                            usable_games.append({
                                "universeId": uni,
                                "placeId": place,
                                "name": game.get("name") or "Unknown",
                            })
                            added += 1
                result["steps"].append(
                    f"<:clipboard:1497344037294702762> {label}: HTTP {r.status_code} — {added} usable game(s)"
                )
            except Exception as e:
                result["steps"].append(
                    f"<:warning:1497344059017003079> {label}: error {e}"
                )

        _collect_games(
            "User games (v2)",
            f"https://games.roblox.com/v2/users/{owner_id}/games?sortOrder=Asc&limit=50",
        )

        if not usable_games:
            _collect_games(
                "Creator dashboard (apis)",
                f"https://apis.roblox.com/universes/v1/search?CreatorType=User&CreatorTargetId={owner_id}&isArchived=false&limit=50",
            )

        if not usable_games:
            try:
                groups_resp = session.get(
                    f"https://groups.roblox.com/v2/users/{owner_id}/groups/roles",
                    timeout=10,
                )
                if groups_resp.status_code == 200:
                    group_entries = groups_resp.json().get("data", [])
                    result["steps"].append(
                        f"<:clipboard:1497344037294702762> Groups: {len(group_entries)} found, scanning for games..."
                    )
                    for entry in group_entries:
                        group_id = (entry.get("group") or {}).get("id")
                        group_name = (entry.get("group") or {}).get("name", "?")
                        if group_id:
                            _collect_games(
                                f"Group '{group_name}' games",
                                f"https://games.roblox.com/v2/groups/{group_id}/games?sortOrder=Asc&limit=50",
                            )
                else:
                    result["steps"].append(
                        f"<:warning:1497344059017003079> Groups list: HTTP {groups_resp.status_code}"
                    )
            except Exception as e:
                result["steps"].append(
                    f"<:warning:1497344059017003079> Groups list error: {e}"
                )

        if not usable_games:
            result["error"] = (
                "No games found on this account or in any group you're in. "
                "Check the process log above for which endpoints returned what. "
                "Worst case, provide a `place_id` of a game you own/edit."
            )
            return _return_result()

        result["games"] = usable_games

        first_game = usable_games[0]
        universe_id = first_game["universeId"]
        place_id = str(first_game["placeId"])
        result["place_name"] = first_game["name"]

        result["steps"].append(
            f"<:check:1497344035696672959> Found **{len(usable_games)}** game(s) - using: **{result['place_name']}**"
        )

        if len(usable_games) > 1:
            other_games = ", ".join(game["name"] for game in usable_games[1:4])
            suffix = "..." if len(usable_games) > 4 else ""
            result["steps"].append(f"Other games: {other_games}{suffix}")

    try:
        csrf_resp = session.post("https://auth.roblox.com/v2/logout", json={}, timeout=10)
        csrf_token = csrf_resp.headers.get("x-csrf-token")
        if not csrf_token:
            result["error"] = "Failed to obtain CSRF token."
            return _return_result()
        result["steps"].append("<:check:1497344035696672959> CSRF token obtained")
    except Exception as e:
        result["error"] = f"CSRF request failed: {e}"
        return _return_result()

    gamepass_name = name if name else f"Gamepass {price}R$"
    gamepass_description = f"Gamepass - {price} Robux"
    result["gamepass_name"] = gamepass_name

    icon_bytes = _create_simple_png()
    create_url = f"https://apis.roblox.com/game-passes/v1/universes/{universe_id}/game-passes"
    create_headers = {"X-CSRF-TOKEN": csrf_token}

    config_applied = False

    try:
        # curl_cffi requires multipart= with a CurlMime, not requests-style
        # files=. Field names are lowercase to match the universe-scoped
        # gamepass create endpoint (Pascal-cased shape returns 410).
        create_mime = CurlMime()
        create_mime.addpart("name", data=gamepass_name.encode("utf-8"))
        create_mime.addpart("description", data=gamepass_description.encode("utf-8"))
        create_mime.addpart("isForSale", data=b"true")
        create_mime.addpart("price", data=str(price).encode("utf-8"))
        create_mime.addpart(
            "imageFile",
            filename="icon.png",
            content_type="image/png",
            data=icon_bytes,
        )

        create_resp = session.post(
            create_url,
            headers=create_headers,
            multipart=create_mime,
            timeout=20,
        )

        if create_resp.status_code == 200:
            create_data = create_resp.json()
            gamepass_id = create_data.get("gamePassId") or create_data.get("id")
            result["gamepass_id"] = gamepass_id
            result["steps"].append(f"<:check:1497344035696672959> Gamepass created - ID: `{gamepass_id}`")

            price_info = create_data.get("priceInformation") or {}
            created_price = price_info.get("defaultPriceInRobux")
            created_for_sale = create_data.get("isForSale")
            if created_for_sale and created_price == price:
                config_applied = True
                result["price"] = price
                result["steps"].append(
                    f"<:check:1497344035696672959> Create API already set the price to **{price} R$**"
                )
        else:
            result["error"] = (
                f"Failed to create gamepass (HTTP {create_resp.status_code}): "
                f"{_format_api_error(create_resp)}"
            )
            return _return_result()
    except Exception as e:
        result["error"] = f"Gamepass creation failed: {e}"
        return _return_result()

    if not config_applied:
        try:
            update_url = f"https://apis.roblox.com/game-passes/v1/universes/{universe_id}/game-passes/{result['gamepass_id']}"
            update_mime = CurlMime()
            update_mime.addpart("name", data=gamepass_name.encode("utf-8"))
            update_mime.addpart("description", data=gamepass_description.encode("utf-8"))
            update_mime.addpart("isForSale", data=b"true")
            update_mime.addpart("price", data=str(price).encode("utf-8"))

            update_resp = session.patch(
                update_url,
                headers=create_headers,
                multipart=update_mime,
                timeout=20,
            )

            if update_resp.status_code in (200, 204):
                config_applied = True
                result["price"] = price
                result["steps"].append(f"<:check:1497344035696672959> Sale settings updated to **{price} R$**")
            else:
                result["error"] = (
                    f"Failed to update gamepass sale settings (HTTP {update_resp.status_code}): "
                    f"{_format_api_error(update_resp)}"
                )
                return _return_result()
        except Exception as e:
            result["error"] = f"Failed to update gamepass sale settings: {e}"
            return _return_result()

    time.sleep(2)

    verified = False
    verify_endpoints = [
        f"https://apis.roblox.com/game-passes/v1/game-passes/{result['gamepass_id']}/product-info",
        f"https://economy.roblox.com/v1/game-passes/{result['gamepass_id']}/game-pass-product-info",
    ]

    for endpoint in verify_endpoints:
        try:
            verify_resp = roblox_get(endpoint, timeout=10)
            if verify_resp.status_code != 200:
                continue

            verify_data = verify_resp.json()
            actual_for_sale = verify_data.get("IsForSale")
            if actual_for_sale is None:
                actual_for_sale = verify_data.get("isForSale", False)

            actual_price = verify_data.get("PriceInRobux")
            if actual_price is None:
                actual_price = ((verify_data.get("priceInformation") or {}).get("defaultPriceInRobux"))

            if actual_for_sale and actual_price == price:
                result["price"] = price
                result["success"] = True
                result["steps"].append(
                    f"<:check:1497344035696672959> Verified public listing - **{price} R$** and for sale"
                )
                verified = True
                break

            if actual_for_sale:
                result["price"] = actual_price
                result["success"] = True
                result["steps"].append(
                    f"<:warning:1497344059017003079> For sale but price is **{actual_price} R$** (requested {price})"
                )
                verified = True
                break
        except Exception:
            continue

    if not verified and config_applied:
        result["success"] = True
        result["price"] = price
        result["steps"].append("<:check:1497344035696672959> Create API accepted the sale settings")
        result["steps"].append("Public verification was not ready yet, but the pass was created successfully")
    elif not verified:
        result["steps"].append("<:warning:1497344059017003079> Gamepass created but could not verify sale settings")
        result["steps"].append("The price may need to be checked manually on Roblox website")
        result["steps"].append(f"https://www.roblox.com/game-pass/configure?id={result['gamepass_id']}")
        result["success"] = True

    return _return_result()


def _format_api_error(response) -> str:
    """Return a compact API error string from Roblox responses."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:300] if response.text else "No details"

    if isinstance(data, dict):
        parts = []
        for key in ("errorMessage", "errorCode", "field", "hint"):
            value = data.get(key)
            if value:
                parts.append(str(value))
        if parts:
            return " | ".join(parts)

    return response.text[:300] if response.text else "No details"


def _create_simple_png(width=150, height=150):
    """Generate a minimal solid-color PNG icon for the gamepass placeholder."""
    r, g, b = 30, 50, 120
    raw_data = b""
    for _y in range(height):
        raw_data += b"\x00"
        for _x in range(width):
            raw_data += bytes([r, g, b])

    compressed = zlib.compress(raw_data)

    def chunk(chunk_type, data):
        payload = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + payload + crc

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", compressed)
    png += chunk(b"IEND", b"")
    return png
