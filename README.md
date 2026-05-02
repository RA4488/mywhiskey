# 🥃 What Should I Pour?

A whiskey shelf companion for me and my friends. Snap a photo of a bottle to add it, get smart recommendations based on the vibe, and never lose track of what's getting low.

## Features

- **Snap to add** — take a photo of a bottle and Claude reads the label: name, type, proof, sealed/opened state, private barrel pick info, and common tasting notes
- **Vibe-based recommendations** — pick a mood ("easy sipper before bed," "sharing with company," "want to focus and taste") instead of cryptic algorithm modes
- **Pour tracking** — log pours by ounce (0.5, 1, 1.5, 2 oz). Fill levels update automatically based on bottle size
- **Inventory management** — search across all fields, sort multiple ways, filter by quick chips (sealed only, running low, private picks), group by type, switch between compact list and detail card views
- **Multi-user with friend viewing** — each user has their own private shelf, but can read-only browse friends' inventories
- **Tasting notes split** — "World's Tasting Notes" (auto-filled by AI) vs "My Tasting Notes" (your own perception, weighted heavier in recommendations)
- **Quantity tracking** — track multiple bottles of the same thing; depleted bottles stay as history
- **Private pick detection** — barrel pick stickers and store selections are auto-detected and tagged

## How recommendations work

Pick a vibe and the app weights different signals:

- **Flavor match** — cosine similarity between bottle notes and your liked profiles
- **Proof match** — fits your preferred proof range
- **Crowd score** — lower proof + multiple bottles + not a private pick = good for sharing
- **Interesting score** — private picks + higher proof + rich notes = worth focused attention
- **Low proof score** — peaks around 90 proof, drops sharply above 105
- **Fill level** — bottles getting low surface for "drink it before it dies" moments
- **Novelty** — bottles you've poured recently get downweighted

Each vibe combines these differently. "Easy sipper before bed" caps proof at 105 and weights low-proof heavily. "Sharing with company" boosts crowd-friendliness. "Want to focus and taste" boosts interesting bottles.

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

If your username matches `admin_username` in secrets, you'll see an **Admin** tab. From there you can reset any user's password (generate a temporary one, share it, they change it from Preferences after signing in). Useful when a friend forgets their password.

## Data storage

Inventory and accounts are saved in `data.json` next to the app. Passwords are hashed with PBKDF2 + per-user salt.

**Important caveat for Streamlit Community Cloud:** the filesystem is ephemeral. After inactivity or redeploys, `data.json` resets and everyone loses their accounts. For real long-term use:

- **Option A (light):** commit `data.json` to the repo periodically as a backup
- **Option B (recommended):** swap the storage layer for Supabase or Turso (free tiers, ~20 lines to integrate). The `load_db`, `save_db`, `create_user`, `verify_user`, `get_user_bottles`, `get_user_prefs` functions are the only ones that touch storage — the rest of the app doesn't change.

## Tech

- [Streamlit](https://streamlit.io) for the UI
- [Anthropic API](https://docs.anthropic.com) for label/bottle identification (Claude Sonnet)
- [streamlit-back-camera-input](https://github.com/phamxtien/streamlit_back_camera_input) for rear-camera photo capture on phones
- Plain Python dataclasses + JSON for everything else

## Privacy

- No images are stored. Photos go to Claude's API, return as text, then the image is discarded.
- Per Anthropic's policy, API inputs aren't used for training.
- Passwords are never stored in plaintext.

## License

Personal project. Use it however you want.
