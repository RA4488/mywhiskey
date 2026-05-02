"""
Whiskey Recommendation App - Multi-user version
Run locally:   streamlit run app.py
Deploy free:   push to GitHub -> share.streamlit.io

Secrets needed (Streamlit Cloud Settings -> Secrets):
    anthropic_api_key = "sk-ant-..."
    signup_code = "your-shared-invite-code"   # anyone with this can register
    admin_username = "you"                     # optional: can manage signup code
"""

import base64
import hashlib
import json
import math
import random
import secrets
from dataclasses import dataclass, field
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
    tasting_notes: List[str]
    fill_percent: float
    opened: bool
    store_pick: Optional[bool] = False


@dataclass
class Preferences:
    liked_profiles: List[str] = field(default_factory=list)
    preferred_proof_min: Optional[float] = None
    preferred_proof_max: Optional[float] = None
    favorite_bottles: List[str] = field(default_factory=list)


# -----------------------------
# Persistence (JSON-backed; swap this layer for SQLite/Supabase later)
# -----------------------------
#
# Data shape:
# {
#   "users": {
#     "username": {
#       "password_hash": "...",
#       "salt": "...",
#       "bottles": [...],
#       "preferences": {...},
#       "recent_ids": [...]
#     }
#   }
# }

DATA_FILE = Path("data.json")


def load_db() -> Dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"users": {}}


def save_db(db: Dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(db, f, indent=2)


def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 100_000
    ).hex()


def create_user(db: Dict, username: str, password: str) -> None:
    salt = secrets.token_hex(16)
    db["users"][username] = {
        "password_hash": hash_password(password, salt),
        "salt": salt,
        "bottles": [],
        "preferences": {},
        "recent_ids": [],
    }
    save_db(db)


def verify_user(db: Dict, username: str, password: str) -> bool:
    user = db["users"].get(username)
    if not user:
        return False
    return hash_password(password, user["salt"]) == user["password_hash"]


def get_user_bottles(db: Dict, username: str) -> List[Bottle]:
    return [Bottle(**b) for b in db["users"][username].get("bottles", [])]


def get_user_prefs(db: Dict, username: str) -> Preferences:
    p = db["users"][username].get("preferences", {})
    return Preferences(
        liked_profiles=p.get("liked_profiles", []),
        preferred_proof_min=p.get("preferred_proof_min"),
        preferred_proof_max=p.get("preferred_proof_max"),
        favorite_bottles=p.get("favorite_bottles", []),
    )


def list_other_users(db: Dict, current_user: str) -> List[str]:
    return sorted(u for u in db["users"].keys() if u != current_user)


# -----------------------------
# Vision: identify bottle from photo
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
        "  confidence: your confidence 0-1 that you identified it correctly (number)\n"
        "  notes: short string explaining what you see\n"
        "If you cannot identify the bottle at all, return name as empty string."
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
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


# -----------------------------
# Scoring
# -----------------------------

FLAVOR_DIMENSIONS = ["oak", "caramel", "vanilla", "spice", "fruit", "sweet", "smoke", "herbal", "chocolate"]


def normalize(vec):
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def notes_to_vector(notes):
    vector = [0.0] * len(FLAVOR_DIMENSIONS)
    for note in notes:
        for i, dim in enumerate(FLAVOR_DIMENSIONS):
            if dim in note.lower():
                vector[i] += 1.0
    return normalize(vector)


def cosine_similarity(a, b):
    return sum(x * y for x, y in zip(a, b))


def flavor_score(bottle, prefs):
    if not prefs.liked_profiles:
        return 0.5
    return cosine_similarity(notes_to_vector(bottle.tasting_notes), notes_to_vector(prefs.liked_profiles))


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
    return 1.0 if bottle.opened else 0.3


def novelty_score(bottle, recent_ids):
    return 0.2 if bottle.id in recent_ids else 1.0


def build_reason(bottle, prefs):
    reasons = []
    if bottle.opened:
        reasons.append("already open")
    if bottle.fill_percent < 40:
        reasons.append("getting low")
    if (bottle.proof and prefs.preferred_proof_min is not None
            and prefs.preferred_proof_max is not None
            and prefs.preferred_proof_min <= bottle.proof <= prefs.preferred_proof_max):
        reasons.append("matches your preferred proof")
    if any(note in prefs.liked_profiles for note in bottle.tasting_notes):
        reasons.append("fits your flavor profile")
    return ", ".join(reasons) if reasons else "good match overall"


def recommend_bottles(inventory, prefs, mode, recent_ids, top_n=3):
    candidates = inventory.copy()
    if mode != "special":
        candidates = [b for b in candidates if b.opened or b.fill_percent < 50]
    if mode == "preservation":
        candidates = [b for b in candidates if b.fill_percent < 40]
    if not candidates:
        candidates = inventory.copy()

    scored = []
    for bottle in candidates:
        f = flavor_score(bottle, prefs)
        p = proof_score(bottle, prefs)
        fi = fill_score(bottle)
        o = opened_score(bottle)
        n = novelty_score(bottle, recent_ids)

        if mode == "random":
            total = random.random()
        elif mode == "special":
            total = (p * 0.3) + (f * 0.3) + (1 - fi) * 0.4
        else:
            total = f * 0.4 + p * 0.2 + fi * 0.2 + o * 0.1 + n * 0.1

        scored.append((bottle, total))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [{"bottle": b, "score": round(s, 3), "reason": build_reason(b, prefs)} for b, s in scored[:top_n]]


# -----------------------------
# Auth UI
# -----------------------------

st.set_page_config(page_title="What Should I Pour?", page_icon="🥃", layout="centered")

db = load_db()

if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.title("🥃 What Should I Pour?")

    auth_tab_login, auth_tab_signup = st.tabs(["Sign in", "Create account"])

    with auth_tab_login:
        u = st.text_input("Username", key="login_user")
        p = st.text_input("Password", type="password", key="login_pw")
        if st.button("Sign in", type="primary"):
            if verify_user(db, u.strip(), p):
                st.session_state.user = u.strip()
                st.rerun()
            else:
                st.error("Wrong username or password.")

    with auth_tab_signup:
        st.caption("You need an invite code to sign up. Ask whoever set this up for you.")
        new_u = st.text_input("Pick a username", key="signup_user")
        new_p = st.text_input("Pick a password", type="password", key="signup_pw")
        new_p2 = st.text_input("Confirm password", type="password", key="signup_pw2")
        code = st.text_input("Invite code", key="signup_code")
        if st.button("Create account", type="primary"):
            expected_code = st.secrets.get("signup_code", "")
            new_u_clean = new_u.strip()
            if not new_u_clean or not new_p:
                st.error("Username and password required.")
            elif new_p != new_p2:
                st.error("Passwords don't match.")
            elif len(new_p) < 6:
                st.error("Password must be at least 6 characters.")
            elif new_u_clean in db["users"]:
                st.error("That username is taken.")
            elif not expected_code:
                st.error("Signup is disabled (no invite code configured).")
            elif code.strip() != expected_code:
                st.error("Invalid invite code.")
            else:
                create_user(db, new_u_clean, new_p)
                st.session_state.user = new_u_clean
                st.success(f"Welcome, {new_u_clean}!")
                st.rerun()

    st.stop()


# -----------------------------
# Logged-in UI
# -----------------------------

current_user = st.session_state.user
inventory = get_user_bottles(db, current_user)
prefs = get_user_prefs(db, current_user)
recent_ids = db["users"][current_user].get("recent_ids", [])

header_col, logout_col = st.columns([4, 1])
header_col.title("🥃 What Should I Pour?")
header_col.caption(f"Signed in as **{current_user}**")
if logout_col.button("Sign out"):
    st.session_state.user = None
    st.session_state.pop("identified", None)
    st.rerun()

tab_recommend, tab_inventory, tab_add, tab_prefs, tab_friends = st.tabs(
    ["Recommend", "Inventory", "Add Bottle", "Preferences", "Friends"]
)

# --- Recommend ---
with tab_recommend:
    if not inventory:
        st.info("Add some bottles first.")
    else:
        mode = st.selectbox(
            "Mode", ["preference", "random", "special", "preservation"],
            help="preference: match your taste · random: surprise me · special: save the good stuff · preservation: drink what's getting low",
        )
        top_n = st.slider("How many suggestions?", 1, 5, 3)

        if st.button("Recommend", type="primary", use_container_width=True):
            results = recommend_bottles(inventory, prefs, mode, recent_ids, top_n)
            for r in results:
                b = r["bottle"]
                with st.container(border=True):
                    st.subheader(b.name)
                    st.caption(f"{b.type} · {b.proof}° proof · {b.fill_percent:.0f}% full")
                    st.write(f"**Why:** {r['reason']}")
                    st.write(f"**Notes:** {', '.join(b.tasting_notes)}")
                    if st.button("I poured this 🥃", key=f"pour_{b.id}"):
                        recent_ids = ([b.id] + recent_ids)[:10]
                        for bot in db["users"][current_user]["bottles"]:
                            if bot["id"] == b.id:
                                bot["opened"] = True
                                bot["fill_percent"] = max(0, bot["fill_percent"] - 5)
                        db["users"][current_user]["recent_ids"] = recent_ids
                        save_db(db)
                        st.success(f"Logged {b.name}. Cheers.")
                        st.rerun()

# --- Inventory ---
with tab_inventory:
    if not inventory:
        st.info("No bottles yet.")
    for b in inventory:
        with st.container(border=True):
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write(f"**{b.name}**")
                st.caption(f"{b.type} · {b.proof}°")
            with col2:
                st.write(f"{b.fill_percent:.0f}%")
                st.caption("open" if b.opened else "sealed")
            new_fill = st.slider("Fill %", 0, 100, int(b.fill_percent), key=f"fill_{b.id}")
            cols = st.columns(2)
            if cols[0].button("Update", key=f"upd_{b.id}"):
                for bot in db["users"][current_user]["bottles"]:
                    if bot["id"] == b.id:
                        bot["fill_percent"] = new_fill
                        bot["opened"] = new_fill < 100
                save_db(db)
                st.rerun()
            if cols[1].button("Remove", key=f"del_{b.id}"):
                db["users"][current_user]["bottles"] = [
                    x for x in db["users"][current_user]["bottles"] if x["id"] != b.id
                ]
                save_db(db)
                st.rerun()

# --- Add Bottle ---
with tab_add:
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

    photo = None
    if st.session_state.camera_open:
        photo = st.camera_input("Take a photo")

    uploaded = col_upload.file_uploader(
        "Upload image", type=["jpg", "jpeg", "png", "webp"], label_visibility="collapsed"
    )

    image_source = photo or uploaded

    if image_source is not None and st.button("Identify bottle from photo", type="primary"):
        with st.spinner("Reading the label..."):
            try:
                mime = image_source.type or "image/jpeg"
                result = identify_bottle_from_image(image_source.getvalue(), mime)
                st.session_state["identified"] = result
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
    notes_raw = st.text_input(
        "Tasting notes (comma-separated)", placeholder="caramel, vanilla, oak",
        help="You'll get better recommendations if you fill these in.",
    )
    fill = st.slider("Fill %", 0, 100, 100)
    opened = st.checkbox("Already opened?", value=False)
    store_pick = st.checkbox("Store pick?", value=False)

    col_save, col_clear = st.columns(2)
    if col_save.button("Save bottle", type="primary"):
        if not name.strip():
            st.error("Name required.")
        else:
            new_id = f"b_{int(random.random() * 100000)}"
            db["users"][current_user]["bottles"].append({
                "id": new_id,
                "name": name.strip(),
                "type": btype,
                "proof": proof,
                "tasting_notes": [n.strip() for n in notes_raw.split(",") if n.strip()],
                "fill_percent": float(fill),
                "opened": opened,
                "store_pick": store_pick,
            })
            save_db(db)
            st.session_state.pop("identified", None)
            st.success(f"Added {name}.")
            st.rerun()

    if col_clear.button("Clear photo result"):
        st.session_state.pop("identified", None)
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

# --- Friends (read-only view of other users' inventories) ---
with tab_friends:
    others = list_other_users(db, current_user)
    if not others:
        st.info("No other users yet. Share your invite code to get friends on board.")
    else:
        friend = st.selectbox("View friend's inventory", options=[""] + others)
        if friend:
            friend_bottles = get_user_bottles(db, friend)
            if not friend_bottles:
                st.caption(f"{friend} hasn't added any bottles yet.")
            else:
                st.caption(f"{friend}'s shelf — read only")
                for b in friend_bottles:
                    with st.container(border=True):
                        st.write(f"**{b.name}**")
                        st.caption(
                            f"{b.type} · {b.proof}° · {b.fill_percent:.0f}% full · "
                            f"{'open' if b.opened else 'sealed'}"
                        )
                        if b.tasting_notes:
                            st.caption(f"Notes: {', '.join(b.tasting_notes)}")
