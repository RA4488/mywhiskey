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
# Persistence (JSON; swap layer for SQLite/Supabase later)
# -----------------------------

DATA_FILE = Path("data.json")


def load_db() -> Dict:
    if not DATA_FILE.exists():
        return {"users": {}}
    try:
        with open(DATA_FILE) as f:
            db = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"users": {}}
    if "users" not in db or not isinstance(db.get("users"), dict):
        db = {"users": {}}

    # One-time migration: lowercase existing usernames, preserving original
    # capitalization as display_name. Strips whitespace from both keys and
    # display names. Skips any collision (first one wins).
    migrated = False
    new_users = {}
    for original_key, info in db["users"].items():
        new_key = original_key.strip().lower()
        # Backfill or clean up display_name
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

    return db


def save_db(db: Dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(db, f, indent=2)


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


# 1 mL = 0.033814 fl oz
ML_PER_OZ = 29.5735


def pour_to_fill_drop(pour_oz: float, size_ml: int) -> float:
    """Convert a pour in ounces to a fill-percent drop for a bottle of a given size."""
    if size_ml <= 0:
        return 0.0
    pour_ml = pour_oz * ML_PER_OZ
    return (pour_ml / size_ml) * 100


def log_pour(db: Dict, username: str, bottle_id: str, pour_oz: float) -> None:
    """Apply a pour: drop fill, mark unsealed, push to recent_ids."""
    user_key = normalize_username(username)
    user_record = db["users"][user_key]
    recent_ids = user_record.get("recent_ids", [])
    recent_ids = ([bottle_id] + [x for x in recent_ids if x != bottle_id])[:10]
    for bot in user_record["bottles"]:
        if bot["id"] == bottle_id:
            size_ml = bot.get("size_ml", 750)
            drop = pour_to_fill_drop(pour_oz, size_ml)
            bot["sealed"] = False
            bot["fill_percent"] = max(0, bot["fill_percent"] - drop)
            break
    user_record["recent_ids"] = recent_ids
    save_db(db)


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


def recommend_bottles(inventory, prefs, mode, recent_ids, top_n=3):
    # Always exclude bottles with quantity 0 from recommendations
    candidates = [b for b in inventory if b.quantity > 0]
    if mode != "special":
        candidates = [b for b in candidates if not b.sealed or b.fill_percent < 50]
    if mode == "preservation":
        candidates = [b for b in candidates if b.fill_percent < 40]
    if not candidates:
        candidates = [b for b in inventory if b.quantity > 0]

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

# --- Auth screens ---
if st.session_state.user is None:
    st.title("🥃 What Should I Pour?")

    auth_tab_login, auth_tab_signup = st.tabs(["Sign in", "Create account"])

    with auth_tab_login:
        u = st.text_input("Username", key="login_user")
        p = st.text_input("Password", type="password", key="login_pw")
        if st.button("Sign in", type="primary"):
            if verify_user(db, u, p):
                st.session_state.user = normalize_username(u)
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
                st.error("Signup is disabled (no invite code configured).")
            elif code.strip() != expected_code:
                st.error("Invalid invite code.")
            else:
                create_user(db, new_u_clean, new_p)
                st.session_state.user = new_u_key
                st.success(f"Welcome, {new_u_clean}!")
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
    st.session_state.user = None
    st.session_state.pop("identified", None)
    st.rerun()

admin_username = normalize_username(st.secrets.get("admin_username", ""))
is_admin = bool(admin_username) and current_user == admin_username

tab_labels = ["Recommend", "Inventory", "Add Bottle", "Preferences", "Friends"]
if is_admin:
    tab_labels.append("Admin")

tabs = st.tabs(tab_labels)
tab_recommend = tabs[0]
tab_inventory = tabs[1]
tab_add = tabs[2]
tab_prefs = tabs[3]
tab_friends = tabs[4]
tab_admin = tabs[5] if is_admin else None

# --- Recommend ---
with tab_recommend:
    if not [b for b in inventory if b.quantity > 0]:
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
                    title = b.name
                    if b.private_pick and b.pick_group:
                        title += f" — {b.pick_group} pick"
                    st.subheader(title)
                    st.caption(
                        f"{b.type} · {b.proof}° proof · {b.fill_percent:.0f}% full · "
                        f"qty {b.quantity}"
                    )
                    st.write(f"**Why:** {r['reason']}")
                    if b.my_tasting_notes:
                        st.write(f"**Your notes:** {', '.join(b.my_tasting_notes)}")
                    if b.world_tasting_notes:
                        st.caption(f"World's notes: {', '.join(b.world_tasting_notes)}")
                    # Pour size selector + log button
                    pour_oz = st.radio(
                        "Pour size (oz)",
                        options=[0.5, 1.0, 1.5, 2.0],
                        index=1,
                        horizontal=True,
                        key=f"pour_size_{b.id}",
                        format_func=lambda x: f"{x} oz",
                    )
                    if st.button("I poured this 🥃", key=f"pour_{b.id}"):
                        log_pour(db, current_user, b.id, pour_oz)
                        st.toast(f"Logged a {pour_oz} oz pour of {b.name}. Cheers.", icon="🥃")
                        st.rerun()

# --- Inventory ---
with tab_inventory:
    if st.button("➕ Add a Bottle", type="primary", use_container_width=True):
        # Streamlit tabs are buttons in the DOM. Find the one labeled "Add Bottle"
        # and click it programmatically so the user lands on the right tab.
        st.components.v1.html(
            """
            <script>
            (function() {
                const doc = window.parent.document;
                // Tabs are rendered as <button> elements with role="tab".
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

    show_zero = st.checkbox("Show out-of-stock bottles", value=True)
    visible = inventory if show_zero else [b for b in inventory if b.quantity > 0]

    for b in visible:
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

            # Quick pour log (only for in-stock bottles)
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

    uploaded = col_upload.file_uploader(
        "Upload image", type=["jpg", "jpeg", "png", "webp"], label_visibility="collapsed"
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

    fill = st.slider("Fill %", 0, 100, 100 if sealed else 90)

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
    )
    my_notes_raw = st.text_input(
        "My Tasting Notes",
        placeholder="What you actually taste",
        help="Your own notes — weighted more heavily in recommendations.",
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
            st.session_state.pop("identified", None)
            # Defer the toast and scroll until after the rerun so they fire on a
            # fresh, stable page (avoids the iframe-being-torn-down race).
            st.session_state["just_added_bottle"] = name.strip()
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
                st.success("Password updated.")

# --- Friends ---
with tab_friends:
    others = list_other_users(db, current_user)
    if not others:
        st.info("No other users yet. Share your invite code to get friends on board.")
    else:
        friend = st.selectbox("View friend's inventory", options=[""] + others)
        if friend:
            friend_bottles = [b for b in get_user_bottles(db, friend) if b.quantity > 0]
            if not friend_bottles:
                st.caption(f"{friend} hasn't added any bottles yet.")
            else:
                st.caption(f"{friend}'s shelf — read only")
                for b in friend_bottles:
                    with st.container(border=True):
                        title = f"**{b.name}**"
                        if b.private_pick and b.pick_group:
                            title += f" — _{b.pick_group} pick_"
                        st.markdown(title)
                        st.caption(
                            f"{b.type} · {b.proof}° · {b.fill_percent:.0f}% full · "
                            f"{'sealed' if b.sealed else 'open'} · qty {b.quantity}"
                        )
                        if b.my_tasting_notes:
                            st.caption(f"Their notes: {', '.join(b.my_tasting_notes)}")
                        elif b.world_tasting_notes:
                            st.caption(f"World's notes: {', '.join(b.world_tasting_notes)}")

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
                    st.success("Password reset. Share this with the user:")
                    st.code(temp, language=None)
                    st.caption(
                        "This temp password is shown once. Copy it now — refreshing "
                        "the page will not show it again."
                    )
                else:
                    if len(specific) < 6:
                        st.error("Password must be at least 6 characters.")
                    else:
                        set_password(db, target, specific)
                        st.success(
                            f"Password updated for {display_name_for(db, target)}."
                        )

        st.divider()
        st.caption(f"Total users: **{len(all_users)}**")
