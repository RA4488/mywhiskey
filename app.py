"""
Whiskey Recommendation App - Multi-user, expanded bottle metadata
Run locally:   streamlit run app.py
Deploy free:   push to GitHub -> share.streamlit.io

Secrets needed (Streamlit Cloud Settings -> Secrets):
    anthropic_api_key = "sk-ant-..."
    signup_code = "your-shared-invite-code"
    admin_username = "your-username"   # gets the Admin tab for password resets
"""

import base64
import hashlib
import json
import math
import random
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Optional

import streamlit as st


# -----------------------------
# Data Models
# -----------------------------

@dataclass
class Bottle:
    id: str
    name: str
    type: str
    proof: Optional[float]
    world_tasting_notes: List[str]
    my_tasting_notes: List[str]
    fill_percent: float
    sealed: bool
    quantity: int
    private_pick: bool = False
    pick_group: str = ""
    size_ml: int = 750


@dataclass
class Preferences:
    liked_profiles: List[str] = field(default_factory=list)
    preferred_proof_min: Optional[float] = None
    preferred_proof_max: Optional[float] = None
    favorite_bottles: List[str] = field(default_factory=list)


# -----------------------------
# -----------------------------
# Persistence — Supabase (with JSON fallback for local dev)
# -----------------------------

DATA_FILE = Path("data.json")


def _get_supabase_client():
    """Lazily build a Supabase client from Streamlit secrets. Returns None if not configured."""
    url = st.secrets.get("supabase_url")
    key = st.secrets.get("supabase_key")
    if not url or not key:
        return None
    # Cache per-session so we don't rebuild on every helper call
    if "_supabase_client" not in st.session_state:
        from supabase import create_client
        st.session_state._supabase_client = create_client(url, key)
    return st.session_state._supabase_client


def _is_supabase_enabled() -> bool:
    return _get_supabase_client() is not None


def _bottle_row_to_dict(row: Dict) -> Dict:
    """Convert a Supabase bottle row into the in-memory dict shape used by the app."""
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "proof": row.get("proof"),
        "world_tasting_notes": row.get("world_tasting_notes") or [],
        "my_tasting_notes": row.get("my_tasting_notes") or [],
        "fill_percent": float(row.get("fill_percent", 100)),
        "sealed": bool(row.get("sealed", True)),
        "quantity": int(row.get("quantity", 1)),
        "private_pick": bool(row.get("private_pick", False)),
        "pick_group": row.get("pick_group", "") or "",
        "size_ml": int(row.get("size_ml", 750)),
    }


def _bottle_dict_to_row(b: Dict, owner: str) -> Dict:
    return {
        "id": b["id"],
        "owner": owner,
        "name": b["name"],
        "type": b["type"],
        "proof": b.get("proof"),
        "world_tasting_notes": b.get("world_tasting_notes", []),
        "my_tasting_notes": b.get("my_tasting_notes", []),
        "fill_percent": float(b.get("fill_percent", 100)),
        "sealed": bool(b.get("sealed", True)),
        "quantity": int(b.get("quantity", 1)),
        "private_pick": bool(b.get("private_pick", False)),
        "pick_group": b.get("pick_group", "") or "",
        "size_ml": int(b.get("size_ml", 750)),
    }


def _trade_row_to_dict(row: Dict) -> Dict:
    return {
        "id": row["id"],
        "from_user": row["from_user"],
        "to_user": row["to_user"],
        "status": row["status"],
        "offered": row.get("offered") or [],
        "requested": row.get("requested") or [],
        "message": row.get("message", "") or "",
        "counter_to_id": row.get("counter_to_id"),
        "from_shipped": bool(row.get("from_shipped", False)),
        "from_received": bool(row.get("from_received", False)),
        "to_shipped": bool(row.get("to_shipped", False)),
        "to_received": bool(row.get("to_received", False)),
        "history": row.get("history") or [],
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }


def _trade_dict_to_row(t: Dict) -> Dict:
    """Strip server-managed columns; the rest map 1:1."""
    out = {k: v for k, v in t.items() if k not in ("created_at",)}
    # updated_at: let our code drive it (we already set it on every state change)
    return out


def _load_from_supabase() -> Dict:
    sb = _get_supabase_client()
    db = {"users": {}, "trades": []}

    # Users
    user_rows = sb.table("app_users").select("*").execute().data or []
    for u in user_rows:
        db["users"][u["username"]] = {
            "display_name": u.get("display_name", u["username"]),
            "password_hash": u["password_hash"],
            "salt": u["salt"],
            "preferences": u.get("preferences") or {},
            "recent_ids": u.get("recent_ids") or [],
            "pour_log": u.get("pour_log") or [],
            "bottles": [],
        }

    # Bottles
    bottle_rows = sb.table("bottles").select("*").execute().data or []
    for row in bottle_rows:
        owner = row.get("owner")
        if owner in db["users"]:
            db["users"][owner]["bottles"].append(_bottle_row_to_dict(row))

    # Trades
    trade_rows = sb.table("trades").select("*").execute().data or []
    db["trades"] = [_trade_row_to_dict(r) for r in trade_rows]

    return db


def _save_to_supabase(db: Dict) -> None:
    """Upsert users + bottles + trades. Deletes removed bottles by syncing per-user lists."""
    sb = _get_supabase_client()

    # ---- Users ----
    user_payload = []
    for username, info in db["users"].items():
        user_payload.append({
            "username": username,
            "display_name": info.get("display_name", username),
            "password_hash": info["password_hash"],
            "salt": info["salt"],
            "preferences": info.get("preferences") or {},
            "recent_ids": info.get("recent_ids") or [],
            "pour_log": info.get("pour_log") or [],
        })
    if user_payload:
        sb.table("app_users").upsert(user_payload, on_conflict="username").execute()

    # ---- Bottles ----
    # Compute the full set of bottle IDs that should exist across all users,
    # delete any DB rows not in that set, then upsert all current bottles.
    all_current_bottles = []
    all_current_ids = set()
    for username, info in db["users"].items():
        for b in info.get("bottles", []):
            all_current_bottles.append(_bottle_dict_to_row(b, username))
            all_current_ids.add(b["id"])

    existing = sb.table("bottles").select("id").execute().data or []
    to_delete = [r["id"] for r in existing if r["id"] not in all_current_ids]
    if to_delete:
        sb.table("bottles").delete().in_("id", to_delete).execute()
    if all_current_bottles:
        sb.table("bottles").upsert(all_current_bottles, on_conflict="id").execute()

    # ---- Trades ----
    trades = db.get("trades", [])
    if trades:
        payload = [_trade_dict_to_row(t) for t in trades]
        sb.table("trades").upsert(payload, on_conflict="id").execute()


def _load_from_json() -> Dict:
    """Original JSON-file loader, kept for local dev / fallback."""
    if not DATA_FILE.exists():
        return {"users": {}, "trades": []}
    try:
        with open(DATA_FILE) as f:
            db = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"users": {}, "trades": []}
    if "users" not in db or not isinstance(db.get("users"), dict):
        db = {"users": {}}
    db.setdefault("trades", [])
    return db


def _save_to_json(db: Dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(db, f, indent=2)


def load_db() -> Dict:
    if _is_supabase_enabled():
        db = _load_from_supabase()
    else:
        db = _load_from_json()

    # One-time username normalization: lowercase keys, preserve original
    # capitalization as display_name. Skips any collision (first one wins).
    migrated = False
    new_users = {}
    for original_key, info in db["users"].items():
        new_key = original_key.strip().lower()
        existing_display = info.get("display_name", original_key)
        cleaned_display = existing_display.strip()
        if "display_name" not in info or cleaned_display != existing_display:
            info["display_name"] = cleaned_display
            migrated = True
        if new_key not in new_users:
            new_users[new_key] = info
            if new_key != original_key:
                migrated = True
    if migrated:
        db["users"] = new_users
        save_db(db)

    db.setdefault("trades", [])
    return db


def save_db(db: Dict) -> None:
    if _is_supabase_enabled():
        _save_to_supabase(db)
    else:
        _save_to_json(db)


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def normalize_username(username: str) -> str:
    """Usernames are stored and compared in lowercase, trimmed."""
    return username.strip().lower()


def create_user(db: Dict, username: str, password: str) -> None:
    salt = secrets.token_hex(16)
    key = normalize_username(username)
    db["users"][key] = {
        "display_name": username.strip(),
        "password_hash": hash_password(password, salt),
        "salt": salt,
        "bottles": [],
        "preferences": {},
        "recent_ids": [],
        "pour_log": [],  # [{ts, bottle_id, oz, vibe}]
    }
    save_db(db)


def verify_user(db: Dict, username: str, password: str) -> bool:
    user = db["users"].get(normalize_username(username))
    if not user:
        return False
    return hash_password(password, user["salt"]) == user["password_hash"]


def set_password(db: Dict, username: str, new_password: str) -> bool:
    """Reset a user's password. Returns True if user exists, False otherwise."""
    user = db["users"].get(normalize_username(username))
    if not user:
        return False
    new_salt = secrets.token_hex(16)
    user["salt"] = new_salt
    user["password_hash"] = hash_password(new_password, new_salt)
    save_db(db)
    return True


def display_name_for(db: Dict, username: str) -> str:
    """Return the user's saved display name, falling back to the lookup key."""
    key = normalize_username(username)
    return db["users"].get(key, {}).get("display_name", key)


# -----------------------------
# Persistent sessions ("Remember me")
# -----------------------------

SESSION_DAYS = 30
SESSION_COOKIE_NAME = "whiskey_session"


def _hash_token(token: str) -> str:
    """SHA-256 hash of a session token. Only the hash is stored server-side."""
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(username: str) -> Optional[str]:
    """Issue a new session token for a user. Returns the raw token (for the cookie),
    or None if persistent sessions aren't available (e.g. local JSON dev mode)."""
    if not _is_supabase_enabled():
        return None  # JSON-file mode doesn't support persistent sessions
    sb = _get_supabase_client()
    raw_token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    try:
        sb.table("sessions").insert({
            "token_hash": _hash_token(raw_token),
            "username": normalize_username(username),
            "expires_at": expires_at,
        }).execute()
        return raw_token
    except Exception:
        return None


def lookup_session(token: str) -> Optional[str]:
    """Look up a session token. Returns the username if valid and not expired, else None."""
    if not token or not _is_supabase_enabled():
        return None
    sb = _get_supabase_client()
    try:
        result = sb.table("sessions").select("username,expires_at").eq(
            "token_hash", _hash_token(token)
        ).execute()
    except Exception:
        return None
    rows = result.data or []
    if not rows:
        return None
    row = rows[0]
    # Check expiry
    try:
        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires:
            # Expired — clean it up so the table doesn't bloat
            try:
                sb.table("sessions").delete().eq("token_hash", _hash_token(token)).execute()
            except Exception:
                pass
            return None
    except Exception:
        return None
    return row["username"]


def revoke_session(token: str) -> None:
    """Delete a single session. Used on normal sign-out."""
    if not token or not _is_supabase_enabled():
        return
    sb = _get_supabase_client()
    try:
        sb.table("sessions").delete().eq("token_hash", _hash_token(token)).execute()
    except Exception:
        pass


def revoke_all_sessions(username: str) -> int:
    """Delete every session for a user. Returns the number of sessions revoked.
    Used by 'Sign out from all devices'."""
    if not _is_supabase_enabled():
        return 0
    sb = _get_supabase_client()
    try:
        result = sb.table("sessions").delete().eq(
            "username", normalize_username(username)
        ).execute()
        return len(result.data or [])
    except Exception:
        return 0


def get_cookie_controller():
    """Lazily build the cookie controller. Returns None if the package isn't installed."""
    if "_cookie_controller" in st.session_state:
        return st.session_state._cookie_controller
    try:
        from streamlit_cookies_controller import CookieController
        ctrl = CookieController()
        st.session_state._cookie_controller = ctrl
        return ctrl
    except ImportError:
        return None


def normalize_bottle_record(b: Dict) -> Dict:
    """Backfill fields on bottles created under older schemas so the UI doesn't crash."""
    b.setdefault("quantity", 1)
    b.setdefault("private_pick", b.get("store_pick", False))
    b.setdefault("pick_group", "")
    # Notes: old schema used "tasting_notes" for everything
    if "world_tasting_notes" not in b and "my_tasting_notes" not in b:
        legacy = b.get("tasting_notes", [])
        b["world_tasting_notes"] = []
        b["my_tasting_notes"] = legacy if isinstance(legacy, list) else []
    b.setdefault("world_tasting_notes", [])
    b.setdefault("my_tasting_notes", [])
    # Sealed is the inverse of "opened" in the old schema
    if "sealed" not in b:
        b["sealed"] = not b.get("opened", False)
    b.setdefault("fill_percent", 100.0)
    b.setdefault("size_ml", 750)
    return b


def get_user_bottles(db: Dict, username: str) -> List[Bottle]:
    key = normalize_username(username)
    raw = db["users"][key].get("bottles", [])
    bottles = []
    for r in raw:
        nb = normalize_bottle_record(r)
        bottles.append(Bottle(
            id=nb["id"],
            name=nb["name"],
            type=nb["type"],
            proof=nb.get("proof"),
            world_tasting_notes=nb["world_tasting_notes"],
            my_tasting_notes=nb["my_tasting_notes"],
            fill_percent=nb["fill_percent"],
            sealed=nb["sealed"],
            quantity=nb["quantity"],
            private_pick=nb["private_pick"],
            pick_group=nb["pick_group"],
            size_ml=nb["size_ml"],
        ))
    return bottles


def get_user_prefs(db: Dict, username: str) -> Preferences:
    p = db["users"][normalize_username(username)].get("preferences", {})
    return Preferences(
        liked_profiles=p.get("liked_profiles", []),
        preferred_proof_min=p.get("preferred_proof_min"),
        preferred_proof_max=p.get("preferred_proof_max"),
        favorite_bottles=p.get("favorite_bottles", []),
    )


def list_other_users(db: Dict, current_user: str) -> List[str]:
    """Return display names of users other than the current one, sorted."""
    current_key = normalize_username(current_user)
    return sorted(
        info.get("display_name", key)
        for key, info in db["users"].items()
        if key != current_key
    )


# -----------------------------
# Trades
# -----------------------------

def get_trades(db: Dict) -> List[Dict]:
    """All trades. Trades live at the top level, not under users."""
    return db.setdefault("trades", [])


def trades_for_user(db: Dict, username: str, statuses: Optional[List[str]] = None) -> List[Dict]:
    """Trades where the user is sender or recipient, optionally filtered by status."""
    key = normalize_username(username)
    out = []
    for t in get_trades(db):
        if t.get("from_user") == key or t.get("to_user") == key:
            if statuses is None or t.get("status") in statuses:
                out.append(t)
    return out


def sealed_bottles_for_user(db: Dict, username: str) -> List[Bottle]:
    """All sealed, in-stock bottles for a user — the only ones eligible for trade."""
    return [b for b in get_user_bottles(db, username) if b.sealed and b.quantity > 0]


def create_trade(
    db: Dict,
    from_user: str,
    to_user: str,
    offered: List[Dict],
    requested: List[Dict],
    message: str = "",
    counter_to_id: Optional[str] = None,
) -> Dict:
    """
    offered/requested are lists of {bottle_id, bottle_name, quantity}.
    'offered' come from from_user's shelf; 'requested' from to_user's shelf.
    """
    from_key = normalize_username(from_user)
    to_key = normalize_username(to_user)
    now = datetime.now(timezone.utc).isoformat()
    trade = {
        "id": f"t_{int(random.random() * 1_000_000_000)}",
        "from_user": from_key,
        "to_user": to_key,
        "status": "pending",
        "offered": offered,
        "requested": requested,
        "message": message.strip(),
        "created_at": now,
        "updated_at": now,
        "counter_to_id": counter_to_id,
        "history": [{"ts": now, "actor": from_key, "action": "proposed"}],
    }
    get_trades(db).append(trade)
    save_db(db)
    return trade


def _validate_transfer(db: Dict, items: List[Dict], owner: str) -> Optional[str]:
    """Make sure all referenced bottles still exist, are sealed, and have quantity available."""
    owner_key = normalize_username(owner)
    bottles = {b["id"]: b for b in db["users"][owner_key].get("bottles", [])}
    for item in items:
        bid = item["bottle_id"]
        qty = int(item.get("quantity", 1))
        bot = bottles.get(bid)
        if not bot:
            return f"{owner} no longer has '{item['bottle_name']}' on their shelf."
        if not bot.get("sealed", True):
            return f"{owner}'s '{item['bottle_name']}' is no longer sealed."
        if int(bot.get("quantity", 0)) < qty:
            return f"{owner} doesn't have enough of '{item['bottle_name']}' anymore."
    return None


def _transfer_bottles(db: Dict, items: List[Dict], from_user: str, to_user: str) -> None:
    """Move bottles from one user to another. Decrements sender, adds/increments recipient."""
    from_key = normalize_username(from_user)
    to_key = normalize_username(to_user)
    sender_bottles = db["users"][from_key]["bottles"]
    recipient_bottles = db["users"][to_key]["bottles"]

    for item in items:
        bid = item["bottle_id"]
        qty = int(item.get("quantity", 1))

        # Find sender's bottle
        sender_bot = None
        for b in sender_bottles:
            if b["id"] == bid:
                sender_bot = b
                break
        if not sender_bot:
            continue  # validation should have caught this

        # Decrement sender (don't delete record — preserves their history)
        sender_bot["quantity"] = max(0, int(sender_bot.get("quantity", 0)) - qty)

        # Add to recipient: if they already have a sealed bottle with the same
        # name, increment quantity; otherwise create a fresh record (their copy)
        existing = None
        for rb in recipient_bottles:
            if (
                rb.get("name", "").lower().strip() == sender_bot.get("name", "").lower().strip()
                and rb.get("sealed", True)
            ):
                existing = rb
                break
        if existing:
            existing["quantity"] = int(existing.get("quantity", 0)) + qty
        else:
            new_id = f"b_{int(random.random() * 1_000_000)}"
            recipient_bottles.append({
                "id": new_id,
                "name": sender_bot.get("name", ""),
                "type": sender_bot.get("type", "other"),
                "proof": sender_bot.get("proof"),
                "world_tasting_notes": list(sender_bot.get("world_tasting_notes", [])),
                "my_tasting_notes": [],  # recipient hasn't tasted it yet
                "fill_percent": 100.0,
                "sealed": True,
                "quantity": qty,
                "private_pick": sender_bot.get("private_pick", False),
                "pick_group": sender_bot.get("pick_group", ""),
                "size_ml": sender_bot.get("size_ml", 750),
            })


def accept_trade(db: Dict, trade_id: str, actor: str) -> Optional[str]:
    """Accept a pending trade. This only AGREES to the trade — bottles don't
    move yet. Both parties must confirm shipped + received before inventories
    update. Returns error string or None on success."""
    actor_key = normalize_username(actor)
    trade = next((t for t in get_trades(db) if t["id"] == trade_id), None)
    if not trade:
        return "That trade doesn't exist anymore."
    if trade["status"] != "pending":
        return f"This trade is already {trade['status']}."
    if trade["to_user"] != actor_key:
        return "Only the recipient can accept this trade."

    # Validate both sides at agreement time so we don't agree to an impossible swap
    err = _validate_transfer(db, trade["offered"], trade["from_user"])
    if err:
        return err
    err = _validate_transfer(db, trade["requested"], trade["to_user"])
    if err:
        return err

    now = datetime.now(timezone.utc).isoformat()
    trade["status"] = "accepted"
    trade["updated_at"] = now
    # Initialize handoff flags. "from" = the person who originally proposed the trade.
    trade.setdefault("from_shipped", False)
    trade.setdefault("from_received", False)
    trade.setdefault("to_shipped", False)
    trade.setdefault("to_received", False)
    trade["history"].append({"ts": now, "actor": actor_key, "action": "accepted"})
    save_db(db)
    return None


def mark_shipped(db: Dict, trade_id: str, actor: str) -> Optional[str]:
    """Mark that the actor has shipped/handed off their side of the trade."""
    actor_key = normalize_username(actor)
    trade = next((t for t in get_trades(db) if t["id"] == trade_id), None)
    if not trade:
        return "That trade doesn't exist anymore."
    if trade["status"] not in ("accepted",):
        return f"Can't mark shipped — trade is {trade['status']}."

    now = datetime.now(timezone.utc).isoformat()
    if actor_key == trade["from_user"]:
        if trade.get("from_shipped"):
            return "You already marked this as shipped."
        trade["from_shipped"] = True
        trade["history"].append({"ts": now, "actor": actor_key, "action": "marked_shipped"})
    elif actor_key == trade["to_user"]:
        if trade.get("to_shipped"):
            return "You already marked this as shipped."
        trade["to_shipped"] = True
        trade["history"].append({"ts": now, "actor": actor_key, "action": "marked_shipped"})
    else:
        return "You're not part of this trade."

    trade["updated_at"] = now
    _maybe_complete_trade(db, trade)
    save_db(db)
    return None


def mark_received(db: Dict, trade_id: str, actor: str) -> Optional[str]:
    """Mark that the actor has received the bottles owed to them."""
    actor_key = normalize_username(actor)
    trade = next((t for t in get_trades(db) if t["id"] == trade_id), None)
    if not trade:
        return "That trade doesn't exist anymore."
    if trade["status"] != "accepted":
        return f"Can't mark received — trade is {trade['status']}."

    now = datetime.now(timezone.utc).isoformat()
    # The recipient receives what the OTHER party shipped.
    if actor_key == trade["from_user"]:
        # Sender receives the recipient's bottles, which require to_shipped first
        if not trade.get("to_shipped"):
            return f"{display_name_for(db, trade['to_user'])} hasn't marked their bottles as shipped yet."
        if trade.get("from_received"):
            return "You already confirmed received."
        trade["from_received"] = True
        trade["history"].append({"ts": now, "actor": actor_key, "action": "marked_received"})
    elif actor_key == trade["to_user"]:
        if not trade.get("from_shipped"):
            return f"{display_name_for(db, trade['from_user'])} hasn't marked their bottles as shipped yet."
        if trade.get("to_received"):
            return "You already confirmed received."
        trade["to_received"] = True
        trade["history"].append({"ts": now, "actor": actor_key, "action": "marked_received"})
    else:
        return "You're not part of this trade."

    trade["updated_at"] = now
    _maybe_complete_trade(db, trade)
    save_db(db)
    return None


def _maybe_complete_trade(db: Dict, trade: Dict) -> None:
    """If both sides have confirmed received, transfer bottles and complete the trade."""
    if not (trade.get("from_received") and trade.get("to_received")):
        return
    # Re-validate one final time — inventories may have shifted since accept
    err = _validate_transfer(db, trade["offered"], trade["from_user"])
    if err:
        # Stuck — log to history but don't auto-resolve
        trade["history"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": "system",
            "action": "complete_failed",
            "note": err,
        })
        return
    err = _validate_transfer(db, trade["requested"], trade["to_user"])
    if err:
        trade["history"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": "system",
            "action": "complete_failed",
            "note": err,
        })
        return

    _transfer_bottles(db, trade["offered"], trade["from_user"], trade["to_user"])
    _transfer_bottles(db, trade["requested"], trade["to_user"], trade["from_user"])
    now = datetime.now(timezone.utc).isoformat()
    trade["status"] = "completed"
    trade["updated_at"] = now
    trade["history"].append({"ts": now, "actor": "system", "action": "completed"})


def abandon_trade(db: Dict, trade_id: str, actor: str) -> Optional[str]:
    """Either party can abandon an accepted trade that never happened.
    No bottles move. Trade goes to 'abandoned' status."""
    actor_key = normalize_username(actor)
    trade = next((t for t in get_trades(db) if t["id"] == trade_id), None)
    if not trade:
        return "That trade doesn't exist anymore."
    if trade["status"] != "accepted":
        return f"Can't abandon — trade is {trade['status']}."
    if actor_key not in (trade["from_user"], trade["to_user"]):
        return "You're not part of this trade."
    now = datetime.now(timezone.utc).isoformat()
    trade["status"] = "abandoned"
    trade["updated_at"] = now
    trade["history"].append({"ts": now, "actor": actor_key, "action": "abandoned"})
    save_db(db)
    return None


def decline_trade(db: Dict, trade_id: str, actor: str) -> Optional[str]:
    actor_key = normalize_username(actor)
    trade = next((t for t in get_trades(db) if t["id"] == trade_id), None)
    if not trade:
        return "That trade doesn't exist anymore."
    if trade["status"] != "pending":
        return f"This trade is already {trade['status']}."
    if trade["to_user"] != actor_key:
        return "Only the recipient can decline this trade."
    now = datetime.now(timezone.utc).isoformat()
    trade["status"] = "declined"
    trade["updated_at"] = now
    trade["history"].append({"ts": now, "actor": actor_key, "action": "declined"})
    save_db(db)
    return None


def cancel_trade(db: Dict, trade_id: str, actor: str) -> Optional[str]:
    actor_key = normalize_username(actor)
    trade = next((t for t in get_trades(db) if t["id"] == trade_id), None)
    if not trade:
        return "That trade doesn't exist anymore."
    if trade["status"] != "pending":
        return f"This trade is already {trade['status']}."
    if trade["from_user"] != actor_key:
        return "Only the sender can cancel this trade."
    now = datetime.now(timezone.utc).isoformat()
    trade["status"] = "canceled"
    trade["updated_at"] = now
    trade["history"].append({"ts": now, "actor": actor_key, "action": "canceled"})
    save_db(db)
    return None


def counter_trade(
    db: Dict,
    original_trade_id: str,
    actor: str,
    new_offered: List[Dict],
    new_requested: List[Dict],
    message: str = "",
) -> Optional[str]:
    """
    Recipient counters: original is closed (status=countered), new pending trade
    is created from recipient back to original sender (offered/requested swap perspective).
    """
    actor_key = normalize_username(actor)
    trade = next((t for t in get_trades(db) if t["id"] == original_trade_id), None)
    if not trade:
        return "That trade doesn't exist anymore."
    if trade["status"] != "pending":
        return f"This trade is already {trade['status']}."
    if trade["to_user"] != actor_key:
        return "Only the recipient can counter this trade."

    now = datetime.now(timezone.utc).isoformat()
    trade["status"] = "countered"
    trade["updated_at"] = now
    trade["history"].append({"ts": now, "actor": actor_key, "action": "countered"})

    # New trade from recipient (now sender) back to original sender (now recipient).
    # new_offered = bottles from actor's shelf
    # new_requested = bottles from original sender's shelf
    create_trade(
        db,
        from_user=actor_key,
        to_user=trade["from_user"],
        offered=new_offered,
        requested=new_requested,
        message=message,
        counter_to_id=original_trade_id,
    )
    save_db(db)
    return None


# 1 mL = 0.033814 fl oz
ML_PER_OZ = 29.5735


def pour_to_fill_drop(pour_oz: float, size_ml: int) -> float:
    """Convert a pour in ounces to a fill-percent drop for a bottle of a given size."""
    if size_ml <= 0:
        return 0.0
    pour_ml = pour_oz * ML_PER_OZ
    return (pour_ml / size_ml) * 100


def log_pour(
    db: Dict,
    username: str,
    bottle_id: str,
    pour_oz: float,
    vibe: Optional[str] = None,
) -> None:
    """Apply a pour: drop fill, mark unsealed, push to recent_ids, append to pour_log."""
    user_key = normalize_username(username)
    user_record = db["users"][user_key]

    # Recent IDs (legacy novelty signal — keep for backward compat)
    recent_ids = user_record.get("recent_ids", [])
    recent_ids = ([bottle_id] + [x for x in recent_ids if x != bottle_id])[:10]
    user_record["recent_ids"] = recent_ids

    # Full pour log (Phase 1: data foundation for learning)
    pour_log = user_record.setdefault("pour_log", [])
    pour_log.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "bottle_id": bottle_id,
        "oz": float(pour_oz),
        "vibe": vibe,
    })
    # Cap log at 1000 entries to keep JSON manageable
    if len(pour_log) > 1000:
        del pour_log[: len(pour_log) - 1000]

    # Update the bottle itself
    for bot in user_record["bottles"]:
        if bot["id"] == bottle_id:
            size_ml = bot.get("size_ml", 750)
            drop = pour_to_fill_drop(pour_oz, size_ml)
            bot["sealed"] = False
            bot["fill_percent"] = max(0, bot["fill_percent"] - drop)
            break

    save_db(db)


# -----------------------------
# Phase 2: Learned affinity from pour history
# -----------------------------

def get_pour_log(db: Dict, username: str) -> List[Dict]:
    user_key = normalize_username(username)
    return db["users"][user_key].get("pour_log", [])


def _parse_ts(ts_str: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def compute_affinity_scores(pour_log: List[Dict], half_life_days: float = 60.0) -> Dict[str, float]:
    """
    0-1 affinity score per bottle from pour history.
    Combines pour count, pour size, and recency (older pours decay exponentially).
    Normalized so the most-poured bottle is ~1.0; bottles never poured aren't included.
    """
    if not pour_log:
        return {}

    now = datetime.now(timezone.utc)
    raw: Dict[str, float] = {}

    for entry in pour_log:
        bid = entry.get("bottle_id")
        oz = float(entry.get("oz", 1.0))
        ts = _parse_ts(entry.get("ts", "")) if entry.get("ts") else None
        if not bid:
            continue

        if ts:
            days_old = max(0.0, (now - ts).total_seconds() / 86400)
            weight = 0.5 ** (days_old / half_life_days)
        else:
            weight = 0.5  # unknown age

        raw[bid] = raw.get(bid, 0.0) + (oz * weight)

    if not raw:
        return {}

    peak = max(raw.values())
    if peak <= 0:
        return {}
    return {bid: round(v / peak, 4) for bid, v in raw.items()}


def affinity_signal(bottle: Bottle, affinity: Dict[str, float]) -> float:
    """Map raw affinity to a recommendation signal. 0.5 = no data."""
    return affinity.get(bottle.id, 0.5)


def days_since_last_pour(pour_log: List[Dict]) -> Dict[str, Optional[int]]:
    """
    For each bottle that's ever been poured, return days since the last pour.
    Bottles never poured are absent from the result.
    """
    now = datetime.now(timezone.utc)
    last_seen: Dict[str, datetime] = {}
    for entry in pour_log:
        bid = entry.get("bottle_id")
        ts = _parse_ts(entry.get("ts", "")) if entry.get("ts") else None
        if not bid or not ts:
            continue
        if bid not in last_seen or ts > last_seen[bid]:
            last_seen[bid] = ts
    return {
        bid: int((now - ts).total_seconds() // 86400)
        for bid, ts in last_seen.items()
    }


# --- Search & sort helpers ---

def bottle_search_haystack(b: Bottle) -> str:
    """All searchable text for a bottle, joined into one lowercase string."""
    parts = [
        b.name,
        b.type,
        b.pick_group,
        " ".join(b.world_tasting_notes),
        " ".join(b.my_tasting_notes),
    ]
    return " ".join(parts).lower()


def filter_and_sort_bottles(
    bottles: List[Bottle],
    search_query: str,
    sort_by: str,
    show_zero: bool,
    quick_filters: List[str],
) -> List[Bottle]:
    """Apply search, quick filters, out-of-stock toggle, and sort."""
    result = bottles

    # Quantity-based filter (out of stock)
    if not show_zero:
        result = [b for b in result if b.quantity > 0]

    # Quick filters (chips). Stackable — all must match.
    if "Sealed only" in quick_filters:
        result = [b for b in result if b.sealed]
    if "Open only" in quick_filters:
        result = [b for b in result if not b.sealed]
    if "Running low" in quick_filters:
        result = [b for b in result if b.fill_percent < 40 and b.quantity > 0]
    if "Private picks" in quick_filters:
        result = [b for b in result if b.private_pick]

    # Search across multiple fields
    q = search_query.strip().lower()
    if q:
        # Split on whitespace; every term must appear somewhere
        terms = q.split()
        result = [b for b in result if all(t in bottle_search_haystack(b) for t in terms)]

    # Sort
    if sort_by == "Name (A–Z)":
        result = sorted(result, key=lambda b: b.name.lower())
    elif sort_by == "Recently added":
        # Bottle IDs are b_<random>; we don't store add-time, so fall back to
        # reverse insertion order, which is the order they appear in the user's
        # bottles list. This is a "good enough" approximation.
        index_map = {b.id: i for i, b in enumerate(bottles)}
        result = sorted(result, key=lambda b: index_map.get(b.id, 0), reverse=True)
    elif sort_by == "Fill % (low to high)":
        result = sorted(result, key=lambda b: (b.quantity <= 0, b.fill_percent))
    elif sort_by == "Proof (high to low)":
        result = sorted(result, key=lambda b: (b.proof or 0), reverse=True)
    elif sort_by == "Sealed first":
        result = sorted(result, key=lambda b: (not b.sealed, b.name.lower()))

    return result


# -----------------------------
# Vision
# -----------------------------

def identify_bottle_from_image(image_bytes: bytes, mime_type: str) -> Dict:
    from anthropic import Anthropic

    api_key = st.secrets.get("anthropic_api_key")
    if not api_key:
        raise RuntimeError("Missing anthropic_api_key in Streamlit secrets.")

    client = Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = (
        "Identify this whiskey/spirit bottle from the photo. "
        "Return ONLY a JSON object with these exact keys, no other text:\n"
        "  name: full bottle name as printed (string)\n"
        '  type: one of "bourbon", "rye", "scotch", "rum", "other" (string)\n'
        "  proof: proof number if visible or known (number or null)\n"
        "  is_sealed: true if the bottle appears unopened (intact tax strip, foil, "
        "capsule, or neck wrap visible and undamaged); false if opened (broken seal, "
        "missing capsule, visible cork or stopper exposed); null if you cannot tell.\n"
        "  estimated_fill_percent: if the bottle is opened AND you can see the liquid "
        "level through the glass, estimate the fill percentage 0-100 (number). "
        "If sealed, return 100. If you cannot see the liquid level (label covers it, "
        "opaque glass, etc.), return null.\n"
        "  is_private_pick: true if the label or any sticker indicates a private barrel "
        "selection, store pick, single barrel selection for a group, or similar (look for "
        '"selected for", "private selection", "barrel pick", "store pick", group/store '
        "names on a sticker or label addition); false otherwise.\n"
        "  pick_group: if is_private_pick is true and you can read the group/store name, "
        "return it as a string; otherwise empty string.\n"
        "  tasting_notes: array of 4-8 short tasting note keywords commonly associated "
        "with this bottle if you recognize it (e.g., [\"caramel\", \"oak\", \"vanilla\", "
        '"baking spice"]). Use lowercase single words or short phrases. Empty array if you '
        "don't recognize the specific bottle.\n"
        "  confidence: your confidence 0-1 that you identified the bottle correctly (number)\n"
        "  notes: short string explaining what you see\n"
        "If you cannot identify the bottle at all, return name as empty string."
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def detect_bottles_from_image(image_bytes: bytes, mime_type: str, source: str) -> List[Dict]:
    """
    Detect MULTIPLE bottles from a single image (a bar menu or backbar shelf).
    Returns a list of dicts, one per detected bottle:
      { name, type, proof, price, tasting_notes, confidence }
    'source' is "menu" or "shelf" — affects the prompt.
    """
    from anthropic import Anthropic

    api_key = st.secrets.get("anthropic_api_key")
    if not api_key:
        raise RuntimeError("Missing anthropic_api_key in Streamlit secrets.")

    client = Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    if source == "menu":
        instruction = (
            "This is a photo of a bar/restaurant whiskey menu. List EVERY whiskey, "
            "bourbon, rye, scotch, or other spirit you can read on the menu. "
            "Include price if visible (parse any currency, return as a number with no symbol)."
        )
    else:  # shelf
        instruction = (
            "This is a photo of a bar's backbar / bottle shelf. List EVERY whiskey, "
            "bourbon, rye, scotch, or other spirit bottle you can identify by reading "
            "the label. Skip vodkas, gins, liqueurs, and bottles you cannot read. "
            "Be conservative — if you are not sure of the name, do not include it."
        )

    prompt = (
        f"{instruction}\n\n"
        "Return ONLY a JSON object with this exact shape (no other text):\n"
        "{\n"
        '  "bottles": [\n'
        "    {\n"
        '      "name": "full bottle name as written/seen",\n'
        '      "type": "bourbon" | "rye" | "scotch" | "rum" | "other",\n'
        '      "proof": <number or null>,\n'
        '      "price": <number or null>,\n'
        '      "tasting_notes": ["caramel", "oak", ...],  // 3-6 common notes if you recognize it; empty if not\n'
        '      "confidence": <0.0-1.0>\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Tasting notes should be lowercase single words. Use only general public knowledge "
        "about commonly-known bottles for the notes — empty array if you don't recognize "
        "the specific bottle. If no bottles are detectable at all, return {\"bottles\": []}."
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    parsed = json.loads(text)
    return parsed.get("bottles", [])


# -----------------------------
# Bottle Look Up — single-bottle deep dive for buy/skip decisions
# -----------------------------

def lookup_bottle_from_image(image_bytes: bytes, mime_type: str) -> Dict:
    """Return rich detail about a single bottle from a photo, for buy-decision context."""
    from anthropic import Anthropic

    api_key = st.secrets.get("anthropic_api_key")
    if not api_key:
        raise RuntimeError("Missing anthropic_api_key in Streamlit secrets.")

    client = Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = (
        "Identify this whiskey/spirit bottle from the photo and provide a detailed "
        "lookup for someone deciding whether to buy it. Return ONLY a JSON object "
        "with these exact keys, no other text:\n"
        "  name: full bottle name as printed (string)\n"
        '  type: one of "bourbon", "rye", "scotch", "rum", "other" (string)\n'
        "  proof: proof number if visible or known (number or null)\n"
        "  age_statement: age if known (e.g. '10 year', 'NAS' for no age statement, or null)\n"
        "  distillery: distillery or brand owner if known (string or null)\n"
        "  region: region of origin if relevant (e.g. 'Kentucky', 'Islay', null)\n"
        "  mash_bill: mash bill if commonly known (e.g. 'high-rye bourbon', or null)\n"
        "  tasting_notes: array of 4-8 short tasting note keywords commonly associated "
        "with this bottle (e.g. ['caramel','oak','vanilla','baking spice']). Lowercase. "
        "Empty array if you don't recognize it.\n"
        "  description: 2-3 sentence plain-English summary of what this bottle is and "
        "what people generally think of it (string).\n"
        "  estimated_msrp_usd: typical retail MSRP in USD if you have any idea, as a "
        "number (no currency symbol). null if unknown. Be conservative — give your "
        "best estimate from training data, but only when reasonably confident.\n"
        "  msrp_confidence: how confident you are in the MSRP estimate, one of: "
        '"high", "medium", "low", "unknown".\n'
        "  allocated: true if this is widely considered an allocated/hard-to-find "
        "bottle that often sells above MSRP at retail; false otherwise; null if unknown.\n"
        "  is_private_pick: true if the label/sticker indicates a barrel pick or store "
        "selection; false otherwise.\n"
        "  pick_group: store/group name if private pick and readable; otherwise empty.\n"
        "  confidence: 0-1, how sure you are you identified the bottle correctly.\n"
        "  notes: short string explaining what you saw or couldn't read.\n"
        "If the bottle cannot be identified at all, return name as empty string."
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1200,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def fit_score_for_lookup(detected: Dict, prefs: Preferences, affinity: Dict[str, float]) -> float:
    """How well does this bottle fit the user, on a 0-1 scale.
    affinity is included so 'people similar to your favorites' boosts fit.
    """
    notes = detected.get("tasting_notes", []) or []
    proof = detected.get("proof")

    # Flavor match — if we have notes and the user has liked profiles, score
    # cosine similarity. Otherwise neutral.
    if not notes or not prefs.liked_profiles:
        flavor = 0.5
    else:
        bottle_vec = normalize(notes_to_vector(notes))
        pref_vec = normalize(notes_to_vector(prefs.liked_profiles))
        flavor = max(0.0, min(1.0, cosine_similarity(bottle_vec, pref_vec)))

    # Proof fit
    if proof is None or prefs.preferred_proof_min is None or prefs.preferred_proof_max is None:
        proof_fit = 0.5
    elif prefs.preferred_proof_min <= proof <= prefs.preferred_proof_max:
        proof_fit = 1.0
    else:
        distance = min(abs(proof - prefs.preferred_proof_min), abs(proof - prefs.preferred_proof_max))
        proof_fit = max(0.0, 1.0 - (distance / 50))

    # Affinity-based "people who like X also like Y" — find the user's top-affinity
    # bottles' notes and see if this bottle shares them. Cheap proxy.
    if affinity and prefs:
        # Skipped here; we'd need access to the user's full inventory in the
        # function. Caller can pass that in if we want this signal.
        pass

    # Weighted combination: flavor matters more than proof
    return round(flavor * 0.65 + proof_fit * 0.35, 3)


def value_score_for_lookup(price_usd: Optional[float], detected: Dict) -> Optional[float]:
    """0-1 score for value (1.0 = great deal, 0.0 = terrible markup).
    Returns None if we can't make any judgment (no price or no MSRP info).
    """
    if price_usd is None:
        return None
    msrp = detected.get("estimated_msrp_usd")
    confidence = detected.get("msrp_confidence", "unknown")
    if msrp is None or confidence == "unknown":
        return None

    ratio = price_usd / msrp  # >1 = above MSRP, <1 = below
    allocated = detected.get("allocated")

    if ratio <= 0.95:
        return 1.0  # below MSRP: great
    if ratio <= 1.10:
        return 0.85  # at MSRP: good
    if ratio <= 1.50:
        # Allocated bottles often sell at 1.5x — still "fair"; non-allocated this is a markup
        return 0.55 if allocated else 0.35
    if ratio <= 2.0:
        return 0.30 if allocated else 0.15
    return 0.10  # 2x+ MSRP — almost always a hard pass on value alone


def lookup_verdict(fit: float, value: Optional[float]) -> Dict:
    """
    Combine fit and value into a verdict.
    Returns {emoji, label, color, rationale}.
    """
    # If no price/value info, the verdict is purely about fit
    if value is None:
        if fit >= 0.70:
            return {"emoji": "🟢", "label": "BUY", "color": "#1f9d55",
                    "rationale": "Strong match for your taste."}
        if fit >= 0.45:
            return {"emoji": "🟡", "label": "YOUR CALL", "color": "#d4a017",
                    "rationale": "Decent fit — depends on what you're in the mood for."}
        return {"emoji": "🔴", "label": "SKIP", "color": "#c53030",
                "rationale": "Doesn't really match your usual style."}

    # With price, weight fit and value together
    combined = fit * 0.6 + value * 0.4

    if combined >= 0.70:
        return {"emoji": "🟢", "label": "BUY", "color": "#1f9d55",
                "rationale": "Good fit for you AND a fair price."}
    if combined >= 0.45:
        # Disambiguate: is it a fit problem or a price problem?
        if value < 0.4:
            note = "The fit is decent, but the price is steep."
        elif fit < 0.5:
            note = "Price is reasonable, but it's not really your style."
        else:
            note = "Solid bottle for you, fair price — close call."
        return {"emoji": "🟡", "label": "YOUR CALL", "color": "#d4a017", "rationale": note}
    if value < 0.3 and fit < 0.5:
        return {"emoji": "🔴", "label": "SKIP", "color": "#c53030",
                "rationale": "Not really your style and the price is rough."}
    if value < 0.3:
        return {"emoji": "🔴", "label": "SKIP", "color": "#c53030",
                "rationale": "The price is too far above MSRP for what this bottle is."}
    return {"emoji": "🔴", "label": "SKIP", "color": "#c53030",
            "rationale": "Doesn't really match your usual taste."}


# -----------------------------
# Scoring
# -----------------------------

FLAVOR_DIMENSIONS = ["oak", "caramel", "vanilla", "spice", "fruit", "sweet", "smoke", "herbal", "chocolate"]


def normalize(vec):
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def notes_to_vector(notes, weight=1.0):
    vector = [0.0] * len(FLAVOR_DIMENSIONS)
    for note in notes:
        for i, dim in enumerate(FLAVOR_DIMENSIONS):
            if dim in note.lower():
                vector[i] += weight
    return vector


def combined_bottle_vector(bottle: Bottle) -> List[float]:
    """Combine world + my notes; my notes weighted 2x when present."""
    world_vec = notes_to_vector(bottle.world_tasting_notes, weight=1.0)
    my_vec = notes_to_vector(bottle.my_tasting_notes, weight=2.0)
    combined = [w + m for w, m in zip(world_vec, my_vec)]
    return normalize(combined)


def cosine_similarity(a, b):
    return sum(x * y for x, y in zip(a, b))


def flavor_score(bottle, prefs):
    if not prefs.liked_profiles:
        return 0.5
    bottle_vec = combined_bottle_vector(bottle)
    pref_vec = normalize(notes_to_vector(prefs.liked_profiles))
    if all(v == 0 for v in bottle_vec):
        return 0.5
    return cosine_similarity(bottle_vec, pref_vec)


def proof_score(bottle, prefs):
    if bottle.proof is None or prefs.preferred_proof_min is None or prefs.preferred_proof_max is None:
        return 0.5
    if prefs.preferred_proof_min <= bottle.proof <= prefs.preferred_proof_max:
        return 1.0
    distance = min(abs(bottle.proof - prefs.preferred_proof_min), abs(bottle.proof - prefs.preferred_proof_max))
    return max(0.0, 1.0 - (distance / 50))


def fill_score(bottle):
    if bottle.fill_percent < 20: return 1.0
    if bottle.fill_percent < 50: return 0.8
    if bottle.fill_percent < 80: return 0.6
    return 0.4


def opened_score(bottle):
    return 0.3 if bottle.sealed else 1.0


def novelty_score(bottle, recent_ids):
    return 0.2 if bottle.id in recent_ids else 1.0


def build_reason(bottle, prefs):
    reasons = []
    if not bottle.sealed:
        reasons.append("already open")
    if bottle.fill_percent < 40:
        reasons.append("getting low")
    if bottle.quantity > 1:
        reasons.append(f"you have {bottle.quantity}")
    if (bottle.proof and prefs.preferred_proof_min is not None
            and prefs.preferred_proof_max is not None
            and prefs.preferred_proof_min <= bottle.proof <= prefs.preferred_proof_max):
        reasons.append("matches your preferred proof")
    all_notes = bottle.world_tasting_notes + bottle.my_tasting_notes
    if any(note in prefs.liked_profiles for note in all_notes):
        reasons.append("fits your flavor profile")
    return ", ".join(reasons) if reasons else "good match overall"


VIBES = {
    "Just a regular pour": {
        "blurb": "Something I'd reach for on any given evening.",
        "weights": {"flavor": 0.25, "proof": 0.10, "fill": 0.15, "opened": 0.10, "novelty": 0.10, "affinity": 0.30},
        "filters": {"opened_or_low": True},
    },
    "Easy sipper before bed": {
        "blurb": "Lower proof, smooth, doesn't ask much of me.",
        "weights": {"flavor": 0.25, "low_proof": 0.25, "fill": 0.10, "opened": 0.10, "novelty": 0.05, "affinity": 0.25},
        "filters": {"opened_or_low": True, "max_proof": 105},
    },
    "Sharing with company": {
        "blurb": "Crowd-pleaser. Probably not my weirdest bottle.",
        "weights": {"flavor": 0.15, "proof": 0.10, "fill": 0.10, "opened": 0.15, "novelty": 0.05, "crowd": 0.20, "affinity": 0.25},
        "filters": {"opened_or_low": True},
    },
    "Want to focus and taste": {
        # Lower affinity weight here — point is to discover, not repeat favorites
        "blurb": "Something interesting, worth paying attention to.",
        "weights": {"flavor": 0.30, "proof": 0.15, "fill": 0.10, "interesting": 0.30, "novelty": 0.15},
        "filters": {"opened_or_low": True},
    },
    "The Forgotten Ones": {
        # Custom-scored — uses days_since_last_pour rather than the weight system
        "blurb": "Bottles you haven't touched in a while. Time to bring them back.",
        "weights": {},  # ignored; uses forgotten-specific scoring
        "filters": {},  # custom — handled inline
        "forgotten": True,
    },
    "Cracking something special": {
        # Custom-scored — for celebrations, milestones, the bottle you've been saving
        "blurb": "An occasion worth remembering. The one you've been saving.",
        "weights": {},  # ignored; uses special-occasion-specific scoring
        "filters": {},  # custom — handled inline
        "special_occasion": True,
    },
    "Surprise me": {
        "blurb": "Roll the dice — pick something random.",
        "weights": {},  # ignored, uses random
        "filters": {"opened_or_low": True},
        "random": True,
    },
}


def crowd_score(bottle: Bottle) -> float:
    """Higher when bottle is approachable: lower proof, multiple bottles, not a private pick."""
    score = 0.5
    if bottle.proof and bottle.proof <= 100:
        score += 0.25
    if bottle.quantity > 1:
        score += 0.15  # if you have multiples, you don't mind sharing
    if bottle.private_pick:
        score -= 0.20  # private picks are usually saved
    return max(0.0, min(1.0, score))


def interesting_score(bottle: Bottle) -> float:
    """Higher for bottles that reward attention: private picks, higher proof, rich notes."""
    score = 0.4
    if bottle.private_pick:
        score += 0.30
    if bottle.proof and bottle.proof >= 110:
        score += 0.20
    note_count = len(bottle.world_tasting_notes) + len(bottle.my_tasting_notes)
    if note_count >= 4:
        score += 0.10
    return max(0.0, min(1.0, score))


def low_proof_score(bottle: Bottle) -> float:
    """Higher for lower-proof bottles. Peaks around 90 proof, drops above 105."""
    if bottle.proof is None:
        return 0.5
    if bottle.proof <= 90:
        return 1.0
    if bottle.proof <= 105:
        return 0.7
    if bottle.proof <= 115:
        return 0.3
    return 0.1


def _has_age_statement(bottle: Bottle) -> Optional[int]:
    """Sniff for an age in years from the bottle name (e.g. 'Eagle Rare 17 Year').
    Returns the age if found, else None. Looks for a number followed by year/yr."""
    if not bottle.name:
        return None
    m = re.search(r"\b(\d{1,2})\s*(?:year|yr|yo)\b", bottle.name, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def special_occasion_score(
    bottle: Bottle,
    affinity: Dict[str, float],
    days_since: Optional[int],
) -> float:
    """
    Score a bottle for special occasions. Higher = more worth cracking.

    Rewards: sealed bottles, private picks, age statements, high proof,
    rarely-poured bottles. Penalizes: daily drinkers (multi-quantity at low
    proof), recently-poured bottles, low affinity if you've already had it
    enough to know you don't love it.
    """
    score = 0.0

    # Sealed = the whole point of saving for a moment
    if bottle.sealed:
        score += 0.30
    else:
        # Open bottles can still be special if they're rarely poured and high-end
        score += 0.05

    # Private pick = personal, often a story attached
    if bottle.private_pick:
        score += 0.25

    # Age statement = traditionally the "good stuff"
    age = _has_age_statement(bottle)
    if age is not None:
        if age >= 15:
            score += 0.30
        elif age >= 10:
            score += 0.20
        elif age >= 6:
            score += 0.10

    # Higher proof = often the special-event bourbon (barrel proof, cask strength)
    if bottle.proof:
        if bottle.proof >= 120:
            score += 0.20
        elif bottle.proof >= 110:
            score += 0.15
        elif bottle.proof >= 100:
            score += 0.05

    # Penalize daily drinkers — multi-quantity low-proof bottles are weeknight pours
    if bottle.quantity > 1 and bottle.proof and bottle.proof < 100:
        score -= 0.20

    # Penalize bottles you've poured very recently — not "special" if you just had it
    if days_since is not None and days_since < 14:
        score -= 0.30

    # Affinity: if you've poured it a TON, it's a daily driver; subtle penalty
    aff = affinity.get(bottle.id, 0)
    if aff >= 0.85:
        score -= 0.15

    # Bottles you've never tried get a small bonus — opening something new IS the celebration
    if days_since is None and bottle.sealed:
        score += 0.10

    return max(0.0, score)


def special_occasion_reasoning_via_ai(
    bottles: List[Bottle], occasion_text: str
) -> Dict[str, str]:
    """
    Optional: call Claude once with the candidate bottles and the occasion text;
    get back a personalized one-line reason per bottle keyed by bottle id.
    Falls back gracefully if the API call fails — caller uses programmatic
    reasoning in that case.
    """
    if not occasion_text or not occasion_text.strip() or not bottles:
        return {}

    try:
        from anthropic import Anthropic
    except ImportError:
        return {}
    api_key = st.secrets.get("anthropic_api_key")
    if not api_key:
        return {}

    client = Anthropic(api_key=api_key)
    bottle_summaries = []
    for b in bottles:
        parts = [f"id={b.id}", f"name={b.name}", f"type={b.type}"]
        if b.proof:
            parts.append(f"proof={b.proof:.0f}")
        if b.private_pick and b.pick_group:
            parts.append(f"private_pick=({b.pick_group})")
        elif b.private_pick:
            parts.append("private_pick=true")
        age = _has_age_statement(b)
        if age:
            parts.append(f"age={age}yr")
        if b.world_tasting_notes:
            parts.append(f"notes=[{', '.join(b.world_tasting_notes[:5])}]")
        bottle_summaries.append("  - " + " ".join(parts))

    prompt = (
        f"The user is celebrating: \"{occasion_text.strip()}\"\n\n"
        f"They're choosing from these bottles:\n"
        + "\n".join(bottle_summaries)
        + "\n\nFor each bottle, write ONE short sentence (under 18 words) explaining "
        "why it fits this occasion. Match the tone of the occasion — celebratory, "
        "reflective, intimate, etc. Don't be generic. Reference what makes the bottle "
        "special (age, private pick, proof, etc.) AND the occasion together.\n\n"
        "Return ONLY a JSON object mapping bottle id to the sentence, like:\n"
        '{"b_123": "Sentence here.", "b_456": "Another sentence."}'
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except Exception:
        return {}


def recommend_bottles(
    inventory: List[Bottle],
    prefs: Preferences,
    vibe: str,
    recent_ids: List[str],
    top_n: int = 3,
    people_count: int = 1,
    affinity: Optional[Dict[str, float]] = None,
    pour_log: Optional[List[Dict]] = None,
    occasion_text: str = "",
) -> List[Dict]:
    """Vibe-driven recommendation. Affinity (learned from pour history) is layered in."""
    vibe_config = VIBES.get(vibe, VIBES["Just a regular pour"])
    filters = vibe_config["filters"]
    weights = vibe_config["weights"]
    affinity = affinity or {}
    pour_log = pour_log or []

    # Always exclude empty bottles
    candidates = [b for b in inventory if b.quantity > 0]

    # --- Special-case: The Forgotten Ones ---
    if vibe_config.get("forgotten"):
        days_since = days_since_last_pour(pour_log)
        NEGLECT_DAYS = 60        # threshold for "starting to be forgotten"
        DEEP_DAYS = 120          # threshold for "deeply forgotten"

        # Bottles never poured are also "forgotten" — but only if you've owned
        # them long enough that you'd reasonably have tried them by now. Since
        # we don't track add date yet, treat all never-poured bottles as eligible
        # but rank them lower than confirmed-neglected open bottles.
        eligible = []
        for b in candidates:
            d = days_since.get(b.id)  # None if never poured
            if d is None:
                # Never poured. Eligible but lower-ranked.
                eligible.append((b, -1))
            elif d >= NEGLECT_DAYS:
                eligible.append((b, d))

        if not eligible:
            # Nothing qualifies — explain by returning empty
            return []

        scored = []
        for b, d in eligible:
            score = 0.0
            if d >= DEEP_DAYS:
                score += 1.0  # baseline: deeply forgotten
            elif d >= NEGLECT_DAYS:
                # Linear ramp from 0.5 at 60 days to 1.0 at 120 days
                score += 0.5 + 0.5 * ((d - NEGLECT_DAYS) / (DEEP_DAYS - NEGLECT_DAYS))
            else:
                # Never-poured fallback
                score += 0.3

            # Open bottles get a bonus — they're actively at risk of degrading
            if not b.sealed:
                score += 0.25

            # Tiny preference signal so the result still loosely fits taste
            score += 0.10 * flavor_score(b, prefs)

            scored.append((b, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [
            {
                "bottle": b,
                "score": round(s, 3),
                "reason": build_natural_reason(b, prefs, vibe, recent_ids, affinity, days_since.get(b.id)),
            }
            for b, s in scored[:top_n]
        ]

    # --- Special-case: Cracking something special ---
    if vibe_config.get("special_occasion"):
        days_since = days_since_last_pour(pour_log)
        scored = []
        for b in candidates:
            d = days_since.get(b.id)  # None if never poured
            score = special_occasion_score(b, affinity, d)
            scored.append((b, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:top_n]

        # If user provided occasion text, get personalized reasoning from the AI;
        # otherwise build it programmatically.
        ai_reasons: Dict[str, str] = {}
        if occasion_text and occasion_text.strip():
            ai_reasons = special_occasion_reasoning_via_ai(
                [b for b, _ in top], occasion_text
            )

        results = []
        for b, s in top:
            ai_reason = ai_reasons.get(b.id, "").strip()
            if ai_reason:
                reason = ai_reason
            else:
                # Programmatic fallback
                bits = []
                if b.sealed:
                    bits.append("sealed and waiting for the right moment")
                if b.private_pick and b.pick_group:
                    bits.append(f"a {b.pick_group} pick")
                elif b.private_pick:
                    bits.append("a private pick")
                age = _has_age_statement(b)
                if age and age >= 10:
                    bits.append(f"{age}-year stated age")
                if b.proof and b.proof >= 110:
                    bits.append(f"{b.proof:.0f}° barrel-strength heat")
                if not bits:
                    bits.append("worth marking the moment")
                reason = "; ".join(bits)
                reason = reason[0].upper() + reason[1:]
            results.append({"bottle": b, "score": round(s, 3), "reason": reason})
        return results

    # --- Standard vibe path ---
    # Apply vibe filters
    if filters.get("opened_or_low"):
        opened_or_low = [b for b in candidates if not b.sealed or b.fill_percent < 50]
        if opened_or_low:
            candidates = opened_or_low
    if "max_proof" in filters:
        within = [b for b in candidates if not b.proof or b.proof <= filters["max_proof"]]
        if within:
            candidates = within

    if not candidates:
        candidates = [b for b in inventory if b.quantity > 0]

    sharing_boost = people_count >= 2

    scored = []
    for bottle in candidates:
        if vibe_config.get("random"):
            total = random.random()
        else:
            f = flavor_score(bottle, prefs)
            p = proof_score(bottle, prefs)
            lp = low_proof_score(bottle)
            fi = fill_score(bottle)
            o = opened_score(bottle)
            n = novelty_score(bottle, recent_ids)
            c = crowd_score(bottle)
            i = interesting_score(bottle)
            a = affinity_signal(bottle, affinity)

            total = (
                weights.get("flavor", 0) * f
                + weights.get("proof", 0) * p
                + weights.get("low_proof", 0) * lp
                + weights.get("fill", 0) * fi
                + weights.get("opened", 0) * o
                + weights.get("novelty", 0) * n
                + weights.get("crowd", 0) * c
                + weights.get("interesting", 0) * i
                + weights.get("affinity", 0) * a
            )
            if sharing_boost:
                total += 0.10 * c

        scored.append((bottle, total))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [
        {
            "bottle": b,
            "score": round(s, 3),
            "reason": build_natural_reason(b, prefs, vibe, recent_ids, affinity),
        }
        for b, s in scored[:top_n]
    ]


def build_natural_reason(
    bottle: Bottle,
    prefs: Preferences,
    vibe: str,
    recent_ids: List[str],
    affinity: Optional[Dict[str, float]] = None,
    days_since: Optional[int] = None,
) -> str:
    """Generate a friendly, conversational reason this bottle fits the vibe."""
    bits = []
    affinity = affinity or {}

    # Vibe-specific framing
    if vibe == "Easy sipper before bed":
        if bottle.proof and bottle.proof <= 95:
            bits.append("gentle proof for a wind-down")
        elif bottle.proof and bottle.proof <= 105:
            bits.append("approachable enough for a nightcap")
    elif vibe == "Sharing with company":
        if bottle.quantity > 1:
            bits.append(f"you've got {bottle.quantity} of these")
        if bottle.proof and bottle.proof <= 100:
            bits.append("crowd-friendly proof")
        if not bottle.private_pick:
            bits.append("a solid pour for guests")
    elif vibe == "Want to focus and taste":
        if bottle.private_pick and bottle.pick_group:
            bits.append(f"a {bottle.pick_group} pick worth attention")
        elif bottle.private_pick:
            bits.append("a private pick worth slowing down for")
        if bottle.proof and bottle.proof >= 110:
            bits.append("higher proof rewards a careful sip")
    elif vibe == "Just a regular pour":
        if not bottle.sealed:
            bits.append("already open and ready")
    elif vibe == "The Forgotten Ones":
        if days_since is None:
            bits.append("haven't poured this one yet")
        elif days_since >= 365:
            years = days_since // 365
            bits.append(f"untouched for over a year ({years}y)")
        elif days_since >= 180:
            bits.append(f"hasn't seen the light in {days_since // 30} months")
        elif days_since >= 120:
            bits.append(f"forgotten for ~{days_since // 30} months")
        else:
            bits.append(f"about {days_since} days since your last pour")
        if not bottle.sealed:
            bits.append("open and slowly losing freshness")

    # Learned-affinity flavor: only mention when it's a strong signal
    affinity_score = affinity.get(bottle.id, 0)
    if affinity_score >= 0.8:
        bits.append("one of your most-poured")
    elif affinity_score >= 0.5:
        bits.append("you've been reaching for this")

    # Cross-vibe reasons
    if bottle.fill_percent < 30:
        bits.append("getting low — drink it before it dies")
    elif bottle.fill_percent < 50:
        bits.append("about half left")

    all_notes = bottle.world_tasting_notes + bottle.my_tasting_notes
    matching_notes = [n for n in all_notes if n in prefs.liked_profiles]
    if matching_notes:
        bits.append(f"notes you like: {', '.join(matching_notes[:3])}")

    if not bits:
        bits.append("a good fit for what you're after")

    reason = "; ".join(bits)
    return reason[0].upper() + reason[1:] if reason else reason


# -----------------------------
# Bar / "I'm at a bar" helpers
# -----------------------------

def _name_match_key(name: str) -> str:
    """Loose normalization for matching detected names against the user's inventory."""
    return "".join(c for c in name.lower() if c.isalnum())


def find_owned_match(detected_name: str, inventory: List[Bottle]) -> Optional[Bottle]:
    """Return a bottle from the user's inventory if it loosely matches the detected name."""
    if not detected_name:
        return None
    key = _name_match_key(detected_name)
    if not key:
        return None
    # First try: detected name appears within an owned bottle name (or vice versa)
    for b in inventory:
        owned_key = _name_match_key(b.name)
        if not owned_key:
            continue
        if key in owned_key or owned_key in key:
            return b
    return None


def score_bar_bottle(detected: Dict, prefs: Preferences) -> float:
    """Score a detected (not necessarily owned) bottle against user preferences."""
    notes = detected.get("tasting_notes") or []
    proof = detected.get("proof")

    # Flavor match (only meaningful if we have notes for the bottle)
    pref_vec = normalize(notes_to_vector(prefs.liked_profiles)) if prefs.liked_profiles else []
    bottle_vec = normalize(notes_to_vector(notes)) if notes else []
    if pref_vec and bottle_vec and any(v != 0 for v in bottle_vec):
        f = cosine_similarity(bottle_vec, pref_vec)
    else:
        f = 0.4  # neutral-low when we have nothing to match on

    # Proof match
    if proof is None or prefs.preferred_proof_min is None or prefs.preferred_proof_max is None:
        p = 0.5
    elif prefs.preferred_proof_min <= proof <= prefs.preferred_proof_max:
        p = 1.0
    else:
        distance = min(
            abs(proof - prefs.preferred_proof_min),
            abs(proof - prefs.preferred_proof_max),
        )
        p = max(0.0, 1.0 - (distance / 50))

    # Detection confidence as a small modifier so we don't surface garbage
    conf = float(detected.get("confidence", 0.5))

    return (f * 0.55 + p * 0.30) * (0.7 + 0.3 * conf)


def build_bar_reason(detected: Dict, prefs: Preferences, owned: Optional[Bottle]) -> str:
    """Friendly explanation for why this bar bottle is or isn't a fit."""
    bits = []
    notes = detected.get("tasting_notes") or []
    proof = detected.get("proof")

    if owned:
        bits.append("you already have this on your shelf")

    matching = [n for n in notes if n in prefs.liked_profiles]
    if matching:
        bits.append(f"hits your notes: {', '.join(matching[:3])}")

    if proof and prefs.preferred_proof_min is not None and prefs.preferred_proof_max is not None:
        if prefs.preferred_proof_min <= proof <= prefs.preferred_proof_max:
            bits.append(f"in your proof range ({proof:.0f}°)")

    if not notes:
        bits.append("limited info available — based mostly on proof")

    return "; ".join(bits) if bits else "a reasonable pick"


# -----------------------------
# UI
# -----------------------------

st.set_page_config(page_title="What Should I Pour?", page_icon="🥃", layout="centered")

# Bigger camera component
st.markdown("""
<style>
    iframe[title="streamlit_back_camera_input.back_camera_input"] {
        min-height: 520px !important;
        width: 100% !important;
    }
    [data-testid="stCameraInput"] video,
    [data-testid="stCameraInput"] img {
        min-height: 480px !important;
        object-fit: cover !important;
    }
    .out-of-stock {
        opacity: 0.5;
    }
</style>
""", unsafe_allow_html=True)

db = load_db()

if "user" not in st.session_state:
    st.session_state.user = None

# Auto-login from a remembered session cookie (if available)
if st.session_state.user is None:
    _ctrl = get_cookie_controller()
    if _ctrl is not None:
        try:
            _existing_token = _ctrl.get(SESSION_COOKIE_NAME)
        except Exception:
            _existing_token = None
        if _existing_token:
            _resolved_user = lookup_session(_existing_token)
            if _resolved_user and _resolved_user in db["users"]:
                st.session_state.user = _resolved_user
                # Also stash the token in session state so sign-out can revoke it
                st.session_state.session_token = _existing_token

# --- Auth screens ---
if st.session_state.user is None:
    # --- Hero ---
    st.markdown(
        """
        <div style="text-align: center; padding: 1.5rem 0 0.5rem 0;">
            <div style="font-size: 4rem; line-height: 1;">🥃</div>
            <h1 style="margin: 0.5rem 0 0.25rem 0; font-size: 2rem;">
                What Should I Pour?
            </h1>
            <p style="color: #888; margin: 0; font-size: 1.05rem;">
                Your personal whiskey shelf — smarter pours, less guesswork.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # --- Three-up "what you get" pitch ---
    feat_col1, feat_col2, feat_col3 = st.columns(3)
    with feat_col1:
        st.markdown(
            "<div style='text-align:center; padding: 0.5rem 0;'>"
            "<div style='font-size:1.6rem;'>📷</div>"
            "<div style='font-size:0.85rem; color:#aaa;'>Snap to add a bottle</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with feat_col2:
        st.markdown(
            "<div style='text-align:center; padding: 0.5rem 0;'>"
            "<div style='font-size:1.6rem;'>✨</div>"
            "<div style='font-size:0.85rem; color:#aaa;'>Pick by mood, not menu</div>"
            "</div>",
            unsafe_allow_html=True,
        )
    with feat_col3:
        st.markdown(
            "<div style='text-align:center; padding: 0.5rem 0;'>"
            "<div style='font-size:1.6rem;'>🧊</div>"
            "<div style='font-size:0.85rem; color:#aaa;'>Track every pour</div>"
            "</div>",
            unsafe_allow_html=True,
        )

    st.divider()

    # --- Signup is the default mode ---
    if "auth_view" not in st.session_state:
        st.session_state.auth_view = "signup"

    if st.session_state.auth_view == "signup":
        st.subheader("Create your account")
        st.caption("Got an invite code from a friend? You're in the right place.")

        new_u = st.text_input("Username", key="signup_user", placeholder="how friends will know you")
        new_p = st.text_input("Password", type="password", key="signup_pw", placeholder="at least 6 characters")
        new_p2 = st.text_input("Confirm password", type="password", key="signup_pw2")
        code = st.text_input(
            "Invite code",
            key="signup_code",
            placeholder="paste the code your friend sent",
        )
        remember_signup = st.checkbox(
            "Keep me signed in for 30 days", value=True, key="signup_remember",
        )

        if st.button("Create my account 🥃", type="primary", use_container_width=True):
            expected_code = st.secrets.get("signup_code", "")
            new_u_clean = new_u.strip()
            new_u_key = normalize_username(new_u_clean)
            if not new_u_clean or not new_p:
                st.error("Username and password required.")
            elif new_p != new_p2:
                st.error("Passwords don't match.")
            elif len(new_p) < 6:
                st.error("Password must be at least 6 characters.")
            elif new_u_key in db["users"]:
                st.error("That username is taken.")
            elif not expected_code:
                st.error("Signup is disabled right now (no invite code configured).")
            elif code.strip() != expected_code:
                st.error("That invite code doesn't look right. Double-check with your friend.")
            else:
                create_user(db, new_u_clean, new_p)
                st.session_state.user = new_u_key
                if remember_signup:
                    _token = create_session(new_u_key)
                    if _token:
                        _ctrl = get_cookie_controller()
                        if _ctrl is not None:
                            try:
                                _ctrl.set(
                                    SESSION_COOKIE_NAME, _token,
                                    max_age=SESSION_DAYS * 86400,
                                )
                                st.session_state.session_token = _token
                            except Exception:
                                pass
                st.toast(f"Welcome, {new_u_clean}! 🥃", icon="🎉")
                st.rerun()

        st.divider()
        already_col = st.columns([1, 2, 1])[1]
        with already_col:
            if st.button(
                "Already have an account? Sign in",
                use_container_width=True,
                key="switch_to_login",
            ):
                st.session_state.auth_view = "login"
                st.rerun()

    else:  # login view
        st.subheader("Welcome back")
        u = st.text_input("Username", key="login_user")
        p = st.text_input("Password", type="password", key="login_pw")
        remember_login = st.checkbox(
            "Keep me signed in for 30 days", value=True, key="login_remember",
        )
        if st.button("Sign in 🥃", type="primary", use_container_width=True):
            if verify_user(db, u, p):
                _user_key = normalize_username(u)
                st.session_state.user = _user_key
                if remember_login:
                    _token = create_session(_user_key)
                    if _token:
                        _ctrl = get_cookie_controller()
                        if _ctrl is not None:
                            try:
                                _ctrl.set(
                                    SESSION_COOKIE_NAME, _token,
                                    max_age=SESSION_DAYS * 86400,
                                )
                                st.session_state.session_token = _token
                            except Exception:
                                pass
                st.rerun()
            else:
                st.error("Wrong username or password.")

        st.divider()
        new_col = st.columns([1, 2, 1])[1]
        with new_col:
            if st.button(
                "New here? Create an account",
                use_container_width=True,
                key="switch_to_signup",
            ):
                st.session_state.auth_view = "signup"
                st.rerun()

    st.stop()

# --- Logged-in UI ---
current_user = st.session_state.user
inventory = get_user_bottles(db, current_user)
prefs = get_user_prefs(db, current_user)
recent_ids = db["users"][current_user].get("recent_ids", [])

header_col, logout_col = st.columns([4, 1])
header_col.title("🥃 What Should I Pour?")
header_col.caption(f"Signed in as **{display_name_for(db, current_user)}**")
if logout_col.button("Sign out"):
    # Revoke this device's session token + clear cookie
    _token = st.session_state.get("session_token")
    if _token:
        revoke_session(_token)
    _ctrl = get_cookie_controller()
    if _ctrl is not None:
        try:
            _ctrl.remove(SESSION_COOKIE_NAME)
        except Exception:
            pass
    st.session_state.user = None
    st.session_state.pop("session_token", None)
    st.session_state.pop("identified", None)
    st.rerun()

admin_username = normalize_username(st.secrets.get("admin_username", ""))
is_admin = bool(admin_username) and current_user == admin_username

# Count trades that need my attention so we can badge the Friends tab.
# This includes pending offers waiting on me, plus accepted trades where
# I haven't done my part of the handoff yet.
_my_key = normalize_username(current_user)
_pending_inbox = [
    t for t in trades_for_user(db, current_user, statuses=["pending"])
    if t["to_user"] == _my_key
]
_my_active_todos = 0
for _t in trades_for_user(db, current_user, statuses=["accepted"]):
    _is_from = _t["from_user"] == _my_key
    _i_shipped = _t.get("from_shipped" if _is_from else "to_shipped", False)
    _i_received = _t.get("from_received" if _is_from else "to_received", False)
    _they_shipped = _t.get("to_shipped" if _is_from else "from_shipped", False)
    # I have a todo if: I haven't shipped yet, OR they shipped and I haven't received
    if not _i_shipped:
        _my_active_todos += 1
    elif _they_shipped and not _i_received:
        _my_active_todos += 1

_total_attention = len(_pending_inbox) + _my_active_todos
_friends_label = "Friends"
if _total_attention:
    _friends_label = f"Friends ({_total_attention})"

tab_labels = ["Recommend", "At the Bar", "Look Up", "Inventory", "Add Bottle", "Preferences", _friends_label]
if is_admin:
    tab_labels.append("Admin")

tabs = st.tabs(tab_labels)
tab_recommend = tabs[0]
tab_bar = tabs[1]
tab_lookup = tabs[2]
tab_inventory = tabs[3]
tab_add = tabs[4]
tab_prefs = tabs[5]
tab_friends = tabs[6]
tab_admin = tabs[7] if is_admin else None

# --- Recommend ---
with tab_recommend:
    available_bottles = [b for b in inventory if b.quantity > 0]
    if not available_bottles:
        # Friendly empty state for first-time users
        st.markdown(
            """
            <div style='text-align:center; padding: 2rem 0;'>
                <div style='font-size:3rem;'>🥃</div>
                <h3 style='margin-top:0.5rem;'>Your shelf is empty</h3>
                <p style='color:#888;'>Add a bottle and I'll start recommending pours.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("➕ Add my first bottle", type="primary", use_container_width=True):
            st.components.v1.html(
                """
                <script>
                const tabs = window.parent.document.querySelectorAll('button[role="tab"]');
                for (const t of tabs) {
                    if (t.innerText.trim() === 'Add Bottle') { t.click(); break; }
                }
                </script>
                """,
                height=0,
            )
    else:
        # Compute learned affinity from full pour history
        pour_log = get_pour_log(db, current_user)
        affinity = compute_affinity_scores(pour_log)

        # --- Top pours panel: show what the app has learned ---
        if affinity:
            id_to_bottle = {b.id: b for b in inventory}
            ranked = sorted(affinity.items(), key=lambda kv: kv[1], reverse=True)
            top_for_panel = [
                (id_to_bottle[bid], score)
                for bid, score in ranked
                if bid in id_to_bottle
            ][:5]

            if top_for_panel:
                with st.expander(
                    f"⭐ Your top pours — what the app has learned ({len(pour_log)} pours logged)",
                    expanded=False,
                ):
                    st.caption(
                        "Built from your pour history. Bigger pours and recent pours "
                        "count more. The 'Just a regular pour' vibe leans on this most."
                    )
                    for b, score in top_for_panel:
                        # Render as a horizontal bar via stars
                        stars = "★" * max(1, round(score * 5)) + "☆" * (5 - max(1, round(score * 5)))
                        st.markdown(f"**{b.name}** {stars}")
                        st.caption(
                            f"{b.type} · {b.proof:.0f}° · "
                            f"{b.fill_percent:.0f}% full · affinity {int(score * 100)}%"
                        )

        st.markdown("### What's the vibe?")

        vibe_keys = list(VIBES.keys())
        vibe = st.radio(
            "Vibe",
            options=vibe_keys,
            index=0,
            label_visibility="collapsed",
            format_func=lambda v: f"**{v}** — {VIBES[v]['blurb']}",
            key="vibe",
        )

        # Occasion text only shows for the special-occasion vibe
        occasion_text = ""
        if vibe == "Cracking something special":
            occasion_text = st.text_input(
                "What's the occasion? (optional)",
                placeholder="e.g. promotion, anniversary, friend in town from out of state",
                key="occasion_text",
                help="Adding context lets the AI tailor the reasoning to your moment.",
            )

        with st.expander("More context (optional)"):
            people_count = st.slider(
                "How many people drinking?",
                min_value=1, max_value=8, value=1,
                help="With more people, we'll lean toward crowd-friendly bottles.",
            )
            top_n = st.slider("Number of suggestions", 1, 5, 3)

        if st.button(
            "🥃 Recommend me a pour",
            type="primary",
            use_container_width=True,
        ):
            results = recommend_bottles(
                inventory, prefs, vibe, recent_ids,
                top_n=top_n,
                people_count=people_count,
                affinity=affinity,
                pour_log=pour_log,
                occasion_text=occasion_text,
            )
            st.session_state["last_recommendation"] = {
                "results_ids": [r["bottle"].id for r in results],
                "vibe": vibe,
                "occasion_text": occasion_text,
                # Stash the per-bottle reasons so they survive the rerun without
                # re-calling the AI (which would cost money and produce different text)
                "reasons": {r["bottle"].id: r["reason"] for r in results},
            }
            st.rerun()

        # Render the most recent recommendation set (persists across pour-logs)
        last = st.session_state.get("last_recommendation")
        if last:
            id_set = last["results_ids"]
            id_to_bottle = {b.id: b for b in inventory}
            shown = [id_to_bottle[bid] for bid in id_set if bid in id_to_bottle]

            if not shown:
                if last["vibe"] == "The Forgotten Ones":
                    st.info(
                        "Nothing's been forgotten yet — every open bottle has been "
                        "poured recently. Try this again in a month or so."
                    )
                else:
                    st.info("Those bottles aren't available anymore. Pick a vibe and recommend again.")
            else:
                st.markdown("---")
                st.markdown(f"#### For **{last['vibe'].lower()}** — here's what I'd pour:")

                # Pre-compute days-since for use in the per-result reason
                last_days_since = days_since_last_pour(pour_log)
                stashed_reasons = last.get("reasons", {})

                for b in shown:
                    # Use stashed reason if we have one (avoids re-calling AI for
                    # special-occasion personalized text). Otherwise rebuild
                    # programmatically.
                    if b.id in stashed_reasons:
                        reason = stashed_reasons[b.id]
                    else:
                        reason = build_natural_reason(
                            b, prefs, last["vibe"], recent_ids, affinity,
                            last_days_since.get(b.id),
                        )
                    with st.container(border=True):
                        title = f"### {b.name}"
                        if b.private_pick and b.pick_group:
                            st.markdown(title)
                            st.caption(f"_{b.pick_group} pick_")
                        else:
                            st.markdown(title)

                        st.caption(
                            f"{b.type} · {b.proof:.0f}° proof · "
                            f"{b.fill_percent:.0f}% full · qty {b.quantity}"
                        )

                        st.markdown(f"**Why:** {reason}.")

                        if b.my_tasting_notes:
                            st.markdown(f"**Your notes:** _{', '.join(b.my_tasting_notes)}_")
                        if b.world_tasting_notes:
                            st.caption(f"World's notes: {', '.join(b.world_tasting_notes)}")

                        # Pour controls
                        pour_oz = st.radio(
                            "Pour size",
                            options=[0.5, 1.0, 1.5, 2.0],
                            index=1,
                            horizontal=True,
                            key=f"pour_size_{b.id}",
                            format_func=lambda x: f"{x} oz",
                        )
                        if st.button(
                            "I poured this 🥃",
                            key=f"pour_{b.id}",
                            use_container_width=True,
                        ):
                            log_pour(db, current_user, b.id, pour_oz, vibe=last["vibe"])
                            st.toast(
                                f"Logged a {pour_oz} oz pour of {b.name}. Cheers.",
                                icon="🥃",
                            )
                            st.rerun()

# --- At the Bar ---
with tab_bar:
    st.markdown("### 🍻 At a bar?")
    st.caption(
        "Snap the menu or the backbar shelf and I'll rank what to order based on "
        "your taste. Bottles you already have at home get flagged."
    )

    source = st.radio(
        "What are you photographing?",
        options=["Menu", "Shelf"],
        horizontal=True,
        key="bar_source",
        help=(
            "Menus are typically more accurate (printed text). "
            "Shelves work but depend on label visibility and lighting."
        ),
    )

    # Camera/upload
    if "bar_camera_open" not in st.session_state:
        st.session_state.bar_camera_open = False
    if "bar_uploader_version" not in st.session_state:
        st.session_state.bar_uploader_version = 0

    col_cam, col_upload = st.columns(2)
    if col_cam.button(
        "📷 Close camera" if st.session_state.bar_camera_open else "📷 Use camera",
        use_container_width=True,
        key="bar_cam_btn",
    ):
        st.session_state.bar_camera_open = not st.session_state.bar_camera_open
        st.rerun()

    bar_uploaded = col_upload.file_uploader(
        "Upload image",
        type=["jpg", "jpeg", "png", "webp"],
        label_visibility="collapsed",
        key=f"bar_uploader_{st.session_state.bar_uploader_version}",
    )

    bar_photo_bytes = None
    bar_photo_mime = "image/jpeg"

    if st.session_state.bar_camera_open:
        try:
            from streamlit_back_camera_input import back_camera_input
            st.caption("Tap the video to capture (rear camera by default).")
            captured = back_camera_input(key="bar_rear_cam")
            if captured is not None:
                if isinstance(captured, str) and captured.startswith("data:image"):
                    header, data = captured.split(",", 1)
                    bar_photo_mime = header.split(";")[0].replace("data:", "")
                    bar_photo_bytes = base64.b64decode(data)
                elif hasattr(captured, "getvalue"):
                    bar_photo_bytes = captured.getvalue()
                    bar_photo_mime = getattr(captured, "type", "image/jpeg") or "image/jpeg"
                else:
                    bar_photo_bytes = bytes(captured)
        except ImportError:
            fallback = st.camera_input("Take a photo")
            if fallback is not None:
                bar_photo_bytes = fallback.getvalue()
                bar_photo_mime = fallback.type or "image/jpeg"

    bar_image_bytes = bar_photo_bytes if bar_photo_bytes else (
        bar_uploaded.getvalue() if bar_uploaded else None
    )
    bar_image_mime = bar_photo_mime if bar_photo_bytes else (
        bar_uploaded.type if bar_uploaded else "image/jpeg"
    )

    if bar_image_bytes is not None and st.button(
        "🔎 Analyze and recommend",
        type="primary",
        use_container_width=True,
        key="bar_analyze",
    ):
        with st.spinner(f"Reading the {source.lower()}... this can take 10–20 seconds."):
            try:
                detected = detect_bottles_from_image(
                    bar_image_bytes, bar_image_mime, source.lower()
                )
                st.session_state["bar_detected"] = detected
                st.session_state["bar_source_used"] = source
            except json.JSONDecodeError:
                st.error("Couldn't parse the image — try a clearer shot.")
            except Exception as e:
                st.error(f"Couldn't analyze image: {e}")

    detected = st.session_state.get("bar_detected")
    if detected is not None:
        if not detected:
            st.warning(
                "I didn't find any whiskeys I could read clearly. "
                "Try a closer or better-lit photo, or switch between Menu and Shelf modes."
            )
        else:
            # Score everything
            scored = []
            for d in detected:
                owned = find_owned_match(d.get("name", ""), inventory)
                score = score_bar_bottle(d, prefs)
                # Small bump for owned matches so they surface (you know you like them)
                if owned:
                    score += 0.05
                scored.append({
                    "detected": d,
                    "owned": owned,
                    "score": score,
                    "reason": build_bar_reason(d, prefs, owned),
                })
            scored.sort(key=lambda x: x["score"], reverse=True)

            st.divider()
            st.markdown(
                f"#### Top picks from this {st.session_state.get('bar_source_used', source).lower()}"
            )
            st.caption(
                f"Found **{len(detected)}** bottle"
                f"{'s' if len(detected) != 1 else ''} · ranked by fit to your taste"
            )

            for r in scored[:3]:
                d = r["detected"]
                with st.container(border=True):
                    title = f"### {d.get('name', 'Unknown')}"
                    st.markdown(title)

                    meta_parts = [d.get("type", "spirit")]
                    if d.get("proof"):
                        meta_parts.append(f"{d['proof']:.0f}°")
                    if d.get("price"):
                        meta_parts.append(f"${d['price']:.2f}")
                    st.caption(" · ".join(meta_parts))

                    if r["owned"]:
                        st.info(
                            f"🏠 **You already have this at home** "
                            f"({r['owned'].fill_percent:.0f}% full, qty {r['owned'].quantity})"
                        )

                    st.markdown(f"**Why:** {r['reason']}.")

                    if d.get("tasting_notes"):
                        st.caption(f"Common notes: {', '.join(d['tasting_notes'])}")

                    conf = float(d.get("confidence", 0))
                    if conf < 0.6:
                        st.caption(
                            f"_Low identification confidence ({int(conf * 100)}%) — "
                            f"the bartender may be the better source for details._"
                        )

            # See-all expander
            if len(scored) > 3:
                with st.expander(f"See all {len(scored)} detected bottles"):
                    for r in scored:
                        d = r["detected"]
                        line = f"**{d.get('name', 'Unknown')}**"
                        sub_bits = [d.get("type", "spirit")]
                        if d.get("proof"):
                            sub_bits.append(f"{d['proof']:.0f}°")
                        if d.get("price"):
                            sub_bits.append(f"${d['price']:.2f}")
                        sub_bits.append(f"score: {r['score']:.2f}")

                        owned_tag = " 🏠" if r["owned"] else ""
                        st.markdown(f"{line}{owned_tag}")
                        st.caption(" · ".join(sub_bits))
                        if d.get("tasting_notes"):
                            st.caption(f"_{', '.join(d['tasting_notes'])}_")
                        st.markdown("---")

            st.divider()
            if st.button("Clear results and start over", key="bar_clear"):
                st.session_state.pop("bar_detected", None)
                st.session_state.pop("bar_source_used", None)
                st.session_state.bar_uploader_version += 1
                st.session_state.bar_camera_open = False
                st.rerun()

# --- Bottle Look Up ---
with tab_lookup:
    st.markdown("### 🔎 Bottle Look Up")
    st.caption(
        "Snap any bottle and get a quick **BUY / YOUR CALL / SKIP** verdict based on "
        "your taste. Add the price (optional) for a value check too."
    )

    # Camera/upload
    if "lookup_camera_open" not in st.session_state:
        st.session_state.lookup_camera_open = False
    if "lookup_uploader_version" not in st.session_state:
        st.session_state.lookup_uploader_version = 0

    col_cam, col_upload = st.columns(2)
    if col_cam.button(
        "📷 Close camera" if st.session_state.lookup_camera_open else "📷 Use camera",
        key="lookup_cam_toggle",
        use_container_width=True,
    ):
        st.session_state.lookup_camera_open = not st.session_state.lookup_camera_open
        st.rerun()

    uploaded = col_upload.file_uploader(
        "Upload",
        type=["jpg", "jpeg", "png", "webp"],
        label_visibility="collapsed",
        key=f"lookup_uploader_{st.session_state.lookup_uploader_version}",
    )

    photo_bytes = None
    photo_mime = "image/jpeg"

    if st.session_state.lookup_camera_open:
        try:
            from streamlit_back_camera_input import back_camera_input
            st.caption("Tap the video to capture.")
            captured = back_camera_input(key="lookup_rear_cam")
            if captured is not None:
                if isinstance(captured, str) and captured.startswith("data:image"):
                    header, data = captured.split(",", 1)
                    photo_mime = header.split(";")[0].replace("data:", "")
                    photo_bytes = base64.b64decode(data)
                elif hasattr(captured, "getvalue"):
                    photo_bytes = captured.getvalue()
                    photo_mime = getattr(captured, "type", "image/jpeg") or "image/jpeg"
                else:
                    photo_bytes = bytes(captured)
        except ImportError:
            fallback = st.camera_input("Take a photo")
            if fallback is not None:
                photo_bytes = fallback.getvalue()
                photo_mime = fallback.type or "image/jpeg"

    image_bytes = photo_bytes if photo_bytes else (uploaded.getvalue() if uploaded else None)
    image_mime = photo_mime if photo_bytes else (uploaded.type if uploaded else "image/jpeg")

    if image_bytes is not None and st.button(
        "🔎 Look up this bottle",
        type="primary",
        use_container_width=True,
    ):
        with st.spinner("Reading the bottle and pulling what's known about it..."):
            try:
                result = lookup_bottle_from_image(image_bytes, image_mime)
                st.session_state["lookup_result"] = result
            except json.JSONDecodeError:
                st.error("AI returned invalid JSON. Try a clearer photo.")
            except Exception as e:
                st.error(f"Couldn't look up bottle: {e}")

    detected = st.session_state.get("lookup_result")

    if detected:
        if not detected.get("name"):
            st.warning("Couldn't identify the bottle from the photo. Try a clearer shot of the label.")
        else:
            # ---- Price input ----
            st.divider()
            price_col, msrp_col = st.columns([1, 1])
            user_price = price_col.number_input(
                "Price you're seeing (USD, optional)",
                min_value=0.0,
                value=0.0,
                step=1.0,
                help="Leave at 0 to skip the value analysis. Fill in for a price check.",
                key="lookup_price",
            )
            user_price_val = user_price if user_price and user_price > 0 else None

            estimated_msrp = detected.get("estimated_msrp_usd")
            msrp_conf = detected.get("msrp_confidence", "unknown")
            if estimated_msrp:
                msrp_col.metric(
                    "Estimated MSRP",
                    f"${estimated_msrp:,.0f}",
                    help=f"AI confidence: {msrp_conf}. From training data, not real-time.",
                )
            else:
                msrp_col.metric("Estimated MSRP", "Unknown")

            # ---- Compute scores and verdict ----
            pour_log = get_pour_log(db, current_user)
            affinity = compute_affinity_scores(pour_log)
            fit = fit_score_for_lookup(detected, prefs, affinity)
            value = value_score_for_lookup(user_price_val, detected)
            verdict = lookup_verdict(fit, value)

            # ---- Verdict banner ----
            st.markdown(
                f"""
                <div style="
                    border-left: 6px solid {verdict['color']};
                    background: rgba(0,0,0,0.03);
                    padding: 14px 18px;
                    border-radius: 6px;
                    margin: 16px 0;
                ">
                    <div style="font-size:1.6rem; font-weight:700; color:{verdict['color']};">
                        {verdict['emoji']} {verdict['label']}
                    </div>
                    <div style="font-size:1rem; margin-top:4px;">
                        {verdict['rationale']}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # ---- Owned-already callout ----
            owned = find_owned_match(detected["name"], inventory)
            if owned:
                st.info(
                    f"🏠 **You already have this at home** "
                    f"({owned.quantity}× · {owned.fill_percent:.0f}% full · "
                    f"{'sealed' if owned.sealed else 'open'})"
                )

            # ---- Bottle details ----
            st.markdown(f"### {detected['name']}")
            meta_bits = []
            if detected.get("type"):
                meta_bits.append(detected["type"].title())
            if detected.get("proof"):
                meta_bits.append(f"{detected['proof']:.0f}° proof")
            if detected.get("age_statement"):
                meta_bits.append(detected["age_statement"])
            if detected.get("region"):
                meta_bits.append(detected["region"])
            if meta_bits:
                st.caption(" · ".join(meta_bits))

            if detected.get("distillery"):
                st.markdown(f"**Distillery:** {detected['distillery']}")
            if detected.get("mash_bill"):
                st.markdown(f"**Mash bill:** {detected['mash_bill']}")
            if detected.get("is_private_pick"):
                pg = detected.get("pick_group", "")
                st.markdown(
                    f"**Private pick** — {pg}" if pg else "**Private pick**"
                )
            if detected.get("allocated"):
                st.caption("⚠️ This bottle is commonly allocated / hard to find.")

            if detected.get("description"):
                st.markdown(f"_{detected['description']}_")

            if detected.get("tasting_notes"):
                st.markdown(f"**Common tasting notes:** {', '.join(detected['tasting_notes'])}")

            # ---- Fit breakdown ----
            with st.expander("Why this verdict?"):
                st.markdown(f"**Fit score:** {int(fit * 100)}% / 100%")
                st.caption(
                    "Based on how well the bottle's known flavor profile matches your "
                    "liked profiles, plus proof fit against your preferred range."
                )
                if value is not None:
                    st.markdown(f"**Value score:** {int(value * 100)}% / 100%")
                    if estimated_msrp and user_price_val:
                        ratio = user_price_val / estimated_msrp
                        if ratio < 1.0:
                            st.caption(f"At ${user_price_val:.0f}, that's **{(1-ratio)*100:.0f}% below MSRP** — a deal.")
                        elif ratio <= 1.10:
                            st.caption(f"At ${user_price_val:.0f}, that's right around MSRP — fair.")
                        else:
                            st.caption(
                                f"At ${user_price_val:.0f}, that's **{(ratio-1)*100:.0f}% above MSRP**."
                                + (" Allocated bottles often sell at this kind of markup." if detected.get("allocated") else "")
                            )
                else:
                    st.caption("Add a price above to get a value check.")

                conf = detected.get("confidence", 0)
                if conf < 0.7:
                    st.caption(
                        f"⚠️ AI was only {int(conf*100)}% confident on the bottle ID — "
                        "verify the name before trusting the rest."
                    )

            # ---- Action buttons ----
            st.divider()
            act_save, act_clear = st.columns(2)

            if act_save.button("💾 Save to my shelf", type="primary", use_container_width=True):
                # Add the bottle to current user's inventory using detected fields
                new_id = f"b_{int(random.random() * 1_000_000)}"
                btype = detected.get("type", "other")
                if btype not in ("bourbon", "rye", "scotch", "rum", "other"):
                    btype = "other"
                db["users"][current_user]["bottles"].append({
                    "id": new_id,
                    "name": detected["name"],
                    "type": btype,
                    "proof": float(detected["proof"]) if detected.get("proof") else 90.0,
                    "world_tasting_notes": detected.get("tasting_notes", []) or [],
                    "my_tasting_notes": [],
                    "fill_percent": 100.0,
                    "sealed": True,
                    "quantity": 1,
                    "private_pick": bool(detected.get("is_private_pick", False)),
                    "pick_group": detected.get("pick_group", "") or "",
                    "size_ml": 750,
                })
                save_db(db)
                st.session_state.pop("lookup_result", None)
                st.session_state.lookup_uploader_version += 1
                st.session_state.lookup_camera_open = False
                st.toast(
                    f"Success! Less Shelf Space Available — {detected['name']} added.",
                    icon="🥃",
                )
                st.rerun()

            if act_clear.button("Clear and look up another", use_container_width=True):
                st.session_state.pop("lookup_result", None)
                st.session_state.lookup_uploader_version += 1
                st.session_state.lookup_camera_open = False
                st.rerun()

# --- Inventory ---
with tab_inventory:
    # Add Bottle redirect button
    if st.button("➕ Add a Bottle", type="primary", use_container_width=True):
        st.components.v1.html(
            """
            <script>
            (function() {
                const doc = window.parent.document;
                const tabButtons = doc.querySelectorAll('button[role="tab"]');
                for (const btn of tabButtons) {
                    if (btn.innerText.trim() === 'Add Bottle') {
                        btn.click();
                        window.parent.scrollTo({top: 0, behavior: 'smooth'});
                        break;
                    }
                }
            })();
            </script>
            """,
            height=0,
        )

    if not inventory:
        st.info("No bottles yet.")
    else:
        # --- Search bar ---
        search_query = st.text_input(
            "🔍 Search",
            key="inv_search",
            placeholder="Name, type, notes, pick group… (multiple words supported)",
            label_visibility="collapsed",
        )

        # --- Sort + view-mode + show-zero on one row ---
        sort_options = [
            "Name (A–Z)",
            "Recently added",
            "Fill % (low to high)",
            "Proof (high to low)",
            "Sealed first",
        ]
        s_col, v_col, z_col = st.columns([2, 1, 1])
        sort_by = s_col.selectbox("Sort by", sort_options, key="inv_sort")
        view_mode = v_col.selectbox("View", ["List", "Cards"], key="inv_view")
        show_zero = z_col.checkbox("Show empties", value=False, key="inv_show_zero")

        # --- Quick filter chips ---
        chip_options = ["Sealed only", "Open only", "Running low", "Private picks"]
        quick_filters = st.multiselect(
            "Quick filters",
            chip_options,
            default=[],
            key="inv_chips",
            label_visibility="collapsed",
            placeholder="Filters: sealed, open, running low, private picks",
        )

        # --- Optional grouping ---
        group_by_type = st.toggle("Group by type", value=False, key="inv_group_type")

        # --- Apply filters and sort ---
        visible = filter_and_sort_bottles(
            inventory, search_query, sort_by, show_zero, quick_filters
        )

        if not visible:
            st.info("No bottles match your search/filters.")
        else:
            # --- Pagination state ---
            PAGE_SIZE = 20
            if "inv_page_size" not in st.session_state:
                st.session_state.inv_page_size = PAGE_SIZE

            # Reset page size when filters change
            filter_signature = (
                search_query,
                sort_by,
                view_mode,
                show_zero,
                tuple(quick_filters),
                group_by_type,
            )
            if st.session_state.get("inv_filter_sig") != filter_signature:
                st.session_state.inv_filter_sig = filter_signature
                st.session_state.inv_page_size = PAGE_SIZE

            total = len(visible)
            showing = min(st.session_state.inv_page_size, total)
            st.caption(f"Showing **{showing}** of **{total}** bottles")

            # --- Render helpers ---

            def render_bottle_card(b: Bottle):
                """Full card layout."""
                out_of_stock = b.quantity <= 0
                with st.container(border=True):
                    if out_of_stock:
                        st.markdown(
                            f"<div class='out-of-stock'><strong>{b.name}</strong> "
                            f"<em>(out of stock)</em></div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        title = f"**{b.name}**"
                        if b.private_pick and b.pick_group:
                            title += f" — _{b.pick_group} pick_"
                        st.markdown(title)

                    st.caption(
                        f"{b.type} · {b.proof}° · "
                        f"{'sealed' if b.sealed else 'open'} · "
                        f"{b.fill_percent:.0f}% full · {b.size_ml} mL"
                    )

                    c1, c2, c3 = st.columns(3)
                    new_qty = c1.number_input(
                        "Quantity", min_value=0, max_value=99,
                        value=int(b.quantity), step=1, key=f"qty_{b.id}",
                    )
                    new_fill = c2.slider(
                        "Fill %", 0, 100, int(b.fill_percent), key=f"fill_{b.id}"
                    )
                    new_sealed = c3.toggle(
                        "Sealed", value=b.sealed, key=f"sealed_{b.id}"
                    )

                    if b.world_tasting_notes:
                        st.caption(f"World's notes: {', '.join(b.world_tasting_notes)}")
                    if b.my_tasting_notes:
                        st.caption(f"My notes: {', '.join(b.my_tasting_notes)}")

                    if not out_of_stock:
                        with st.expander("🥃 Log a pour"):
                            pour_oz = st.radio(
                                "Pour size (oz)",
                                options=[0.5, 1.0, 1.5, 2.0],
                                index=1,
                                horizontal=True,
                                key=f"inv_pour_size_{b.id}",
                                format_func=lambda x: f"{x} oz",
                            )
                            if st.button("Log this pour", key=f"inv_pour_{b.id}", type="primary"):
                                log_pour(db, current_user, b.id, pour_oz)
                                st.toast(
                                    f"Logged a {pour_oz} oz pour of {b.name}. Cheers.",
                                    icon="🥃",
                                )
                                st.rerun()

                    cols = st.columns(2)
                    if cols[0].button("Update", key=f"upd_{b.id}"):
                        for bot in db["users"][current_user]["bottles"]:
                            if bot["id"] == b.id:
                                bot["quantity"] = int(new_qty)
                                bot["fill_percent"] = new_fill
                                bot["sealed"] = bool(new_sealed)
                        save_db(db)
                        st.rerun()
                    if cols[1].button("Remove", key=f"del_{b.id}"):
                        db["users"][current_user]["bottles"] = [
                            x for x in db["users"][current_user]["bottles"] if x["id"] != b.id
                        ]
                        save_db(db)
                        st.rerun()

            def render_bottle_list_row(b: Bottle):
                """Compact one-line layout. Tap 'Details' to expand inline."""
                out_of_stock = b.quantity <= 0
                line_parts = [f"**{b.name}**"]
                if b.private_pick and b.pick_group:
                    line_parts.append(f"_({b.pick_group})_")
                meta = (
                    f"{b.type} · {b.proof:.0f}° · {b.fill_percent:.0f}% · "
                    f"qty {b.quantity} · {'🔒' if b.sealed else '🥃'}"
                )
                if out_of_stock:
                    meta = f"_(out of stock)_ · {meta}"

                c_main, c_action = st.columns([5, 2])
                c_main.markdown(" ".join(line_parts))
                c_main.caption(meta)
                is_open = st.session_state.get(f"row_open_{b.id}", False)
                if c_action.button(
                    "Hide" if is_open else "Details",
                    key=f"toggle_{b.id}",
                    use_container_width=True,
                ):
                    st.session_state[f"row_open_{b.id}"] = not is_open
                    st.rerun()

                if is_open:
                    render_bottle_card(b)

            def render_bottle(b: Bottle):
                if view_mode == "Cards":
                    render_bottle_card(b)
                else:
                    render_bottle_list_row(b)

            # --- Render bottles ---
            paged = visible[: st.session_state.inv_page_size]

            if group_by_type:
                seen_types = []
                type_to_bottles: Dict[str, List[Bottle]] = {}
                for b in paged:
                    if b.type not in type_to_bottles:
                        type_to_bottles[b.type] = []
                        seen_types.append(b.type)
                    type_to_bottles[b.type].append(b)

                for t in seen_types:
                    group = type_to_bottles[t]
                    with st.expander(f"**{t.title()}** — {len(group)}", expanded=True):
                        for b in group:
                            render_bottle(b)
            else:
                for b in paged:
                    render_bottle(b)

            # --- Pagination footer ---
            if total > st.session_state.inv_page_size:
                if st.button(
                    f"Show more ({total - st.session_state.inv_page_size} remaining)",
                    use_container_width=True,
                ):
                    st.session_state.inv_page_size += PAGE_SIZE
                    st.rerun()

# --- Add Bottle ---
with tab_add:
    # Handle the "just added a bottle" state set by the previous run
    if "just_added_bottle" in st.session_state:
        added_name = st.session_state.pop("just_added_bottle")
        st.toast(
            f"Success! Less Shelf Space Available — {added_name} added.",
            icon="🥃",
        )
        st.components.v1.html(
            """
            <script>
            // Use instant scroll (not smooth) so it completes before any further reruns
            window.parent.scrollTo({top: 0, behavior: 'instant'});
            </script>
            """,
            height=0,
        )

    st.write("Take or upload a photo of the bottle, or enter manually.")

    if "camera_open" not in st.session_state:
        st.session_state.camera_open = False

    col_cam, col_upload = st.columns(2)
    if col_cam.button(
        "📷 Close camera" if st.session_state.camera_open else "📷 Use camera",
        use_container_width=True,
    ):
        st.session_state.camera_open = not st.session_state.camera_open
        st.rerun()

    # Counter lets us remount the file uploader to clear its selection
    if "uploader_version" not in st.session_state:
        st.session_state.uploader_version = 0

    uploaded = col_upload.file_uploader(
        "Upload image",
        type=["jpg", "jpeg", "png", "webp"],
        label_visibility="collapsed",
        key=f"bottle_uploader_{st.session_state.uploader_version}",
    )

    photo_bytes = None
    photo_mime = "image/jpeg"

    if st.session_state.camera_open:
        try:
            from streamlit_back_camera_input import back_camera_input
            st.caption("Tap the video to capture (rear camera by default).")
            captured = back_camera_input(key="rear_cam")
            if captured is not None:
                if isinstance(captured, str) and captured.startswith("data:image"):
                    header, data = captured.split(",", 1)
                    photo_mime = header.split(";")[0].replace("data:", "")
                    photo_bytes = base64.b64decode(data)
                elif hasattr(captured, "getvalue"):
                    photo_bytes = captured.getvalue()
                    photo_mime = getattr(captured, "type", "image/jpeg") or "image/jpeg"
                else:
                    photo_bytes = bytes(captured)
        except ImportError:
            st.warning(
                "Rear-camera component not installed. Falling back to default camera."
            )
            fallback = st.camera_input("Take a photo")
            if fallback is not None:
                photo_bytes = fallback.getvalue()
                photo_mime = fallback.type or "image/jpeg"

    image_bytes = photo_bytes if photo_bytes else (uploaded.getvalue() if uploaded else None)
    image_mime = photo_mime if photo_bytes else (uploaded.type if uploaded else "image/jpeg")

    if image_bytes is not None and st.button("Identify bottle from photo", type="primary"):
        with st.spinner("Reading the label..."):
            try:
                result = identify_bottle_from_image(image_bytes, image_mime)
                st.session_state["identified"] = result
                # Clear stale form state so the new auto-fill values take effect.
                # Streamlit text_inputs with explicit keys persist their value
                # across reruns, which would otherwise override the new defaults.
                for k in ("bottle_world_notes", "bottle_my_notes"):
                    st.session_state.pop(k, None)
            except json.JSONDecodeError:
                st.error("Claude returned invalid JSON. Try another photo or enter manually.")
            except Exception as e:
                st.error(f"Couldn't identify bottle: {e}")

    identified = st.session_state.get("identified", {})

    if identified:
        conf = identified.get("confidence", 0)
        if conf >= 0.7:
            st.success(f"Identified with {int(conf * 100)}% confidence — review and save.")
        elif conf >= 0.4:
            st.warning(f"Best guess ({int(conf * 100)}% confidence) — verify the details.")
        else:
            st.error(f"Low confidence ({int(conf * 100)}%) — most fields may be wrong.")
        if identified.get("notes"):
            st.caption(f"_{identified['notes']}_")

    st.divider()
    st.write("**Confirm details**")

    type_options = ["bourbon", "rye", "scotch", "rum", "other"]
    default_type = identified.get("type", "bourbon")
    if default_type not in type_options:
        default_type = "other"

    name = st.text_input("Name", value=identified.get("name", ""))
    btype = st.selectbox("Type", type_options, index=type_options.index(default_type))
    default_proof = identified.get("proof") or 90.0
    proof = st.number_input("Proof", 80.0, 160.0, float(default_proof), step=0.1)

    size_options = [375, 500, 700, 750, 1000, 1750]
    size_ml = st.selectbox(
        "Bottle size",
        options=size_options,
        index=size_options.index(750),
        format_func=lambda ml: (
            f"{ml} mL"
            + ("  (375 — half)" if ml == 375 else "")
            + ("  (500 — European)" if ml == 500 else "")
            + ("  (700 — UK/EU)" if ml == 700 else "")
            + ("  (750 — standard US)" if ml == 750 else "")
            + ("  (1 L)" if ml == 1000 else "")
            + ("  (1.75 L — handle)" if ml == 1750 else "")
        ),
    )
    quantity = st.number_input("Quantity", min_value=1, max_value=99, value=1, step=1)

    # Sealed toggle: default to sealed (True) unless vision detected otherwise
    detected_sealed = identified.get("is_sealed")
    sealed_default = True if detected_sealed is None else bool(detected_sealed)
    sealed = st.toggle(
        "Sealed",
        value=sealed_default,
        help="On = unopened bottle. Off = already broken into.",
    )
    if detected_sealed is not None and identified:
        st.caption(
            f"_AI detected the bottle appears "
            f"{'sealed' if detected_sealed else 'opened'} from the photo._"
        )

    # Fill % default: prefer the AI's estimate when available; otherwise 100 if
    # sealed, 90 if opened (just a reasonable starting guess for the user to adjust).
    detected_fill = identified.get("estimated_fill_percent") if identified else None
    if detected_fill is not None:
        try:
            detected_fill = int(round(float(detected_fill)))
            detected_fill = max(0, min(100, detected_fill))
        except (TypeError, ValueError):
            detected_fill = None
    if detected_fill is not None:
        fill_default = detected_fill
    else:
        fill_default = 100 if sealed else 90

    fill = st.slider("Fill %", 0, 100, fill_default)
    if detected_fill is not None and identified:
        st.caption(f"_AI estimated fill at {detected_fill}% from the photo._")
    elif identified and not sealed:
        st.caption("_Couldn't see the liquid level — adjust manually._")

    # Private pick toggle + conditional group field
    detected_private = bool(identified.get("is_private_pick", False))
    detected_pick_group = identified.get("pick_group", "") or ""
    private_pick = st.toggle(
        "Private Pick",
        value=detected_private,
        help="On = single barrel pick for a store, group, or club.",
    )
    pick_group = ""
    if private_pick:
        pick_group = st.text_input(
            "Pick group / store",
            value=detected_pick_group,
            placeholder="e.g., Justins' House of Bourbon",
        )

    # Tasting notes — split into two fields
    detected_world_notes = identified.get("tasting_notes", []) or []
    world_default = ", ".join(detected_world_notes)
    world_notes_raw = st.text_input(
        "World's Tasting Notes",
        value=world_default,
        placeholder="caramel, vanilla, oak (auto-filled when bottle is recognized)",
        help="Commonly known notes — auto-populated by AI when possible.",
        key="bottle_world_notes",
    )
    my_notes_raw = st.text_input(
        "My Tasting Notes",
        placeholder="What you actually taste",
        help="Your own notes — weighted more heavily in recommendations.",
        key="bottle_my_notes",
    )

    col_save, col_clear = st.columns(2)
    if col_save.button("Save bottle", type="primary"):
        if not name.strip():
            st.error("Name required.")
        elif private_pick and not pick_group.strip():
            st.error("Pick group / store required when 'Private Pick' is on.")
        else:
            new_id = f"b_{int(random.random() * 1_000_000)}"
            db["users"][current_user]["bottles"].append({
                "id": new_id,
                "name": name.strip(),
                "type": btype,
                "proof": proof,
                "world_tasting_notes": [n.strip() for n in world_notes_raw.split(",") if n.strip()],
                "my_tasting_notes": [n.strip() for n in my_notes_raw.split(",") if n.strip()],
                "fill_percent": float(fill),
                "sealed": bool(sealed),
                "quantity": int(quantity),
                "private_pick": bool(private_pick),
                "pick_group": pick_group.strip(),
                "size_ml": int(size_ml),
            })
            save_db(db)
            # Reset the form: drop the AI identification, clear tasting notes,
            # and bump the uploader key so the file picker remounts (this is the
            # only reliable way to clear st.file_uploader's selection).
            st.session_state.pop("identified", None)
            st.session_state.pop("bottle_world_notes", None)
            st.session_state.pop("bottle_my_notes", None)
            st.session_state.uploader_version += 1
            # Also close the camera if it was open
            st.session_state.camera_open = False
            # Defer the toast and scroll until after the rerun so they fire on a
            # fresh, stable page (avoids the iframe-being-torn-down race).
            st.session_state["just_added_bottle"] = name.strip()
            st.rerun()

    if col_clear.button("Clear photo result"):
        st.session_state.pop("identified", None)
        st.session_state.pop("bottle_world_notes", None)
        st.session_state.pop("bottle_my_notes", None)
        st.session_state.uploader_version += 1
        st.rerun()

# --- Preferences ---
with tab_prefs:
    st.write("These shape the recommendations.")
    profiles_raw = st.text_input(
        "Liked flavor profiles (comma-separated)",
        value=", ".join(prefs.liked_profiles), placeholder="caramel, oak, spice",
    )
    col1, col2 = st.columns(2)
    pmin = col1.number_input(
        "Min proof", 80.0, 160.0,
        float(prefs.preferred_proof_min) if prefs.preferred_proof_min else 90.0,
    )
    pmax = col2.number_input(
        "Max proof", 80.0, 160.0,
        float(prefs.preferred_proof_max) if prefs.preferred_proof_max else 120.0,
    )

    if st.button("Save preferences", type="primary"):
        db["users"][current_user]["preferences"] = {
            "liked_profiles": [p.strip() for p in profiles_raw.split(",") if p.strip()],
            "preferred_proof_min": pmin,
            "preferred_proof_max": pmax,
            "favorite_bottles": prefs.favorite_bottles,
        }
        save_db(db)
        st.success("Saved.")
        st.rerun()

    st.divider()
    with st.expander("Change my password"):
        cp_current = st.text_input("Current password", type="password", key="cp_current")
        cp_new = st.text_input("New password", type="password", key="cp_new")
        cp_confirm = st.text_input("Confirm new password", type="password", key="cp_confirm")
        if st.button("Update password"):
            if not verify_user(db, current_user, cp_current):
                st.error("Current password is incorrect.")
            elif len(cp_new) < 6:
                st.error("New password must be at least 6 characters.")
            elif cp_new != cp_confirm:
                st.error("New passwords don't match.")
            else:
                set_password(db, current_user, cp_new)
                # Changing password should also invalidate all "remember me"
                # tokens for safety — same reason most apps do this.
                revoke_all_sessions(current_user)
                _ctrl = get_cookie_controller()
                if _ctrl is not None:
                    try:
                        _ctrl.remove(SESSION_COOKIE_NAME)
                    except Exception:
                        pass
                st.success("Password updated. You've been signed out of all other devices.")

    with st.expander("Sign out from all devices"):
        st.caption(
            "Use this if you've lost a phone or signed in somewhere shared. "
            "It revokes every 'remember me' session for your account, including this one."
        )
        if st.button("Sign out from all devices", key="signout_all"):
            count = revoke_all_sessions(current_user)
            _ctrl = get_cookie_controller()
            if _ctrl is not None:
                try:
                    _ctrl.remove(SESSION_COOKIE_NAME)
                except Exception:
                    pass
            st.session_state.user = None
            st.session_state.pop("session_token", None)
            st.toast(
                f"Signed out from {count} device{'s' if count != 1 else ''}.",
                icon="🔒",
            )
            st.rerun()

# --- Friends ---
with tab_friends:
    others = list_other_users(db, current_user)

    # Sub-tabs to keep the friend view, trade proposing, and trade tracking separate
    sub_view, sub_inbox, sub_outbox, sub_progress, sub_history = st.tabs([
        "Browse friends", "Inbox", "Sent", "In progress", "History"
    ])

    # ---- Helper: render a list of bottle items in a trade compactly ----
    def _render_items(items: List[Dict], owner_label: str):
        if not items:
            st.caption(f"_{owner_label} offers nothing._")
            return
        for it in items:
            qty = int(it.get("quantity", 1))
            qty_label = f" × {qty}" if qty > 1 else ""
            st.markdown(f"- **{it['bottle_name']}**{qty_label}")

    # ---- BROWSE: pick a friend, see their shelf, propose a trade ----
    with sub_view:
        if not others:
            st.info("No other users yet. Share your invite code to get friends on board.")
        else:
            friend_display = st.selectbox(
                "View friend's shelf",
                options=[""] + others,
                key="friend_view_select",
            )
            if friend_display:
                friend_key = normalize_username(friend_display)
                friend_bottles_all = [
                    b for b in get_user_bottles(db, friend_display) if b.quantity > 0
                ]
                friend_sealed = [b for b in friend_bottles_all if b.sealed]

                colA, colB = st.columns([3, 2])
                colA.caption(f"{friend_display}'s shelf — read only")
                if friend_sealed:
                    if colB.button(
                        "🤝 Propose a trade",
                        type="primary",
                        use_container_width=True,
                        key=f"open_trade_{friend_key}",
                    ):
                        st.session_state["trade_target"] = friend_key
                        st.session_state["trade_target_display"] = friend_display
                        # Reset selections from any prior session
                        st.session_state.pop("trade_request_picks", None)
                        st.session_state.pop("trade_offer_picks", None)
                        st.session_state.pop("trade_message", None)
                        st.rerun()
                else:
                    colB.caption("_No sealed bottles to trade for._")

                if not friend_bottles_all:
                    st.caption(f"{friend_display} hasn't added any bottles yet.")
                else:
                    for b in friend_bottles_all:
                        with st.container(border=True):
                            title = f"**{b.name}**"
                            if b.private_pick and b.pick_group:
                                title += f" — _{b.pick_group} pick_"
                            st.markdown(title)
                            st.caption(
                                f"{b.type} · {b.proof}° · {b.fill_percent:.0f}% full · "
                                f"{'sealed 🔒' if b.sealed else 'open'} · qty {b.quantity}"
                            )
                            if b.my_tasting_notes:
                                st.caption(f"Their notes: {', '.join(b.my_tasting_notes)}")
                            elif b.world_tasting_notes:
                                st.caption(f"World's notes: {', '.join(b.world_tasting_notes)}")

            # Trade composer (appears below browser when a trade is in progress)
            if st.session_state.get("trade_target"):
                target_key = st.session_state["trade_target"]
                target_display = st.session_state.get("trade_target_display", target_key)

                st.markdown("---")
                st.markdown(f"### 🤝 Propose a trade with **{target_display}**")
                st.caption("Sealed bottles only on both sides.")

                # Their sealed bottles -> what you want
                their_sealed = sealed_bottles_for_user(db, target_display)
                # Your sealed bottles -> what you'll offer
                my_sealed = sealed_bottles_for_user(db, current_user)

                col_req, col_off = st.columns(2)
                with col_req:
                    st.markdown(f"#### What you want from {target_display}")
                    if not their_sealed:
                        st.caption("_They have no sealed bottles right now._")
                        request_items = []
                    else:
                        request_items = []
                        for b in their_sealed:
                            label = b.name + (f" — {b.pick_group} pick" if b.private_pick and b.pick_group else "")
                            label += f"  ({b.proof:.0f}°)"
                            picked = st.checkbox(
                                label,
                                key=f"req_pick_{b.id}",
                            )
                            if picked:
                                qty = 1
                                if b.quantity > 1:
                                    qty = st.number_input(
                                        f"  Quantity (they have {b.quantity})",
                                        min_value=1, max_value=b.quantity, value=1,
                                        key=f"req_qty_{b.id}",
                                    )
                                request_items.append({
                                    "bottle_id": b.id,
                                    "bottle_name": b.name,
                                    "quantity": int(qty),
                                })

                with col_off:
                    st.markdown(f"#### What you'll offer")
                    if not my_sealed:
                        st.caption("_You have no sealed bottles to offer._")
                        offer_items = []
                    else:
                        offer_items = []
                        for b in my_sealed:
                            label = b.name + (f" — {b.pick_group} pick" if b.private_pick and b.pick_group else "")
                            label += f"  ({b.proof:.0f}°)"
                            picked = st.checkbox(
                                label,
                                key=f"off_pick_{b.id}",
                            )
                            if picked:
                                qty = 1
                                if b.quantity > 1:
                                    qty = st.number_input(
                                        f"  Quantity (you have {b.quantity})",
                                        min_value=1, max_value=b.quantity, value=1,
                                        key=f"off_qty_{b.id}",
                                    )
                                offer_items.append({
                                    "bottle_id": b.id,
                                    "bottle_name": b.name,
                                    "quantity": int(qty),
                                })

                msg = st.text_area(
                    "Optional message",
                    placeholder="Anything you want to say with the offer?",
                    key="trade_message",
                )

                send_col, cancel_col = st.columns([3, 1])
                if send_col.button(
                    f"📨 Send offer to {target_display}",
                    type="primary",
                    use_container_width=True,
                    key="send_trade_btn",
                ):
                    if not request_items and not offer_items:
                        st.error("Pick at least one bottle on either side.")
                    elif not request_items:
                        st.error("Pick at least one bottle you want from them.")
                    elif not offer_items:
                        st.error("Pick at least one bottle to offer.")
                    else:
                        create_trade(
                            db,
                            from_user=current_user,
                            to_user=target_key,
                            offered=offer_items,
                            requested=request_items,
                            message=msg or "",
                        )
                        st.session_state.pop("trade_target", None)
                        st.session_state.pop("trade_target_display", None)
                        st.toast(f"Offer sent to {target_display} 🤝", icon="📨")
                        st.rerun()

                if cancel_col.button("Cancel", key="cancel_compose"):
                    st.session_state.pop("trade_target", None)
                    st.session_state.pop("trade_target_display", None)
                    st.rerun()

    # ---- INBOX: trades sent TO the current user that are pending ----
    with sub_inbox:
        pending_in = [
            t for t in trades_for_user(db, current_user, statuses=["pending"])
            if t["to_user"] == normalize_username(current_user)
        ]
        if not pending_in:
            st.caption("No pending offers right now.")
        else:
            st.caption(f"You have **{len(pending_in)}** offer{'s' if len(pending_in) != 1 else ''} waiting on you.")

        for t in pending_in:
            sender_display = display_name_for(db, t["from_user"])
            with st.container(border=True):
                st.markdown(f"### From **{sender_display}**")
                if t.get("message"):
                    st.markdown(f"_\"{t['message']}\"_")

                col_o, col_r = st.columns(2)
                with col_o:
                    st.markdown(f"**They give you:**")
                    _render_items(t["offered"], sender_display)
                with col_r:
                    st.markdown(f"**You give them:**")
                    _render_items(t["requested"], "You")

                accept_col, decline_col, counter_col = st.columns(3)
                if accept_col.button("✅ Accept", key=f"acc_{t['id']}", type="primary", use_container_width=True):
                    err = accept_trade(db, t["id"], current_user)
                    if err:
                        st.error(err)
                    else:
                        st.toast("Trade accepted 🤝", icon="🥃")
                        st.rerun()
                if decline_col.button("❌ Decline", key=f"dec_{t['id']}", use_container_width=True):
                    err = decline_trade(db, t["id"], current_user)
                    if err:
                        st.error(err)
                    else:
                        st.rerun()
                if counter_col.button("↔️ Counter", key=f"cnt_btn_{t['id']}", use_container_width=True):
                    st.session_state["countering_trade_id"] = t["id"]
                    st.rerun()

                # Inline counter-offer composer
                if st.session_state.get("countering_trade_id") == t["id"]:
                    st.markdown("---")
                    st.markdown("**Build your counter-offer**")
                    st.caption("Sealed bottles only.")
                    sender_sealed = sealed_bottles_for_user(db, t["from_user"])
                    my_sealed = sealed_bottles_for_user(db, current_user)

                    cc_req, cc_off = st.columns(2)
                    new_requested = []
                    new_offered = []

                    with cc_req:
                        st.markdown(f"_What you want from {sender_display}:_")
                        for b in sender_sealed:
                            label = f"{b.name} ({b.proof:.0f}°)"
                            if st.checkbox(label, key=f"cnt_req_{t['id']}_{b.id}"):
                                qty = 1
                                if b.quantity > 1:
                                    qty = st.number_input(
                                        f"  Qty (they have {b.quantity})",
                                        min_value=1, max_value=b.quantity, value=1,
                                        key=f"cnt_req_qty_{t['id']}_{b.id}",
                                    )
                                new_requested.append({
                                    "bottle_id": b.id,
                                    "bottle_name": b.name,
                                    "quantity": int(qty),
                                })

                    with cc_off:
                        st.markdown("_What you'll offer:_")
                        for b in my_sealed:
                            label = f"{b.name} ({b.proof:.0f}°)"
                            if st.checkbox(label, key=f"cnt_off_{t['id']}_{b.id}"):
                                qty = 1
                                if b.quantity > 1:
                                    qty = st.number_input(
                                        f"  Qty (you have {b.quantity})",
                                        min_value=1, max_value=b.quantity, value=1,
                                        key=f"cnt_off_qty_{t['id']}_{b.id}",
                                    )
                                new_offered.append({
                                    "bottle_id": b.id,
                                    "bottle_name": b.name,
                                    "quantity": int(qty),
                                })

                    counter_msg = st.text_area(
                        "Note (optional)",
                        key=f"cnt_msg_{t['id']}",
                        placeholder="Why this works better for you, etc.",
                    )

                    send_cnt, cancel_cnt = st.columns([3, 1])
                    if send_cnt.button(
                        "📨 Send counter-offer",
                        type="primary",
                        key=f"cnt_send_{t['id']}",
                        use_container_width=True,
                    ):
                        if not new_requested or not new_offered:
                            st.error("Pick at least one bottle on each side.")
                        else:
                            err = counter_trade(
                                db, t["id"], current_user,
                                new_offered=new_offered,
                                new_requested=new_requested,
                                message=counter_msg or "",
                            )
                            if err:
                                st.error(err)
                            else:
                                st.session_state.pop("countering_trade_id", None)
                                st.toast("Counter-offer sent ↔️", icon="📨")
                                st.rerun()
                    if cancel_cnt.button("Cancel", key=f"cnt_cancel_{t['id']}"):
                        st.session_state.pop("countering_trade_id", None)
                        st.rerun()

    # ---- SENT: trades the current user has open with others ----
    with sub_outbox:
        pending_out = [
            t for t in trades_for_user(db, current_user, statuses=["pending"])
            if t["from_user"] == normalize_username(current_user)
        ]
        if not pending_out:
            st.caption("Nothing waiting on a friend's response.")
        for t in pending_out:
            recipient_display = display_name_for(db, t["to_user"])
            with st.container(border=True):
                st.markdown(f"### To **{recipient_display}**")
                if t.get("message"):
                    st.markdown(f"_\"{t['message']}\"_")

                col_o, col_r = st.columns(2)
                with col_o:
                    st.markdown("**You're offering:**")
                    _render_items(t["offered"], "You")
                with col_r:
                    st.markdown(f"**You want from {recipient_display}:**")
                    _render_items(t["requested"], recipient_display)

                if st.button("🚫 Cancel offer", key=f"cancel_{t['id']}"):
                    err = cancel_trade(db, t["id"], current_user)
                    if err:
                        st.error(err)
                    else:
                        st.rerun()

    # ---- IN PROGRESS: trades agreed to but not yet completed ----
    with sub_progress:
        my_key = normalize_username(current_user)
        active = [
            t for t in trades_for_user(db, current_user, statuses=["accepted"])
        ]
        if not active:
            st.caption(
                "No trades in progress. When an offer is accepted, it shows up here "
                "until both sides confirm the bottles have changed hands."
            )

        for t in active:
            is_from = t["from_user"] == my_key
            other_key = t["to_user"] if is_from else t["from_user"]
            other_display = display_name_for(db, other_key)

            # What "I" send vs receive based on perspective
            if is_from:
                i_send = t["offered"]
                i_receive = t["requested"]
                i_shipped = t.get("from_shipped", False)
                i_received = t.get("from_received", False)
                they_shipped = t.get("to_shipped", False)
                they_received = t.get("to_received", False)
            else:
                i_send = t["requested"]
                i_receive = t["offered"]
                i_shipped = t.get("to_shipped", False)
                i_received = t.get("to_received", False)
                they_shipped = t.get("from_shipped", False)
                they_received = t.get("from_received", False)

            with st.container(border=True):
                st.markdown(f"### Trade with **{other_display}**")
                if t.get("message"):
                    st.markdown(f"_\"{t['message']}\"_")

                col_i, col_t = st.columns(2)
                with col_i:
                    st.markdown("**You send:**")
                    _render_items(i_send, "You")
                    if i_shipped:
                        st.success("✅ You marked these shipped")
                    else:
                        st.caption("⏳ Not yet marked shipped")
                with col_t:
                    st.markdown(f"**You receive (from {other_display}):**")
                    _render_items(i_receive, other_display)
                    if they_shipped:
                        st.info(f"📦 {other_display} marked shipped — waiting on you to confirm received")
                    else:
                        st.caption(f"⏳ {other_display} hasn't shipped yet")

                # Action buttons
                act_cols = st.columns(3)
                if not i_shipped:
                    if act_cols[0].button(
                        "📦 I shipped my side",
                        key=f"ship_{t['id']}",
                        type="primary",
                        use_container_width=True,
                    ):
                        err = mark_shipped(db, t["id"], current_user)
                        if err:
                            st.error(err)
                        else:
                            st.toast("Marked as shipped 📦", icon="✅")
                            st.rerun()
                else:
                    act_cols[0].caption("_Shipped ✓_")

                # Receive button only enabled if other party has shipped
                if not i_received:
                    if act_cols[1].button(
                        "🥃 I got the bottles",
                        key=f"recv_{t['id']}",
                        type="primary",
                        use_container_width=True,
                        disabled=not they_shipped,
                    ):
                        err = mark_received(db, t["id"], current_user)
                        if err:
                            st.error(err)
                        else:
                            st.toast("Confirmed received 🥃", icon="🤝")
                            st.rerun()
                else:
                    act_cols[1].caption("_Received ✓_")

                if act_cols[2].button(
                    "🚫 Abandon trade",
                    key=f"abandon_{t['id']}",
                    use_container_width=True,
                ):
                    err = abandon_trade(db, t["id"], current_user)
                    if err:
                        st.error(err)
                    else:
                        st.toast("Trade abandoned", icon="🚫")
                        st.rerun()

                # Visual progress summary
                steps_done = sum([i_shipped, i_received, they_shipped, they_received])
                st.caption(f"Handoff progress: **{steps_done} / 4** steps done")

    # ---- HISTORY: closed trades (completed, declined, canceled, countered, abandoned) ----
    with sub_history:
        archived = [
            t for t in trades_for_user(db, current_user)
            if t["status"] in ("completed", "declined", "canceled", "countered", "abandoned")
        ]
        if not archived:
            st.caption("No completed trades yet.")
        # Sort newest first
        archived.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

        status_emoji = {
            "completed": "🤝",
            "declined": "❌",
            "canceled": "🚫",
            "countered": "↔️",
            "abandoned": "🪦",
        }
        for t in archived:
            other = t["to_user"] if t["from_user"] == normalize_username(current_user) else t["from_user"]
            other_display = display_name_for(db, other)
            direction = "→" if t["from_user"] == normalize_username(current_user) else "←"
            emoji = status_emoji.get(t["status"], "•")

            with st.expander(
                f"{emoji}  **{t['status'].title()}**  {direction}  {other_display}  "
                f"·  {t.get('updated_at', '')[:10]}"
            ):
                if t.get("message"):
                    st.markdown(f"_\"{t['message']}\"_")
                col_o, col_r = st.columns(2)
                from_display = display_name_for(db, t["from_user"])
                to_display = display_name_for(db, t["to_user"])
                with col_o:
                    st.markdown(f"**{from_display} offered:**")
                    _render_items(t["offered"], from_display)
                with col_r:
                    st.markdown(f"**{to_display} would have given:**")
                    _render_items(t["requested"], to_display)
                if t.get("counter_to_id"):
                    st.caption("_Counter-offer to a previous trade._")

# --- Admin (only visible to admin user) ---
if tab_admin is not None:
    with tab_admin:
        st.write("**Admin tools** — visible only to you.")
        st.caption(
            "Use this when a friend forgets their password. Generate a temp "
            "password, share it with them, and tell them to change it from "
            "Preferences → Change my password after signing in."
        )
        st.divider()

        all_users = sorted(db["users"].keys())
        target = st.selectbox(
            "Reset password for user",
            options=[""] + all_users,
            format_func=lambda k: display_name_for(db, k) if k else "— pick a user —",
        )

        if target:
            st.caption(f"Selected: **{display_name_for(db, target)}**")

            mode = st.radio(
                "Password",
                ["Generate a temporary password", "Set a specific password"],
                horizontal=True,
            )

            specific = ""
            if mode == "Set a specific password":
                specific = st.text_input(
                    "New password (at least 6 chars)",
                    type="password",
                    key="admin_pw",
                )

            if st.button("Reset password", type="primary"):
                if target == current_user:
                    st.error(
                        "Use Preferences → Change my password to change your own. "
                        "Resetting yourself here is blocked to avoid mistakes."
                    )
                elif mode == "Generate a temporary password":
                    temp = secrets.token_urlsafe(9)
                    set_password(db, target, temp)
                    revoke_all_sessions(target)
                    st.success("Password reset. Share this with the user:")
                    st.code(temp, language=None)
                    st.caption(
                        "This temp password is shown once. Copy it now — refreshing "
                        "the page will not show it again. The user has also been signed "
                        "out of all their devices."
                    )
                else:
                    if len(specific) < 6:
                        st.error("Password must be at least 6 characters.")
                    else:
                        set_password(db, target, specific)
                        revoke_all_sessions(target)
                        st.success(
                            f"Password updated for {display_name_for(db, target)}. "
                            f"They've been signed out of all their devices."
                        )

        st.divider()
        st.caption(f"Total users: **{len(all_users)}**")
