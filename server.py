"""
server.py

Flask backend for the web version of the collaborative pixel canvas.
Serves the static frontend, the canvas API, and Discord OAuth login.

Identity: painting now requires signing in with Discord (scope: `identify`).
The session holds the Discord user id/username/avatar; charges and
cooldowns are keyed by that Discord id in canvas_state.py, so a user's
timer follows their account instead of a browser. Nobody can spoof
another account's charges because the user id comes from the signed
session cookie, never from a client-supplied header.

Run:
    pip install -r requirements.txt
    cp .env.example .env   # fill in your Discord app's client id/secret
    python server.py
Then open http://localhost:5000
"""

import os
import re
import secrets
import time
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory, abort, session, redirect

import canvas_state as state

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")

app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    # Dev fallback so the app still runs, but sessions won't survive a
    # restart and won't be safe in production. Set FLASK_SECRET_KEY for
    # any real deployment.
    app.secret_key = secrets.token_hex(32)
    print("WARNING: FLASK_SECRET_KEY not set — using an ephemeral dev key. "
          "Sessions will reset every restart. Set FLASK_SECRET_KEY in .env "
          "before deploying.")

app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    # Cookies only over HTTPS once deployed for real — inferred from the
    # redirect URI's scheme so local http development isn't broken by it.
    SESSION_COOKIE_SECURE=os.environ.get("DISCORD_REDIRECT_URI", "").startswith("https://"),
)

ADMIN_KEY = os.environ.get("PLACE_ADMIN_KEY", "")  # set this to enable /api/admin/*

DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "http://localhost:5000/auth/callback")
DISCORD_API = "https://discord.com/api"

_NAME_RE = re.compile(r"[^a-zA-Z0-9 _.-]")


# ---------------------------------------------------------------------------
# Session / identity helpers
# ---------------------------------------------------------------------------

def _current_user():
    """Returns the logged-in user's {id, username, avatar_url} from the
    session, or None if not logged in. Never trusts client input."""
    if "discord_id" not in session:
        return None
    return {
        "id": session["discord_id"],
        "username": session.get("discord_username", "unknown"),
        "avatar_url": session.get("discord_avatar_url"),
    }


def _require_login():
    user = _current_user()
    if not user:
        abort(401, description="sign in with Discord to do that")
    return user


def _default_avatar_url(discord_id: str, discriminator: str) -> str:
    if discriminator and discriminator != "0":
        index = int(discriminator) % 5
    else:
        index = (int(discord_id) >> 22) % 6
    return f"https://cdn.discordapp.com/embed/avatars/{index}.png"


def _avatar_url(user_json: dict) -> str:
    discord_id = user_json["id"]
    avatar_hash = user_json.get("avatar")
    if avatar_hash:
        ext = "gif" if avatar_hash.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.{ext}?size=64"
    return _default_avatar_url(discord_id, user_json.get("discriminator", "0"))


def _display_name(user_json: dict) -> str:
    name = user_json.get("global_name") or user_json.get("username") or "unknown"
    return _NAME_RE.sub("", name)[:32] or "unknown"


# ---------------------------------------------------------------------------
# Discord OAuth
# ---------------------------------------------------------------------------

@app.get("/auth/login")
def auth_login():
    if not DISCORD_CLIENT_ID or not DISCORD_CLIENT_SECRET:
        abort(500, description="Discord OAuth isn't configured on this server "
                                "(missing DISCORD_CLIENT_ID/DISCORD_CLIENT_SECRET)")

    csrf_state = secrets.token_urlsafe(24)
    session["oauth_state"] = csrf_state
    session["oauth_return_to"] = request.args.get("return_to", "/")

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": csrf_state,
        "prompt": "none",
    }
    return redirect(f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}")


@app.get("/auth/callback")
def auth_callback():
    error = request.args.get("error")
    if error:
        return redirect(f"/?auth_error={error}")

    if not request.args.get("state") or request.args.get("state") != session.pop("oauth_state", None):
        abort(400, description="OAuth state mismatch — please try signing in again")

    code = request.args.get("code")
    if not code:
        abort(400, description="missing authorization code")

    token_res = requests.post(
        f"{DISCORD_API}/oauth2/token",
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )
    if not token_res.ok:
        return redirect("/?auth_error=token_exchange_failed")
    access_token = token_res.json().get("access_token")

    user_res = requests.get(
        f"{DISCORD_API}/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not user_res.ok:
        return redirect("/?auth_error=user_fetch_failed")
    user_json = user_res.json()

    session["discord_id"] = user_json["id"]
    session["discord_username"] = _display_name(user_json)
    session["discord_avatar_url"] = _avatar_url(user_json)
    session.permanent = True

    return_to = session.pop("oauth_return_to", "/")
    return redirect(return_to or "/")


@app.post("/auth/logout")
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/session")
def api_session():
    user = _current_user()
    return jsonify({"logged_in": bool(user), "user": user})


# ---------------------------------------------------------------------------
# Canvas API
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/canvas")
def api_canvas():
    return jsonify(state.get_canvas_snapshot())


@app.get("/api/palette")
def api_palette():
    return jsonify(state.load_palette())


@app.get("/api/me")
def api_me():
    user = _require_login()
    status = state.get_user_status(user["id"])
    status["user"] = user
    return jsonify(status)


@app.get("/api/updates")
def api_updates():
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        abort(400, description="`since` must be an integer version number")
    changes = state.get_changes_since(since)
    return jsonify({"since": since, "changes": changes})


@app.get("/api/activity")
def api_activity():
    limit = min(int(request.args.get("limit", "25")), 100)
    return jsonify(state.get_recent_placements(limit))


@app.post("/api/paint")
def api_paint():
    user = _require_login()
    body = request.get_json(silent=True) or {}

    try:
        x = int(body["x"])
        y = int(body["y"])
        color_id = int(body["color_id"])
    except (KeyError, TypeError, ValueError):
        abort(400, description="body must include integer x, y, color_id")

    palette = state.load_palette()
    if str(color_id) not in palette:
        abort(400, description="unknown color_id")

    result = state.paint_pixel(x, y, color_id, user["id"], user["username"])
    status_code = 200 if result.get("ok") else 429 if result.get("error") == "no_charges" else 400
    return jsonify(result), status_code


def _require_admin():
    if not ADMIN_KEY or request.headers.get("X-Admin-Key") != ADMIN_KEY:
        abort(403, description="admin key missing or incorrect")


@app.post("/api/admin/fill")
def api_admin_fill():
    _require_admin()
    user = _current_user() or {"id": "admin", "username": "admin"}
    body = request.get_json(silent=True) or {}
    try:
        x1, y1, x2, y2, color_id = (int(body[k]) for k in ("x1", "y1", "x2", "y2", "color_id"))
    except (KeyError, TypeError, ValueError):
        abort(400, description="body must include integer x1, y1, x2, y2, color_id")
    return jsonify(state.fill_rect(x1, y1, x2, y2, color_id, user["id"], user["username"]))


@app.post("/api/admin/revert")
def api_admin_revert():
    _require_admin()
    user = _current_user()
    body = request.get_json(silent=True) or {}
    try:
        n = int(body.get("n", 1))
    except (TypeError, ValueError):
        abort(400, description="body must include integer n")
    if user:
        return jsonify(state.revert_last_n_ops(n, acting_user_id=user["id"], acting_display_name=user["username"]))
    return jsonify(state.revert_last_n_ops(n))


# JSON error responses instead of Flask's default HTML error pages, so the
# frontend can read `error.description` from any failed fetch consistently.
@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(429)
@app.errorhandler(500)
def _json_error(e):
    return jsonify({"ok": False, "error": getattr(e, "description", str(e))}), e.code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # Debug mode enables Werkzeug's interactive debugger, which lets anyone
    # who triggers an unhandled error run arbitrary Python in your process.
    # Fine for local dev, never safe to expose publicly — off by default,
    # opt in explicitly with FLASK_DEBUG=true for local troubleshooting.
    debug = os.environ.get("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)

