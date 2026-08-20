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
/home/ubuntu/bot/urkocoin/.venv/bin/python add_account.py <nama> <nomor_hp>
# contoh: python add_account.py alt1 +62812xxxxxxx
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

## Dashboard

- Header: total LUMIS semua akun + estimasi $/jam + $/hari
- Per akun: LUMIS, rank, streak, slots, produksi/jam, delta session
- Per monster: level, personality, bar food/hygiene/energy, prod/jam, XP progress
- Eggs: status incubation + sisa waktu
- Activity log: 10 aksi terakhir

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

## Catatan

- Turnstile solver lokal di `localhost:8001` harus jalan (dari skill global-captcha)
- Slot 2 butuh 3 ads manual di app (gak bisa via API, dan kita gak fake ads)
- Jangan share `accounts.json` / `sessions/` — itu credential penuh akun TG
