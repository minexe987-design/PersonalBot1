# ──────────────────────────────────────────────────────────────────
# Personal Roblox Gamepass Purchase Utility
# This module automates the gamepass purchase flow for the account
# owner's own account. It uses official Roblox Economy API endpoints
# to look up gamepass details, verify balance, and submit a purchase.
# All actions are performed by the authenticated account owner only.
# ──────────────────────────────────────────────────────────────────

from core.utils import make_roblox_session, record_rotated_cookie, roblox_get, sanitize_cookie


def autobuy_gamepass(cookie: str, gamepass_id: str) -> dict:
    """
    Purchases a Roblox gamepass on behalf of the authenticated user.

    Workflow:
        1. Validate the session token and identify the buyer
        2. Fetch gamepass details (name, price, seller info)
        3. Check buyer's Robux balance
        4. Obtain CSRF token for the purchase request
        5. Submit the purchase

    Returns a dict with:
        success (bool), steps (list of str), error (str or None),
        gamepass_name (str or None), price (int or None),
        seller (str or None), robux_balance (int or None)
    """
    result = {
        "success": False,
        "steps": [],
        "error": None,
        "gamepass_name": None,
        "gamepass_id": gamepass_id,
        "price": None,
        "seller": None,
        "robux_balance": None,
        "buyer_username": None,
        "buyer_user_id": None,
        "cookie_was_rotated": False,
        "rotated_cookie": None,
    }

    cookie = sanitize_cookie(cookie)
    gamepass_id = gamepass_id.strip()

    if not cookie:
        result["error"] = "No cookie provided."
        return result
    if not gamepass_id:
        result["error"] = "No gamepass ID provided."
        return result

    session = make_roblox_session(cookie)

    def _return_result():
        record_rotated_cookie(
            result,
            session,
            cookie,
            "<:warning:1497344059017003079> Roblox rotated this cookie during autobuy. Use the fresh cookie sent privately.",
        )
        return result

    # Step 1: Validate session token and get buyer info
    try:
        auth_resp = session.get("https://users.roblox.com/v1/users/authenticated", timeout=10)
        if auth_resp.status_code == 200:
            buyer = auth_resp.json()
            buyer_id = buyer.get("id")
            buyer_name = buyer.get("name")
            result["buyer_username"] = buyer_name
            result["buyer_user_id"] = buyer_id
            result["steps"].append(f"<:check:1497344035696672959> Buyer: `{buyer_name}` (ID: `{buyer_id}`)")
        else:
            result["error"] = f"Cookie is invalid (HTTP {auth_resp.status_code})"
            return _return_result()
    except Exception as e:
        result["error"] = f"Failed to verify cookie: {e}"
        return _return_result()

    # Step 2: Fetch gamepass product details
    gp_data = None
    endpoints = [
        f"https://economy.roblox.com/v1/game-passes/{gamepass_id}/game-pass-product-info",
        f"https://apis.roblox.com/game-passes/v1/game-passes/{gamepass_id}/product-info",
    ]

    # Gamepass product-info is PUBLIC — hit it without the cookie so Roblox
    # doesn't rotate .ROBLOSECURITY on a plain read. (See CLAUDE.md gotcha.)
    for ep in endpoints:
        try:
            gp_resp = roblox_get(ep, timeout=10)
            if gp_resp.status_code == 200:
                gp_data = gp_resp.json()
                break
        except Exception:
            continue

    if not gp_data:
        # Fallback: marketplace batch endpoint — also PUBLIC.
        try:
            batch_resp = roblox_get(
                f"https://games.roblox.com/v1/games/game-passes?gamePassIds={gamepass_id}",
                timeout=10,
            )
            if batch_resp.status_code == 200:
                batch_data = batch_resp.json().get("data", [])
                if batch_data:
                    gp_item = batch_data[0]
                    gp_data = {
                        "Name": gp_item.get("name", "Unknown"),
                        "PriceInRobux": gp_item.get("price"),
                        "IsForSale": gp_item.get("price") is not None,
                        "Creator": {"Name": "Unknown", "Id": gp_item.get("sellerId")},
                        "ProductId": gp_item.get("productId"),
                    }
        except Exception:
            pass

    if not gp_data:
        result["error"] = f"Failed to fetch gamepass info — gamepass ID `{gamepass_id}` not found."
        return _return_result()

    result["gamepass_name"] = gp_data.get("Name", "Unknown")
    result["price"] = gp_data.get("PriceInRobux")
    creator_name = gp_data.get("Creator", {}).get("Name", "Unknown")
    creator_id = gp_data.get("Creator", {}).get("Id")
    is_for_sale = gp_data.get("IsForSale", False)
    product_id = gp_data.get("ProductId")

    result["seller"] = creator_name
    result["steps"].append(
        f"<:check:1497344035696672959> Gamepass: **{result['gamepass_name']}** — "
        f"Price: **{result['price']} R$** — "
        f"Seller: `{creator_name}`"
    )

    if not is_for_sale:
        result["error"] = "This gamepass is **not for sale**."
        return _return_result()

    if result["price"] is None or result["price"] == 0:
        result["error"] = "This gamepass has no price set (free or not purchasable)."
        return _return_result()

    # Step 3: Check Robux balance before purchasing
    try:
        robux_resp = session.get(
            f"https://economy.roblox.com/v1/users/{buyer_id}/currency",
            timeout=10,
        )
        if robux_resp.status_code == 200:
            balance = robux_resp.json().get("robux", 0)
            result["robux_balance"] = balance
            result["steps"].append(f"<a:moneybag:1497344054990733535> Robux balance: **{balance} R$**")

            if balance < result["price"]:
                result["error"] = (
                    f"Insufficient Robux! Need **{result['price']} R$** but only have **{balance} R$**."
                )
                return _return_result()
        else:
            result["steps"].append("<:warning:1497344059017003079> Could not check Robux balance — proceeding anyway")
    except Exception:
        result["steps"].append("<:warning:1497344059017003079> Could not check Robux balance — proceeding anyway")

    # Step 4: Obtain CSRF token for the purchase
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

    # Step 5: Submit the purchase request
    purchase_url = f"https://economy.roblox.com/v1/purchases/products/{product_id}"
    purchase_headers = {
        "X-CSRF-TOKEN": csrf_token,
        "Content-Type": "application/json",
    }
    purchase_body = {
        "expectedCurrency": 1,
        "expectedPrice": result["price"],
        "expectedSellerId": creator_id,
    }

    try:
        buy_resp = session.post(
            purchase_url,
            headers=purchase_headers,
            json=purchase_body,
            timeout=15,
        )

        if buy_resp.status_code == 200:
            buy_data = buy_resp.json()
            purchased = buy_data.get("purchased", False)
            reason = buy_data.get("reason", "")
            status_code = buy_data.get("statusCode")

            if purchased:
                result["success"] = True
                result["steps"].append("<:check:1497344035696672959> **Purchase successful!**")

                # Update remaining balance after purchase
                try:
                    bal_resp = session.get(
                        f"https://economy.roblox.com/v1/users/{buyer_id}/currency",
                        timeout=10,
                    )
                    if bal_resp.status_code == 200:
                        result["robux_balance"] = bal_resp.json().get("robux", 0)
                        result["steps"].append(f"<a:moneybag:1497344054990733535> Remaining balance: **{result['robux_balance']} R$**")
                except Exception:
                    pass
            else:
                error_msg = reason or f"Purchase rejected (status code: {status_code})"
                result["error"] = error_msg
                result["steps"].append(f"<:x:1497344061592436737> {error_msg}")
        else:
            error_text = buy_resp.text[:300] if buy_resp.text else "No details"
            result["error"] = f"Purchase failed (HTTP {buy_resp.status_code}): {error_text}"

    except Exception as e:
        result["error"] = f"Purchase request failed: {e}"

    return _return_result()
