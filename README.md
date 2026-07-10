# PLACE — web version

A browser-based clone of the Discord pixel canvas bot, styled after r/place:
click-and-drag to pan, scroll/pinch to zoom, click a cell then hit **place
pixel** to commit it. Painting requires signing in with Discord — charges
and cooldowns are tied to your Discord account, not your browser.

## Run it

```
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:
1. Go to https://discord.com/developers/applications → **New Application**.
2. Under **OAuth2** → **General**, copy the **Client ID** and **Client
   Secret** into `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET`.
3. Under **OAuth2** → **Redirects**, add `http://localhost:5000/auth/callback`
   exactly (it must match `DISCORD_REDIRECT_URI` character for character —
   Discord rejects the login if these don't match).
4. Generate a session secret: `python -c "import secrets; print(secrets.token_hex(32))"`
   and put it in `FLASK_SECRET_KEY`.

Then:

```
python server.py
```

Open **http://localhost:5000**, click **Sign in with Discord**, and you're
in. Multiple browser tabs/devices all see each other's pixels (polled every
~1.5s).

## About the 405 you were seeing

I tested `POST /api/paint` directly against this Flask app (both via
Flask's test client and a live `curl` against the running server) and it
returns 200 correctly — so the bug isn't in the paint route itself. The
far more common cause of a 405 specifically on the "place pixel" action is
**the frontend being served from somewhere that isn't this Flask app** —
for example, opening `static/index.html` directly, or hosting the static
files on a plain static host / CDN in front of the app. Most static hosts
(GitHub Pages, a bare `python -m http.server`, many CDNs) only serve GET
and will hard-reject any POST with a 405, regardless of what the backend
would've done with it.

Since Discord OAuth needs a real backend at a stable URL anyway (for the
`/auth/callback` redirect), the fix is the same either way: **serve
`static/` and the API from this one Flask process**, which is exactly what
`python server.py` does — don't put a separate static host in front of it.
If you deploy this (e.g. to Fly.io, Render, a VPS), deploy this whole app
as one service rather than splitting frontend/backend across two hosts.

If you're still seeing a 405 after confirming that, check your browser's
Network tab for the exact request URL and response — happy to dig further
with those details.

## How it's put together

- **`canvas_state.py`** — same data model as the Discord bot: a flat pixel
  grid, per-user charges that regen on a timer, an append-only change log
  for revert support. User ids are now Discord snowflakes instead of a
  client-generated UUID. (While rebuilding this I also caught and fixed a
  real bug in the refill math: spending a charge shortly after a partial
  refill was snapping the count back up instead of decrementing, because
  the refill "anchor" timestamp wasn't being advanced correctly — fixed
  and covered by a reproduction test.)

  **Change log format:** every pixel edit (coordinate, who did it, which
  color) is stored, but as a fixed-width **binary** record rather than a
  JSON line — each edit costs exactly 22 bytes (`data/changes.log.bin`),
  versus roughly 150–220 bytes for the equivalent JSON-per-line entry.
  Display names aren't repeated per edit either; they're normalized into
  `users.json` (one row per Discord account) and looked up by id when
  building a response, so a user placing 10,000 pixels costs one copy of
  their name, not 10,000. Because every record is exactly 22 bytes and
  versions increment by exactly 1 per record, "changes since version N"
  is a single file seek, not a scan of the whole log — this also made
  `revert` cheaper, since finding the last N operations reads backward in
  fixed-size chunks instead of loading the entire history into memory.
  One side effect worth knowing: reverts now write their own log entries
  (attributed to whoever triggered them), so they show up in the activity
  history — previously a revert changed pixels without leaving any trace
  in the log at all.

  If an old `data/changes.log.jsonl` from a previous run of this project
  is found on startup, it's migrated to the new binary format
  automatically and renamed to `changes.log.jsonl.migrated` (kept as a
  backup, not deleted). Your existing canvas and pixels are untouched by
  this either way — only the history/audit log's storage format changes.

- **`server.py`** — the Flask API plus Discord OAuth:
  - `GET /auth/login` — redirects to Discord's authorize page (with a
    CSRF `state` token stored in the session).
  - `GET /auth/callback` — validates `state`, exchanges the code for a
    token, fetches the Discord profile, and stores `{id, username,
    avatar_url}` in a signed, httpOnly session cookie.
  - `POST /auth/logout` — clears the session.
  - `GET /api/session` — current login state, used by the frontend on load.
  - `POST /api/paint` and `GET /api/me` now require a valid session —
    identity comes from the cookie, **not** a client header, so nobody can
    spend someone else's charges by sending a different id.
  - `/api/admin/fill` and `/api/admin/revert` still exist, gated behind
    `PLACE_ADMIN_KEY` as before (see "Staff tools" below).
- **`static/`** — the frontend. `app.js` handles pan/zoom, an explicit
  "place pixel" confirm step, the charge/cooldown display, a short-lived
  "wet paint" glow on freshly placed pixels, and now a Discord sign-in/out
  panel that gates painting behind login.

## Hosting it for other people

A few things need to change between "running on my laptop" and "other
people can reach this," beyond just picking a host:

### 1. Turn off debug mode (already done by default)

`server.py` now only enables Flask's debugger if you explicitly set
`FLASK_DEBUG=true`. Leave it unset (or `false`) for anything reachable by
anyone but you — the debugger lets whoever triggers an unhandled error run
arbitrary Python in your process. Never turn it on outside local dev.

### 2. Use a real WSGI server, not `python server.py`

Flask's built-in server (what `python server.py` runs) says so itself:
it's not meant for production traffic. Use gunicorn instead:

```
gunicorn -w 1 --threads 8 --bind 0.0.0.0:$PORT server:app
```

This repo includes a `Procfile` with that exact command, which Render,
Railway, and similar platforms auto-detect.

**Why `-w 1` (one worker):** the canvas/charges/log writes are protected
by an in-process lock, which only coordinates threads within a single
process — it does nothing across multiple worker *processes*. Multiple
gunicorn workers could each grab the "same" pixel at once and one write
would silently clobber the other. `--threads 8` still gives you real
concurrency (the lock handles that correctly), just within one process.
If you outgrow that, the fix is moving state into something that handles
real multi-process concurrency itself (SQLite with proper locking,
Postgres, Redis) rather than adding workers.

### 3. Point `data/` at storage that survives a redeploy

Most hosting platforms wipe the filesystem on every deploy — if `data/`
lives inside the app's own folder, your canvas resets every time you push
a change. Set `PLACE_DATA_DIR` to a mounted persistent volume:

```
PLACE_DATA_DIR=/data
```

(Fly.io volumes, Render persistent disks, and a plain VPS's normal
filesystem all work — just point it somewhere that isn't wiped on deploy.)

### 4. Update the Discord redirect URI to your real domain

Both of these need to say the same thing, exactly:
- `DISCORD_REDIRECT_URI` in your production `.env` / host's environment
  variables — e.g. `https://place.yourdomain.com/auth/callback`
- **OAuth2 → Redirects** in the Discord dashboard for this application

Use `https://`, not `http://` — every option below gives you HTTPS for
free, and the session cookie is automatically marked "HTTPS only" once it
detects an `https://` redirect URI (see `SESSION_COOKIE_SECURE` in
`server.py`), so painting won't work over plain HTTP in production anyway.

### Picking a host

**Fly.io** or **Render** are the easiest fit for this app's shape (one
small Python process + a persistent volume for `data/`), and both have
a free or near-free tier:
- Push this repo, set the environment variables above in their dashboard
  (`DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`,
  `FLASK_SECRET_KEY`, `PLACE_ADMIN_KEY`, `PLACE_DATA_DIR`), attach a small
  persistent volume mounted at whatever path you set `PLACE_DATA_DIR` to,
  and deploy. Both platforms give you HTTPS automatically.

**A plain VPS** (DigitalOcean, Linode, a home server) if you want full
control: install Python, `pip install -r requirements.txt`, run it under
gunicorn as above (ideally supervised by `systemd` so it restarts if it
crashes), and put nginx in front of it for HTTPS via
[Certbot](https://certbot.eff.org/) / Let's Encrypt. `data/` just lives on
the VPS's normal disk — no extra volume needed since nothing wipes it
between deploys unless you tell it to.

Either way, generate a fresh `FLASK_SECRET_KEY` for production (don't
reuse your local dev one) and keep `PLACE_ADMIN_KEY` private — anyone
with it can fill/revert the canvas.



- Canvas size and charge/cooldown parameters: top of `canvas_state.py`
  (`CANVAS_SIZE`, `MAX_CHARGES`, `CHARGE_INTERVAL`).
- Colors: `palette.json` — edited live, no restart needed.

## Staff tools

`/api/admin/fill` and `/api/admin/revert` are gated behind `PLACE_ADMIN_KEY`
(set it in `.env`, then send it as the `X-Admin-Key` header). This is
separate from Discord login — it's a shared secret rather than a per-role
check. If you want this tied to specific Discord roles/permissions instead
(e.g. "only admins in this guild"), that needs one more step: after login,
call the Discord API (`GET /guilds/{guild.id}/members/{user.id}`) with a
bot token to check the member's roles, and store an `is_staff` flag in the
session. Happy to add that next if you want role-gated staff tools instead
of a shared key.

## Notes / what I'd add next

- Polling is simple and dependency-free but not instant push; a websocket
  layer would make other users' pixels appear faster if that matters at
  scale.
- Storage is flat JSON files in `data/` — fine for a small canvas, but
  worth moving to SQLite/Postgres if this gets busy, since the in-process
  lock in `canvas_state.py` only protects against concurrent writes within
  a single server process (multiple processes/workers would need a real
  database or a cross-process lock).
- Sessions are Flask's default signed-cookie sessions (no server-side
  store) — fine for this scale; if you need to forcibly log a user out
  server-side later, that needs a server-side session store instead.

