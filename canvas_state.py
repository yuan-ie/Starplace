"""
canvas_state.py

Persistent state for the web version of the collaborative pixel canvas.
This is the same data model used by the original Discord bot (charges,
palette, append-only change log with revert), adapted so it's keyed by a
Discord user id (verified via OAuth session, not a client-supplied
header), with a monotonic `version` counter so the frontend can poll for
just the pixels that changed since it last checked.

The change log (`changes.log.bin`) is a fixed-width BINARY format rather
than one-JSON-object-per-line. Reasoning: a JSONL line repeating a 36-char
UUID, an 18-digit Discord id *as text*, and (previously) a duplicated
display name runs ~150-220 bytes per pixel edit. For a canvas that can
accumulate hundreds of thousands of edits, that adds up fast, and text
lines can't be randomly seeked into — reading "changes since version N"
means parsing every line from the start.

Each edit is instead packed into one 22-byte record:

    op_id           uint32   groups pixels from one logical action
                             (a fill of 500 pixels shares one op_id)
    ts              uint32   unix epoch seconds
    user_id         uint64   Discord snowflake, stored numerically
    x, y            uint16   canvas coordinates
    color_id        uint8    palette index placed
    prev_color_id   uint8    palette index it replaced (for revert)

Display names are NOT repeated per record — they're normalized into
users.json (one row per user, already used for charges) and looked up by
user_id when building a response. A user painting 10,000 pixels used to
cost 10,000 copies of their name; now it costs one.

Because every record is exactly RECORD_SIZE bytes and versions increment
by exactly 1 per record (reverts now log their own pixel changes too,
see revert_last_n_ops), a version's record lives at a fixed byte offset:
    offset = (version - 1) * RECORD_SIZE
so "changes since version N" is a single seek + read, not a full scan.
"""

import json
import os
import struct
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DATA_DIR = Path(os.environ.get("PLACE_DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CANVAS_FILE = DATA_DIR / "canvas.json"
USERS_FILE = DATA_DIR / "users.json"
CHANGELOG_FILE = DATA_DIR / "changes.log.bin"
LEGACY_CHANGELOG_FILE = DATA_DIR / "changes.log.jsonl"  # migrated on first run if present
PALETTE_FILE = Path(__file__).parent / "palette.json"

# ---------------------------------------------------------------------------
# Tunable parameters — edit these to change canvas size / cooldown behavior.
# Canvas size changes only take effect for a fresh canvas (no existing
# canvas.json on disk); charge parameters take effect immediately since
# refill math is computed live from timestamps, not pre-scheduled.
# ---------------------------------------------------------------------------
CANVAS_SIZE = 200            # NxN grid
MAX_CHARGES = 5               # max stacked pixels a user can bank
CHARGE_INTERVAL = 1 * 60     # seconds to regain one charge
# ---------------------------------------------------------------------------

SYSTEM_USER_ID = 0  # sentinel for system/admin actions with no real account behind them

# op_id(u32) ts(u32) user_id(u64) x(u16) y(u16) color_id(u8) prev_color_id(u8)
RECORD_STRUCT = struct.Struct("<IIQHHBB")
RECORD_SIZE = RECORD_STRUCT.size  # 22 bytes
assert RECORD_SIZE == 22

_lock = threading.Lock()  # single-process safety net around read-modify-write


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

def load_palette() -> Dict[str, dict]:
    """Re-read palette.json from disk every call so colors can be edited
    live without restarting the server. Keys are string color ids."""
    with open(PALETTE_FILE, "r") as f:
        return json.load(f)


def get_color_hex(color_id) -> str:
    palette = load_palette()
    entry = palette.get(str(color_id))
    return entry["hex"] if entry else "#000000"


# ---------------------------------------------------------------------------
# Canvas grid
# ---------------------------------------------------------------------------

def _default_canvas_doc() -> dict:
    return {
        "size": CANVAS_SIZE,
        "version": 0,
        "next_op_id": 1,
        "pixels": [0] * (CANVAS_SIZE * CANVAS_SIZE),
    }


def load_canvas_doc() -> dict:
    if not CANVAS_FILE.exists():
        doc = _default_canvas_doc()
        _save_canvas_doc(doc)
        return doc
    with open(CANVAS_FILE, "r") as f:
        doc = json.load(f)
    doc.setdefault("next_op_id", 1)  # backfill for canvases saved before this field existed
    return doc


def _alloc_op_id(doc: dict) -> int:
    """Allocates a new op id and advances the counter on `doc` in place.
    Caller is responsible for saving `doc` afterward."""
    op_id = doc["next_op_id"]
    doc["next_op_id"] += 1
    return op_id


def _save_canvas_doc(doc: dict) -> None:
    tmp = CANVAS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(doc, f)
    tmp.replace(CANVAS_FILE)


def get_canvas_snapshot() -> dict:
    """Full state for a client loading the page for the first time."""
    doc = load_canvas_doc()
    return {"size": doc["size"], "version": doc["version"], "pixels": doc["pixels"]}


# ---------------------------------------------------------------------------
# Users / charges
# ---------------------------------------------------------------------------

def _load_users() -> dict:
    if not USERS_FILE.exists():
        return {}
    with open(USERS_FILE, "r") as f:
        return json.load(f)


def _save_users(users: dict) -> None:
    tmp = USERS_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(users, f)
    tmp.replace(USERS_FILE)


def _touch_username(users: dict, user_id: str, display_name: str) -> None:
    """Records/updates a user's display name in the (already-loaded) users
    dict. This is the log's only copy of the name — the binary change log
    stores just the numeric user_id, so a user painting thousands of
    pixels doesn't cost thousands of copies of their name."""
    record = users.setdefault(user_id, {"charges": MAX_CHARGES, "last_refill_ts": _now()})
    record["username"] = display_name


def _lookup_usernames(user_ids: set) -> Dict[str, str]:
    """Batch name lookup for rendering change-log entries."""
    if not user_ids:
        return {}
    users = _load_users()
    return {uid: users.get(uid, {}).get("username", f"user-{uid[:6]}") for uid in user_ids}


def _compute_live_charges(record: dict) -> Tuple[int, float, Optional[float]]:
    """Given a stored user record, compute (current_charges,
    advanced_last_refill_ts, next_refill_at) as of right now, based on
    elapsed time since last_refill_ts.

    `advanced_last_refill_ts` folds in any whole intervals that have
    already ticked over, so callers that persist it back don't re-grant
    the same elapsed time on a later call. Read-only callers can ignore
    it; `_spend_charge` must persist it or charges can "rewind" back up
    after being spent (each read would re-derive the same gain from the
    stale anchor)."""
    charges = record.get("charges", MAX_CHARGES)
    last_refill_ts = record.get("last_refill_ts", _now())

    if charges >= MAX_CHARGES:
        return MAX_CHARGES, last_refill_ts, None

    elapsed = _now() - last_refill_ts
    gained = int(elapsed // CHARGE_INTERVAL)
    if gained > 0:
        charges = min(MAX_CHARGES, charges + gained)
        last_refill_ts += gained * CHARGE_INTERVAL

    if charges >= MAX_CHARGES:
        return MAX_CHARGES, last_refill_ts, None

    next_refill_at = last_refill_ts + CHARGE_INTERVAL
    return charges, last_refill_ts, next_refill_at


def get_user_status(user_id: str) -> dict:
    """Read-only charge status for a user; does not persist refills."""
    users = _load_users()
    record = users.get(user_id, {"charges": MAX_CHARGES, "last_refill_ts": _now()})
    charges, _, next_refill_at = _compute_live_charges(record)
    return {
        "charges": charges,
        "max_charges": MAX_CHARGES,
        "next_refill_at": next_refill_at,
        "charge_interval": CHARGE_INTERVAL,
    }


def _spend_charge(user_id: str) -> bool:
    """Attempts to spend one charge for user_id. Returns True on success,
    False if the user has no charges available. Always persists the
    advanced refill anchor so already-granted elapsed time isn't re-applied
    on a later call."""
    with _lock:
        users = _load_users()
        record = users.get(user_id, {"charges": MAX_CHARGES, "last_refill_ts": _now()})
        charges, advanced_last_refill_ts, _ = _compute_live_charges(record)

        if charges <= 0:
            return False

        was_full = charges >= MAX_CHARGES
        charges -= 1

        record["charges"] = charges
        # Starting a fresh countdown from now when spending down from full
        # (no countdown was running while at max); otherwise persist the
        # advanced anchor so partial progress isn't lost or double-counted.
        record["last_refill_ts"] = _now() if was_full else advanced_last_refill_ts

        users[user_id] = record
        _save_users(users)
        return True


# ---------------------------------------------------------------------------
# Painting + change log (fixed-width binary — see module docstring)
# ---------------------------------------------------------------------------

def _pack_record(op_id: int, ts: float, user_id: int, x: int, y: int,
                  color_id: int, prev_color_id: int) -> bytes:
    return RECORD_STRUCT.pack(op_id & 0xFFFFFFFF, int(ts), user_id, x, y, color_id, prev_color_id)


def _unpack_record(raw: bytes, version: int) -> dict:
    op_id, ts, user_id, x, y, color_id, prev_color_id = RECORD_STRUCT.unpack(raw)
    return {
        "op_id": op_id,
        "version": version,
        "ts": ts,
        "user_id": str(user_id),  # stringified: Discord snowflakes exceed JS's safe integer range
        "x": x, "y": y,
        "color_id": color_id,
        "prev_color_id": prev_color_id,
    }


def _append_records(entries: List[dict]) -> None:
    """entries: dicts with op_id, ts, user_id (int), x, y, color_id, prev_color_id.
    Appends in order — callers must ensure one record is appended per
    version increment so `version - 1` stays a valid record index."""
    if not entries:
        return
    with open(CHANGELOG_FILE, "ab") as f:
        for e in entries:
            f.write(_pack_record(e["op_id"], e["ts"], e["user_id"], e["x"], e["y"],
                                  e["color_id"], e["prev_color_id"]))


def _record_count() -> int:
    if not CHANGELOG_FILE.exists():
        return 0
    return CHANGELOG_FILE.stat().st_size // RECORD_SIZE


def _read_records(start_index: int, count: Optional[int] = None) -> List[dict]:
    """Reads records starting at 0-based `start_index` (== version `start_index`,
    since version N's record lives at index N-1). A direct seek — no need
    to parse anything before it."""
    if not CHANGELOG_FILE.exists() or start_index < 0:
        return []
    with open(CHANGELOG_FILE, "rb") as f:
        f.seek(start_index * RECORD_SIZE)
        raw = f.read(count * RECORD_SIZE) if count is not None else f.read()
    out = []
    for i in range(0, len(raw) - RECORD_SIZE + 1, RECORD_SIZE):
        out.append(_unpack_record(raw[i:i + RECORD_SIZE], version=start_index + (i // RECORD_SIZE) + 1))
    return out


def _hydrate_names(entries: List[dict]) -> List[dict]:
    names = _lookup_usernames({e["user_id"] for e in entries})
    for e in entries:
        e["display_name"] = "system" if e["user_id"] == str(SYSTEM_USER_ID) else names[e["user_id"]]
    return entries


def paint_pixel(x: int, y: int, color_id: int, user_id: str, display_name: str) -> dict:
    """Spends a charge and paints one pixel. Returns the new canvas version
    and the pixel change, or an error dict if the user has no charges or
    the coordinates are out of range."""
    doc = load_canvas_doc()
    size = doc["size"]

    if not (0 <= x < size and 0 <= y < size):
        return {"ok": False, "error": "out_of_range"}

    if not _spend_charge(user_id):
        return {"ok": False, "error": "no_charges"}

    with _lock:
        doc = load_canvas_doc()
        idx = y * doc["size"] + x
        prev_color_id = doc["pixels"][idx]
        doc["pixels"][idx] = color_id
        doc["version"] += 1
        new_version = doc["version"]
        op_id = _alloc_op_id(doc)
        _save_canvas_doc(doc)

        _append_records([{
            "op_id": op_id, "ts": _now(), "user_id": int(user_id),
            "x": x, "y": y, "color_id": color_id, "prev_color_id": prev_color_id,
        }])

        users = _load_users()
        _touch_username(users, user_id, display_name)
        _save_users(users)

    return {"ok": True, "version": new_version, "x": x, "y": y, "color_id": color_id}


def get_recent_placements(limit: int = 25) -> List[dict]:
    """Tail of the change log, newest first — used for the activity ticker.
    Reads only the last `limit` records via a seek from the end, not the
    whole file."""
    total = _record_count()
    if total == 0:
        return []
    start = max(0, total - limit)
    entries = _read_records(start, total - start)
    entries.reverse()
    return _hydrate_names(entries)


def get_changes_since(version: int, cap: int = 5000) -> List[dict]:
    """Returns pixel changes with version > `version`, oldest first, capped
    so a client that's been away a long time falls back to a full refetch
    instead of replaying tens of thousands of individual writes. This is a
    single seek to the right offset, not a scan from the start."""
    total = _record_count()
    if version >= total:
        return []
    entries = _read_records(version, min(cap, total - version))
    return _hydrate_names(entries)


# ---------------------------------------------------------------------------
# Staff tools: rectangle fill + revert last N ops (mirrors the Discord bot's
# }fill and }revert). Gated behind a shared ADMIN_KEY at the API layer.
# ---------------------------------------------------------------------------

def fill_rect(x1: int, y1: int, x2: int, y2: int, color_id: int, user_id: str, display_name: str) -> dict:
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))

    with _lock:
        doc = load_canvas_doc()
        size = doc["size"]
        x1, x2 = max(0, x1), min(size - 1, x2)
        y1, y2 = max(0, y1), min(size - 1, y2)

        op_id = _alloc_op_id(doc)
        entries = []
        for y in range(y1, y2 + 1):
            for x in range(x1, x2 + 1):
                idx = y * size + x
                prev_color_id = doc["pixels"][idx]
                if prev_color_id == color_id:
                    continue
                doc["pixels"][idx] = color_id
                doc["version"] += 1
                entries.append({
                    "op_id": op_id, "ts": _now(), "user_id": int(user_id),
                    "x": x, "y": y, "color_id": color_id, "prev_color_id": prev_color_id,
                })

        _save_canvas_doc(doc)
        _append_records(entries)

        users = _load_users()
        _touch_username(users, user_id, display_name)
        _save_users(users)

    return {"ok": True, "version": doc["version"], "pixels_changed": len(entries)}


def revert_last_n_ops(n: int, acting_user_id: str = str(SYSTEM_USER_ID),
                       acting_display_name: str = "system") -> dict:
    """Undoes the last n distinct op_ids, walking the log newest-first.
    Reads backward in fixed-size chunks (never loads the whole log into
    memory) so this stays cheap even on a long-running canvas. The revert
    itself is logged as new pixel-edit records — attributed to
    `acting_user_id` — so the audit trail covers reverts too, not just the
    original edits."""
    total = _record_count()
    if total == 0:
        return {"ok": False, "error": "no_log"}

    CHUNK_RECORDS = 4096  # ~90KB per chunk — plenty to find n ops without reading everything
    seen_ops: List[int] = []
    seen_set = set()
    touched: Dict[Tuple[int, int], int] = {}  # (x, y) -> prev_color_id, newest-first wins

    cursor = total
    while cursor > 0 and len(seen_ops) < n:
        chunk_start = max(0, cursor - CHUNK_RECORDS)
        chunk = _read_records(chunk_start, cursor - chunk_start)
        for entry in reversed(chunk):
            op_id = entry["op_id"]
            if op_id not in seen_set:
                if len(seen_ops) >= n:
                    break
                seen_set.add(op_id)
                seen_ops.append(op_id)
            if op_id in seen_set:
                key = (entry["x"], entry["y"])
                touched.setdefault(key, entry["prev_color_id"])
        cursor = chunk_start

    with _lock:
        doc = load_canvas_doc()
        size = doc["size"]
        op_id = _alloc_op_id(doc)
        entries = []
        for (x, y), prev_color_id_to_restore in touched.items():
            idx = y * size + x
            current = doc["pixels"][idx]
            if current == prev_color_id_to_restore:
                continue
            doc["pixels"][idx] = prev_color_id_to_restore
            doc["version"] += 1
            entries.append({
                "op_id": op_id, "ts": _now(), "user_id": int(acting_user_id),
                "x": x, "y": y, "color_id": prev_color_id_to_restore, "prev_color_id": current,
            })

        _save_canvas_doc(doc)
        _append_records(entries)

        if int(acting_user_id) != SYSTEM_USER_ID:
            users = _load_users()
            _touch_username(users, acting_user_id, acting_display_name)
            _save_users(users)

    return {
        "ok": True,
        "ops_reverted": len(seen_ops),
        "pixels_reverted": len(entries),
        "version": doc["version"],
    }


# ---------------------------------------------------------------------------
# One-time migration from the older per-line JSON log format, if present.
# The canvas grid itself (canvas.json) is untouched by this — only the
# history/audit log format changes, so no pixels are lost either way.
# ---------------------------------------------------------------------------

def _migrate_legacy_jsonl_if_needed() -> None:
    if not LEGACY_CHANGELOG_FILE.exists() or CHANGELOG_FILE.exists():
        return

    with _lock:
        if CHANGELOG_FILE.exists():  # re-check inside the lock
            return

        doc = load_canvas_doc()
        users = _load_users()
        op_id_map: Dict[str, int] = {}  # old string uuid -> new sequential int
        legacy = []

        with open(LEGACY_CHANGELOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    old = json.loads(line)
                except json.JSONDecodeError:
                    continue
                legacy.append(old)
        legacy.sort(key=lambda e: e.get("version", 0))

        entries = []

        def _pad_gap(ts: float):
            # The pre-OAuth revert implementation bumped the version counter
            # without writing a log line for it, leaving a gap in the old
            # log. Pad with clearly-marked placeholder records (op_id=0,
            # reserved/never issued by _alloc_op_id) purely so later
            # offset-based lookups (version - 1 == record index) stay
            # correct — we have no way to recover what those old reverts
            # actually touched.
            entries.append({
                "op_id": 0, "ts": ts, "user_id": SYSTEM_USER_ID,
                "x": 0, "y": 0, "color_id": 0, "prev_color_id": 0,
            })

        expected_next_version = 1
        for old in legacy:
            v = old.get("version", expected_next_version)
            last_ts = old.get("ts", _now())
            while expected_next_version < v:
                _pad_gap(last_ts)
                expected_next_version += 1

            old_op = old.get("op_id", "")
            if old_op not in op_id_map:
                op_id_map[old_op] = _alloc_op_id(doc)

            user_id_str = str(old.get("user_id", SYSTEM_USER_ID))
            try:
                user_id_int = int(user_id_str)
            except ValueError:
                user_id_int = SYSTEM_USER_ID  # legacy non-numeric ids (e.g. old test/anon ids)

            if old.get("display_name") and user_id_int != SYSTEM_USER_ID:
                _touch_username(users, user_id_str, old["display_name"])

            entries.append({
                "op_id": op_id_map[old_op],
                "ts": old.get("ts", _now()),
                "user_id": user_id_int,
                "x": old["x"], "y": old["y"],
                "color_id": old["color_id"],
                "prev_color_id": old.get("prev_color_id", 0),
            })
            expected_next_version += 1

        # Trailing gap: an old revert happened after the last logged entry.
        while expected_next_version <= doc["version"]:
            _pad_gap(_now())
            expected_next_version += 1

        _append_records(entries)
        _save_canvas_doc(doc)  # persists the advanced next_op_id counter
        _save_users(users)
        LEGACY_CHANGELOG_FILE.rename(LEGACY_CHANGELOG_FILE.with_suffix(".jsonl.migrated"))
        print(f"Migrated {len(entries)} legacy log entries to the binary format "
              f"({LEGACY_CHANGELOG_FILE.name} -> {LEGACY_CHANGELOG_FILE.name}.migrated)")


_migrate_legacy_jsonl_if_needed()
