# 🥃 What Should I Pour?

A whiskey shelf companion for me and my friends. Snap a photo of a bottle to add it, get smart vibe-based recommendations, log every pour, trade sealed bottles with friends, and never lose track of what's getting low.

## What it does

- **Snap to add** — point your phone at a bottle, Claude reads the label and pre-fills name, type, proof, sealed/opened state, private barrel pick info, and common tasting notes
- **Vibe-based recommendations** — pick a mood (regular pour / nightcap / sharing with company / focused tasting / forgotten ones / surprise me) and get suggestions ranked for that situation
- **Learning over time** — the app builds personal affinity from your pour history; recently and frequently poured bottles surface higher in the modes that prefer favorites
- **At the Bar** — snap a photo of a bar's menu or backbar shelf, and the app identifies bottles and ranks them by fit to your taste, flagging ones you already own
- **Pour tracking** — log pours by ounce (0.5 / 1 / 1.5 / 2). Fill levels update based on actual bottle size (375 mL through 1.75 L)
- **Smart inventory** — search across all fields, sort multiple ways, filter by quick chips (sealed only, running low, private picks), group by type, switch between compact list and detail card views, paginate at 20+
- **Multi-user with sealed-bottle trading** — each user has their own private shelf; can browse friends' shelves read-only and propose trades with a full state machine (pending → accepted → ship → receive → completed) including counter-offers, abandonment, and history
- **Tasting notes split** — "World's Tasting Notes" (auto-filled by AI) vs "My Tasting Notes" (your own perception, weighted heavier in recommendations)
- **Quantity tracking** — handle multiple bottles of the same thing; depleted bottles preserved as history

## How recommendations work

Pick a vibe and the engine weights different signals:

| Signal | What it measures |
|---|---|
| Flavor match | Cosine similarity between bottle notes and your liked profiles |
| Proof match | Fits your preferred proof range |
| Crowd score | Lower proof + multiple bottles + not a private pick = good for sharing |
| Interesting score | Private picks + higher proof + rich notes = worth focused attention |
| Low proof score | Peaks around 90 proof, drops sharply above 105 |
| Fill level | Bottles getting low surface for "drink it before it dies" moments |
| Novelty | Recently poured bottles get downweighted |
| **Affinity** | Learned from pour history — recent + larger pours = stronger signal |

Each vibe combines these differently. "Easy sipper before bed" caps proof at 105 and weights low-proof heavily. "Sharing with company" boosts crowd-friendliness. "Want to focus and taste" intentionally ignores affinity (so it doesn't keep suggesting your favorites — the point is exploration). "The Forgotten Ones" finds bottles you haven't poured in 60+ days.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Open http://localhost:8501.

## Deploy free

1. Push to GitHub
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in with GitHub
3. **Create app**, point it at this repo, main file `app.py`
4. In the app's **Settings → Secrets**, add:
   ```
   anthropic_api_key = "sk-ant-your-key-here"
   signup_code = "your-shared-invite-code"
   admin_username = "your-username"
   ```
5. Share the URL + invite code with friends. On phones, "Add to Home Screen" makes it feel like a native app.

## Setup for friends

The app uses an invite-code signup flow. Anyone with the code can create an account; without it, signup is blocked. Share the code privately and rotate it any time by updating the secret.

## Admin tools

If your username matches `admin_username` in secrets, you'll see an **Admin** tab. From there you can reset any user's password (generate a temporary one, share it, they change it from Preferences after signing in).

## Data storage

Inventory, accounts, pour logs, and trades live in `data.json` next to the app. Passwords are hashed with PBKDF2 + per-user salt.

**Important caveat for Streamlit Community Cloud:** the filesystem is ephemeral. After inactivity or redeploys, `data.json` resets and everyone loses their accounts. For real long-term use:

- **Option A (light):** commit `data.json` to the repo periodically as a backup
- **Option B (recommended):** swap the storage layer for Supabase or Turso (free tiers, ~20 lines to integrate). The `load_db`, `save_db`, `create_user`, `verify_user`, `get_user_bottles`, `get_user_prefs`, `log_pour`, and trade helper functions are the only ones that touch storage.

## Tech

- [Streamlit](https://streamlit.io) — UI framework
- [Anthropic API](https://docs.claude.com) — bottle / menu / shelf identification (Claude Sonnet)
- [streamlit-back-camera-input](https://github.com/phamxtien/streamlit_back_camera_input) — rear-camera capture on phones
- Plain Python dataclasses + JSON file for everything else

## Privacy

- No images stored. Photos go to Claude's API, return as text, image is discarded.
- Per Anthropic policy, API inputs aren't used for training.
- Passwords never stored in plaintext.

## License

Personal project. Use it however you want.
