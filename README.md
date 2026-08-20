# 🐾 Monsterland Farm Bot

Multi-account farming bot for **Monsterland** (Telegram Mini App). Auto-farms LUMIS tokens with a live Rich TUI dashboard, smart leveling, and full pet care automation.

> **LUMIS** is the withdrawable in-game currency (1,000,000 LUMIS = $1, min withdrawal 500k).

## ✨ Features

- **Multi-account** — farm unlimited Telegram accounts in parallel, each with its own session
- **Rich TUI dashboard** — live view of all accounts: LUMIS, rank, streak, pet vitals, production, XP, eggs
- **Smart Rush Mode** — new accounts (monster level < 3) automatically enter aggressive leveling mode, then switch back to normal mode at L3+
- **Full automation per cycle (default 4 min):**
  - 🔄 Auth refresh (initData via Telethon WebView + Cloudflare Turnstile, re-auth every 30 min)
  - 📅 Daily streak claim
  - 🍎 Vitals maintenance (food/hygiene/energy kept in max-multiplier zone, inventory-first spending)
  - ⬆️ Auto level-up when affordable
  - 🥚 Egg pipeline (hatch → incubate → buy)
  - 💬 Chat XP (10 messages/day per monster, +12 XP each)
  - ⏰ Auto wake sleeping monsters

## 📋 Requirements

| Requirement | Details |
|---|---|
| OS | Linux (tested on Ubuntu 22.04+) |
| Python | 3.11+ |
| RAM | ~100 MB for the bot + **500 MB–1.5 GB for the Turnstile solver** (browser-based) |
| Telegram API credentials | `api_id` + `api_hash` from https://my.telegram.org |
| Turnstile solver | Any service exposing the HTTP interface below (see [Turnstile Solver](#-turnstile-solver-setup)) |
| `screen` | For persistent background runs |

### Python dependencies

```
requests>=2.31
telethon>=1.36
rich>=13.7
```

## 🚀 Setup

### 1. Clone & create venv

```bash
git clone https://github.com/Devin1-tri/mnstrlnd.git
cd mnstrlnd
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Telegram credentials

Create `~/.tg_cred.env`:

```env
TG_API_ID=12345678
TG_API_HASH=your_api_hash_here
TG_PHONE=+62812xxxxxxxx
```

Get `api_id`/`api_hash` from https://my.telegram.org → API development tools.

### 3. Turnstile solver (see section below)

### 4. Add your first account

```bash
./.venv/bin/python add_account.py main +62812xxxxxxxx
# Enter the OTP code sent to your Telegram (type it in the terminal!)
# If 2FA is enabled, enter your password too
```

This creates `sessions/main.session` and registers the account in `accounts.json`.

### 5. Run

```bash
./.venv/bin/python monster_live.py
```

Or in a persistent screen session:

```bash
screen -dmS monster -T xterm-256color bash -c 'cd /path/to/mnstrlnd && ./.venv/bin/python monster_live.py; exec bash'
screen -r monster    # view dashboard (detach: Ctrl+A then D)
```

## 🔐 Turnstile Solver Setup

Monsterland's API requires a **Cloudflare Turnstile token** on every session bootstrap (initData TTL is short, so the bot re-authenticates every 30 minutes). The bot calls a local solver service and expects this HTTP interface:

```
GET http://localhost:8001/turnstile?url=<target_url>&sitekey=<sitekey>
→ {"task_id": "uuid", "status": "accepted"}

GET http://localhost:8001/result?id=<task_id>
→ {"status": "success", "value": "<turnstile_token>"}   # poll until success/failure
```

**Options:**

1. **Self-hosted browser solver** (free) — a FastAPI service running a stealth browser pool (e.g. Camoufox/Playwright) that renders the Turnstile widget and extracts the token. Configure it to listen on `localhost:8001` with the endpoints above.
2. **Paid solving services** — wrap 2Captcha/CapSolver in a tiny HTTP shim that exposes the same two endpoints. Monsterland's tokens are accepted cross-session, so external solvers work.

The solver URL is configurable via the `SOLVER` constant in `monster_api.py`.

**Running the solver persistently (screen):**

```bash
screen -dmS captcha bash -c 'cd /path/to/solver && .venv/bin/python run_captcha_solver.py; exec bash'
```

**RAM tip:** if self-hosting a browser-pool solver, keep the pool small — `thread: 1, page_count: 2` is enough for a handful of accounts (each solve takes ~4–15 s, re-auth only every 30 min per account). A 3×3 pool wastes ~1 GB RAM on an idle VPS.

⚠️ **The solver must be running before you start the bot** — otherwise all accounts will show `failed to solve turnstile`.

## 🎮 Usage

```bash
./.venv/bin/python monster_live.py                     # farm all enabled accounts
./.venv/bin/python monster_live.py --accounts main,alt1  # only specific accounts
./.venv/bin/python monster_live.py --interval 300      # 5-minute cycles
./.venv/bin/python monster_live.py --no-chat           # disable chat XP
./.venv/bin/python monster_live.py --once              # single cycle (testing)
```

### Adding more accounts

```bash
./.venv/bin/python add_account.py <name> <phone_number>
```

Each account gets its own Telethon session. Restart the bot to pick up new accounts.

> ⚠️ **Enter OTP codes in the terminal only.** Never paste Telegram OTP codes into chats — Telegram's anti-fraud may invalidate the login.

## 🧠 Smart Rush Mode

Accounts are automatically managed based on their highest monster level:

| Condition | Mode | Behavior |
|---|---|---|
| Max monster level **< 3** | 🔥 RUSH | Lower LUMIS reserve (500 vs 1000), lower food target (50 vs 80) — saves LUMIS for faster level-ups |
| Max monster level **≥ 3** | ✅ NORMAL | Full vitals optimization, conservative reserve |

The transition is automatic and per-account — established accounts are never affected by rush-mode logic.

## 📊 Game Formulas (extracted from JS, verified live)

```
Production = baseRate × levelMult × foodMult × hygMult × energyMult × personalityBonus
```

| Vital | Multiplier thresholds |
|---|---|
| Food | ≥80 → 1.25x · ≥50 → 1.0x · ≥20 → 0.5x · <20 → 0.01x |
| Hygiene | ≥30 → 1.0x · else 0.5x |
| Energy | ≥30 → 1.0x · ≥1 → 0.6x · else 0.01x |

- **Level multiplier:** L1=1.0 → L10=2.6 → L25=8.9
- **Personality bonus:** zoomer 1.5x, overlord 3.0x
- **Decay rates:** food −20/h, hygiene −12.5/h, energy −16.67/h
- **Level-up cost:** `floor(3 × baseRate × level^1.5)` LUMIS + XP requirement
- **Chat XP:** +12 XP per message, capped at 10 messages/day

## 📁 Files

| File | Purpose |
|---|---|
| `monster_live.py` | **Main** — TUI dashboard + multi-account farm engine |
| `monster_api.py` | API client, auth, account registry |
| `add_account.py` | Interactive Telegram login (OTP/2FA) |
| `requirements.txt` | Python dependencies |
| `accounts.json` | Account registry (auto-created, **never share**) |
| `sessions/` | Telethon sessions per account (**never share**) |

## 🔧 Troubleshooting

### ❌ `failed to solve turnstile` — all accounts stuck

The Turnstile solver on port 8001 is down or unreachable. Start/verify it:

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8001/docs   # expect 200
```

The bot auto-recovers on the next cycle once the solver is back. After a VPS reboot, start the solver **before** the bot.

### ❌ `No module named 'telethon'`

Dependencies not installed in the venv:

```bash
.venv/bin/pip install -r requirements.txt   # don't forget the -r flag!
```

### ❌ `pip install requirements.txt` → "No matching distribution"

Missing the `-r` flag — pip thinks you're installing a package literally named "requirements.txt". Use `pip install -r requirements.txt`.

### ⚠️ High RAM usage

The bot itself uses ~70–100 MB. Heavy RAM usage comes from the **solver's browser pool**. If self-hosting, keep the pool small (1–2 browser instances is enough for a handful of accounts — each solve takes ~4–15 s and re-auth only happens every 30 min per account).

### ⚠️ Dashboard shows `Layout()` boxes with >2 accounts

Fixed in current version (grid layout bug). Pull the latest code.

## 🔒 Security Notes

- `accounts.json`, `sessions/`, `initdata.txt`, and `~/.tg_cred.env` contain **full account credentials** — they are gitignored and must never be committed or shared
- The bot only talks to Monsterland's official API and Telegram — no fake ad impressions, no event fabrication
- Slot 2 monster unlocks require watching 3 ads **manually in the app** (no API path, and we don't fake ads)

## ⚖️ Disclaimer

This project is for educational purposes. Use at your own risk — automation may violate the game's Terms of Service and could result in account action. Not affiliated with Monsterland.
