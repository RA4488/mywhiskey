"""
Whiskey Recommendation App - Streamlit version
Run locally:   streamlit run app.py
Deploy free:   push to GitHub -> share.streamlit.io
"""

import json
import math
import random
from dataclasses import dataclass, field, asdict
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
# Persistence (simple JSON file)
# -----------------------------

DATA_FILE = Path("data.json")


def load_data() -> Dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {"bottles": [], "preferences": {}, "recent_ids": []}


def save_data(data: Dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def bottles_from_data(data: Dict) -> List[Bottle]:
    return [Bottle(**b) for b in data["bottles"]]


def prefs_from_data(data: Dict) -> Preferences:
    p = data.get("preferences", {})
    return Preferences(
        liked_profiles=p.get("liked_profiles", []),
        preferred_proof_min=p.get("preferred_proof_min"),
        preferred_proof_max=p.get("preferred_proof_max"),
        favorite_bottles=p.get("favorite_bottles", []),
    )


# -----------------------------
# Scoring (from original code)
# -----------------------------

FLAVOR_DIMENSIONS = [
    "oak", "caramel", "vanilla", "spice",
    "fruit", "sweet", "smoke", "herbal", "chocolate"
]


def normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def notes_to_vector(notes: List[str]) -> List[float]:
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
    return cosine_similarity(
        notes_to_vector(bottle.tasting_notes),
        notes_to_vector(prefs.liked_profiles),
    )


def proof_score(bottle, prefs):
    if bottle.proof is None or prefs.preferred_proof_min is None or prefs.preferred_proof_max is None:
        return 0.5
    if prefs.preferred_proof_min <= bottle.proof <= prefs.preferred_proof_max:
        return 1.0
    distance = min(
        abs(bottle.proof - prefs.preferred_proof_min),
        abs(bottle.proof - prefs.preferred_proof_max),
    )
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
    return [
        {"bottle": b, "score": round(s, 3), "reason": build_reason(b, prefs)}
        for b, s in scored[:top_n]
    ]


# -----------------------------
# Streamlit UI
# -----------------------------

st.set_page_config(page_title="What Should I Pour?", page_icon="🥃", layout="centered")

# Simple shared-password gate (optional - delete this block to make public)
PASSWORD = st.secrets.get("password", "letmein")  # set in Streamlit Cloud secrets
if "auth" not in st.session_state:
    st.session_state.auth = False
if not st.session_state.auth:
    pw = st.text_input("Password", type="password")
    if st.button("Enter"):
        if pw == PASSWORD:
            st.session_state.auth = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()

# Load data
data = load_data()
inventory = bottles_from_data(data)
prefs = prefs_from_data(data)
recent_ids = data.get("recent_ids", [])

st.title("🥃 What Should I Pour?")

tab_recommend, tab_inventory, tab_add, tab_prefs = st.tabs(
    ["Recommend", "Inventory", "Add Bottle", "Preferences"]
)

# --- Recommend Tab ---
with tab_recommend:
    if not inventory:
        st.info("Add some bottles first.")
    else:
        mode = st.selectbox(
            "Mode",
            ["preference", "random", "special", "preservation"],
            help=(
                "preference: best match for your taste · "
                "random: surprise me · "
                "special: save the good stuff · "
                "preservation: drink what's getting low"
            ),
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
                    st.write(f"Score: `{r['score']}`")
                    if st.button(f"I poured this 🥃", key=f"pour_{b.id}"):
                        # Mark recent + drop fill a bit
                        recent_ids = ([b.id] + recent_ids)[:10]
                        for bot in data["bottles"]:
                            if bot["id"] == b.id:
                                bot["opened"] = True
                                bot["fill_percent"] = max(0, bot["fill_percent"] - 5)
                        data["recent_ids"] = recent_ids
                        save_data(data)
                        st.success(f"Logged {b.name}. Cheers.")
                        st.rerun()

# --- Inventory Tab ---
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
            new_fill = st.slider(
                "Fill %", 0, 100, int(b.fill_percent), key=f"fill_{b.id}"
            )
            cols = st.columns(2)
            if cols[0].button("Update", key=f"upd_{b.id}"):
                for bot in data["bottles"]:
                    if bot["id"] == b.id:
                        bot["fill_percent"] = new_fill
                        bot["opened"] = new_fill < 100
                save_data(data)
                st.rerun()
            if cols[1].button("Remove", key=f"del_{b.id}"):
                data["bottles"] = [x for x in data["bottles"] if x["id"] != b.id]
                save_data(data)
                st.rerun()

# --- Add Bottle Tab ---
with tab_add:
    name = st.text_input("Name", placeholder="Eagle Rare 10")
    btype = st.selectbox("Type", ["bourbon", "rye", "scotch", "rum", "other"])
    proof = st.number_input("Proof", 80.0, 160.0, 90.0, step=0.1)
    notes_raw = st.text_input(
        "Tasting notes (comma-separated)",
        placeholder="caramel, vanilla, oak",
    )
    fill = st.slider("Fill %", 0, 100, 100)
    opened = st.checkbox("Already opened?", value=False)
    store_pick = st.checkbox("Store pick?", value=False)

    if st.button("Add bottle", type="primary"):
        if not name.strip():
            st.error("Name required.")
        else:
            new_id = f"b_{len(data['bottles']) + 1}_{int(random.random() * 10000)}"
            data["bottles"].append({
                "id": new_id,
                "name": name.strip(),
                "type": btype,
                "proof": proof,
                "tasting_notes": [n.strip() for n in notes_raw.split(",") if n.strip()],
                "fill_percent": float(fill),
                "opened": opened,
                "store_pick": store_pick,
            })
            save_data(data)
            st.success(f"Added {name}.")
            st.rerun()

# --- Preferences Tab ---
with tab_prefs:
    st.write("These shape the recommendations.")
    profiles_raw = st.text_input(
        "Liked flavor profiles (comma-separated)",
        value=", ".join(prefs.liked_profiles),
        placeholder="caramel, oak, spice",
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
        data["preferences"] = {
            "liked_profiles": [p.strip() for p in profiles_raw.split(",") if p.strip()],
            "preferred_proof_min": pmin,
            "preferred_proof_max": pmax,
            "favorite_bottles": prefs.favorite_bottles,
        }
        save_data(data)
        st.success("Saved.")
        st.rerun()
