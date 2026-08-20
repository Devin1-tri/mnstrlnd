# Monsterland Farm Bot — Multi-Account + Dashboard

Bot farming LUMIS untuk Monsterland (Telegram Mini App). Multi-akun, dashboard Rich TUI, jalan manual di `screen`.

## Quick Start

```bash
cd /home/ubuntu/bot/monsterland
./.venv/bin/python monster_live.py
```

Setup pertama kali (atau kalau deps berubah):
```bash
cd /home/ubuntu/bot/monsterland
uv venv --python 3.11 .venv
uv pip install -r requirements.txt --python .venv/bin/python
```

Jalankan di screen:
```bash
screen -dmS monster -T xterm-256color bash -c 'cd /home/ubuntu/bot/monsterland && ./.venv/bin/python monster_live.py; exec bash'
screen -r monster        # lihat dashboard (detach: Ctrl+A D)
```

## Multi-Akun

Akun terdaftar di `accounts.json` (auto-migrate dari setup lama saat pertama run).

**Tambah akun baru:**
```bash
./.venv/bin/python add_account.py <nama> <nomor_hp>
# contoh: ./.venv/bin/python add_account.py alt1 +62812xxxxxxx
# -> masukin OTP (dan 2FA kalau aktif) -> session tersimpan di sessions/<nama>.session
```

**Pilih akun tertentu:**
```bash
python monster_live.py --accounts main,alt1
```

**Disable akun:** edit `accounts.json`, set `"enabled": false`.

## Opsi CLI

| Flag | Fungsi |
|---|---|
| `--accounts a,b` | cuma farm akun tertentu |
| `--interval 300` | cycle tiap N detik (default 240) |
| `--no-chat` | matikan chat XP |
| `--once` | 1 cycle lalu exit (buat test) |

## Yang Diotomasi (per cycle, per akun)

1. **Auth** — initData fresh via Telethon WebView + Turnstile (re-auth tiap 30 menit, initData TTL pendek)
2. **Streak** — claim daily streak otomatis
3. **Vitals** — jaga food≥80, hyg≥30, energy≥30 (zona multiplier max). Pakai inventory dulu, baru beli item termurah
4. **Level-up** — auto kalau LUMIS cukup (cost = `3 × baseRate × level^1.5`, reserve 1000)
5. **Egg pipeline** — hatch egg selesai → incubate egg owned → beli mystery egg (50k) kalau slot kosong + mampu
6. **Chat XP** — 1 pesan/cycle/monster sampai cap 10/hari (+12 XP/pesan)
7. **Wake** — bangunin monster tidur (mining stop saat tidur)
8. **Smart Rush Mode** — akun dengan monster level < 3 otomatis masuk rush mode (reserve 500, food target 50, level-up lebih agresif). Begitu capai L3, auto switch balik normal mode. Akun yang udah ≥ L3 gak tersentuh.

## Dashboard

- Header: total LUMIS semua akun + estimasi $/jam + $/hari
- Per akun: LUMIS, rank, streak, slots, produksi/jam, delta session
- Per monster: level, personality, bar food/hygiene/energy, prod/jam, XP progress
- Eggs: status incubation + sisa waktu
- Activity log: 10 aksi terakhir (clean, tanpa spam auth)

## Formula (extracted dari JS, verified live)

- Produksi = `baseRate × levelMult × foodMult × hygMult × energyMult × personalityBonus`
- Food: ≥80→1.25x, ≥50→1.0x, ≥20→0.5x, <20→0.01x
- Hygiene: ≥30→1.0x, else 0.5x | Energy: ≥30→1.0x, ≥1→0.6x, else 0.01x
- Level mult: L1=1.0 ... L10=2.6 ... L25=8.9
- Zoomer personality: 1.5x produksi
- Decay: food 20/jam, hyg 12.5/jam, energy 16.67/jam
- 1M LUMIS = $1, min WD 500k

## File

| File | Fungsi |
|---|---|
| `monster_live.py` | **MAIN** — dashboard + engine multi-akun |
| `monster_api.py` | API client + account registry |
| `add_account.py` | login akun TG baru (OTP/2FA) |
| `accounts.json` | daftar akun (chmod 600) |
| `sessions/` | Telethon session per akun |
| `monster_daemon.py` | daemon lama (headless, tanpa dashboard) — superseded |

## Troubleshooting

### ❌ "failed to solve turnstile" — semua akun stuck re-auth

**Penyebab:** Local Turnstile solver (port 8001) mati atau belum jalan. Bot butuh solver ini buat re-auth setiap 30 menit. Tanpa solver, semua akun gagal bootstrap dan dashboard tampil 0 LUMIS / ERROR.

**Fix:**
```bash
# 1. Setup solver (sekali aja)
cd /home/ubuntu/.hermes/skills/automation/global-captcha
python3 -m venv .venv
.venv/bin/pip install loguru fastapi uvicorn camoufox playwright Pillow
.venv/bin/python -m camoufox fetch

# 2. Start solver (background)
screen -dmS captcha bash -c 'cd /home/ubuntu/.hermes/skills/automation/global-captcha && .venv/bin/python run_captcha_solver.py; exec bash'

# 3. Verify solver live (harus return 200)
curl -s -o /dev/null -w "%{http_code}" http://localhost:8001/docs

# 4. Restart monster bot
screen -S monster -X quit
cd /home/ubuntu/bot/monsterland
screen -dmS monster -T xterm-256color bash -c './.venv/bin/python monster_live.py; exec bash'
```

Bot auto-recover di cycle berikutnya setelah solver live. **Solver harus tetap jalan** — kalau VPS reboot, start ulang solver dulu sebelum bot.

### ❌ "No module named 'telethon'"

**Penyebab:** Dependencies belum terinstall di venv.

**Fix:**
```bash
cd /home/ubuntu/bot/monsterland
pip install -r requirements.txt   # jangan lupa flag -r!
```

### ⚠️ Activity log penuh spam auth

Log turnstile (`solving turnstile...`, `turnstile ok`) sudah dihilangkan dari code. Hanya error yang muncul. Kalau masih rame, restart bot biar patch aktif.

## Catatan

- Turnstile solver lokal di `localhost:8001` **wajib jalan** (dari skill global-captcha). Lihat Troubleshooting di atas.
- Slot 2 butuh 3 ads manual di app (gak bisa via API, dan kita gak fake ads)
- Jangan share `accounts.json` / `sessions/` — itu credential penuh akun TG
