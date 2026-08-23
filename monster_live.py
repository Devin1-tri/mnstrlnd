#!/usr/bin/env python3
"""monster_live.py — Monsterland multi-account farm bot with Rich TUI dashboard.

Run:  cd /home/ubuntu/bot/monsterland && /home/ubuntu/bot/urkocoin/.venv/bin/python monster_live.py
Stop: Ctrl+C

Features:
  - Multi-account (accounts.json, each with own Telethon session)
  - Auto vitals maintenance (max multiplier zone: food>=80, hyg>=30, energy>=30)
  - Auto level-up, gated by egg priority + lifespan payback
  - Egg pipeline: free fragment eggs -> incubate -> hatch -> buy (real 1.5^n price)
  - Free XP: chat (12 XP x10/day); tiny_drop is ad-gated, intentionally skipped
  - Lifespan guard: expired miners are ascended to Mentors instead of fed
  - Auto streak claim
  - Row-based dashboard that scales to any number of accounts

Usage:
  python monster_live.py                    # all enabled accounts
  python monster_live.py --accounts main    # only 'main'
  python monster_live.py --interval 300     # 5-min cycles
  python monster_live.py --no-chat          # skip chat XP
  python monster_live.py --width 140        # force render width (wide terminals)
  python monster_live.py --once             # single cycle, then exit
"""
import argparse, math, random, sys, threading, time, traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from monster_api import MonsterAPI, Account, load_accounts

# ─── Game constants (extracted from JS, verified live) ───────────────
LEVEL_MULT = {1:1, 2:1.1, 3:1.2, 4:1.3, 5:1.5, 6:1.7, 7:1.9, 8:2.1, 9:2.3,
              10:2.6, 11:2.9, 12:3.2, 13:3.5, 14:3.8, 15:4.2, 16:4.6, 17:5,
              18:5.4, 19:5.8, 20:6.3, 21:6.8, 22:7.3, 23:7.8, 24:8.3, 25:8.9}
PERSONALITY_PROD = {'zoomer': 1.5, 'overlord': 3.0, 'starborn': 1.5}
LUMIS_PER_USD = 1_000_000
# Mystery egg price escalates per purchase: min(3M, 50k * 1.5^n) — n = profile.mystery_egg_lumis_purchases
EGG_BASE_COST = 50_000
EGG_PRICE_CAP = 3_000_000
LUMIS_RESERVE = 1_000
RUSH_RESERVE = 500       # lower reserve for rush mode (accounts below RUSH_LEVEL_THRESHOLD)
RUSH_LEVEL_THRESHOLD = 3 # accounts with max monster level < this enter rush mode automatically
REBOOTSTRAP_SEC = 1800   # initData TTL is short; re-auth every 30 min

# ── Egg-vs-levelup spend policy ──────────────────────────────────────
# A free slot is worth far more than a level: mystery egg EV ~1.76M LUMIS
# (60/27/9/3/1 % common/uncommon/rare/epic/mythic, each with a fresh 45d
# lifespan) vs a single level which only adds base*Δmult*1.25 per hour for
# the pet's REMAINING lifespan. So when a slot is free we save toward the egg.
# But holding forever is wrong too: levels compound income and make the egg
# arrive sooner. Rule: hold for the egg only once we're already close to it.
EGG_HOLD_RATIO = 0.60    # lumis >= 60% of egg price -> stop leveling, save for egg
# Lifespan: source_egg wins over rarity (see getMonsterLifespanMs in the bundle)
LIFESPAN_DAYS_BY_EGG = {'ancient_egg': 45, 'mystery_egg': 45,
                        'golden_egg': 75, 'celestial_egg': 95}
LIFESPAN_DAYS_BY_RARITY = {'common': 45, 'uncommon': 60, 'rare': 75,
                           'epic': 90, 'mythic': 100}
# Don't buy a level the pet won't live long enough to repay
LEVELUP_PAYBACK_MARGIN = 1.25   # need lifespan_left >= payback * margin
CHAT_DAILY_CAP = 10
EGG_FRAGMENT_TYPES = ['celestial_egg', 'golden_egg', 'mystery_egg']  # best first
FRAGMENTS_PER_EGG = 5

FOOD_TARGET, HYG_TARGET, EN_TARGET = 85, 35, 35
FOOD_BUY, HYG_BUY, EN_BUY = 80, 30, 30
# Rush mode targets: lower food target to save LUMIS for XP/level-up
RUSH_FOOD_BUY = 50
FOOD_ITEMS = [('magic_apple', 250), ('fairy_berries', 750), ('dragon_steak', 2000)]
HYG_ITEMS = [('magic_towel', 200), ('fairy_bath', 600), ('royal_spa', 1800)]
EN_ITEMS = [('wizard_coffee', 300), ('spark_juice', 900), ('thunder_tea', 2300)]
INV_ITEMS = {
    'food': ['magic_apple','fairy_berries','dragon_steak','royal_feast','hunger_potion'],
    'hygiene': ['magic_towel','fairy_bath','royal_spa','crystal_shower'],
    'energy': ['wizard_coffee','spark_juice','thunder_tea','phoenix_elixir'],
}
CHAT_MSGS = [
    'halo! lagi ngapain?', 'semangat mining ya hari ini!',
    'kamu monster paling rajin deh', 'udah makan belum?',
    'ayo kumpulin lumis yang banyak!', 'kangen nih, apa kabar?',
    'hari ini cerah ya', 'jangan lupa istirahat kalau capek',
    'kita bakal kaya bareng nih', 'gimana kabarmu hari ini?',
    'rajin banget kamu mining', 'nanti aku bawain makanan enak ya',
]


def level_up_cost(level, base_rate):
    if level >= 25:
        return float('inf')
    return math.floor(3 * base_rate * (level ** 1.5))


def mystery_egg_price(purchases):
    """Server formula: min(3M, floor(50k * 1.5^purchases)) — calculateMysteryEggPrice."""
    try:
        n = int(purchases or 0)
    except (TypeError, ValueError):
        n = 0
    return min(EGG_PRICE_CAP, math.floor(EGG_BASE_COST * (1.5 ** n)))


def lifespan_days(m):
    """source_egg takes precedence over rarity (getMonsterLifespanMs)."""
    src = m.get('source_egg')
    if src in LIFESPAN_DAYS_BY_EGG:
        return LIFESPAN_DAYS_BY_EGG[src]
    return LIFESPAN_DAYS_BY_RARITY.get(m.get('rarity'), 45)


def lifespan_hours_left(m):
    """Hours until this miner expires (production drops to 0). None if unknown."""
    hatched = m.get('hatched_at')
    if not hatched:
        return None
    try:
        h = datetime.fromisoformat(str(hatched).replace('Z', '+00:00'))
    except Exception:
        return None
    end = h + timedelta(days=lifespan_days(m))
    return (end - datetime.now(h.tzinfo)).total_seconds() / 3600.0


def is_expired(m):
    left = lifespan_hours_left(m)
    return left is not None and left <= 0


def levelup_payback_hours(m):
    """Hours for a level-up to repay itself from the extra production it buys."""
    lvl = m.get('level', 1)
    if lvl >= 25:
        return float('inf')
    base = m.get('base_production_rate', 0) or 0
    dmult = LEVEL_MULT.get(lvl + 1, 0) - LEVEL_MULT.get(lvl, 0)
    pm = PERSONALITY_PROD.get((m.get('personality') or '').lower(), 1.0)
    gain = base * dmult * 1.25 * pm      # assumes vitals kept in max zone
    if gain <= 0:
        return float('inf')
    return level_up_cost(lvl, base) / gain


# ─── Cryptomon pump prediction (exact port from JS) ─────────────────
PUMP_CYCLE_MS = 1_260_000   # 21 min  (PERSONALITY_MULTIPLIERS.cryptomon.cycleMs)
PUMP_WINDOW_MS = 60_000     # 1 min window
PUMP_MIN, PUMP_MAX = 5, 15  # production multiplier range
# Average multiplier over a full cycle: window/cycle of the time at ~10x, rest 1x
PUMP_AVG = 1 + (PUMP_WINDOW_MS / PUMP_CYCLE_MS) * ((PUMP_MIN + PUMP_MAX) / 2 - 1)


def _cryptomon_hash(e):
    t = 43758.5453 * math.sin(127.1 * e + 311.7)
    return t - math.floor(t)


def _pump_seed(mid):
    try:
        return int(str(mid)[-6:], 16)
    except (ValueError, TypeError):
        return 0


def pump_mult(mid, cycle=None):
    """getCryptomonPumpMultiplier: min + hash(seed + 13*cycle) * (max-min)."""
    i = _pump_seed(mid)
    if cycle is None:
        cycle = (int(time.time() * 1000) + i) // PUMP_CYCLE_MS
    return PUMP_MIN + _cryptomon_hash(i + 13 * cycle) * (PUMP_MAX - PUMP_MIN)


def pump_status(mid):
    """Return (is_pumping, next_pump_in_sec, mult) — exact port of isCryptomonPumping.

    JS: u = floor((now+i)/cycle); c = floor(hash(i+7u)*(cycle-window));
        f = (now+i) % cycle; pumping = c <= f < c+window
    """
    i = _pump_seed(mid)
    now = int(time.time() * 1000)
    u = (now + i) // PUMP_CYCLE_MS
    f = (now + i) % PUMP_CYCLE_MS
    c = math.floor(_cryptomon_hash(i + 7 * u) * (PUMP_CYCLE_MS - PUMP_WINDOW_MS))
    if c <= f < c + PUMP_WINDOW_MS:
        return True, 0, pump_mult(mid, u)
    if f < c:                                    # pump still ahead in this cycle
        return False, (c - f) / 1000.0, pump_mult(mid, u)
    nu = u + 1                                   # already passed -> next cycle
    nc = math.floor(_cryptomon_hash(i + 7 * nu) * (PUMP_CYCLE_MS - PUMP_WINDOW_MS))
    wait_ms = (PUMP_CYCLE_MS - f) + nc
    return False, wait_ms / 1000.0, pump_mult(mid, nu)


def personality_prod_mult(m, total_monsters=1, use_pump_avg=False):
    """Production multiplier from personality (getPersonalityMultiplier)."""
    p = (m.get('personality') or '').lower()
    if p == 'cryptomon':
        if use_pump_avg:
            return PUMP_AVG
        pumping, _, mult = pump_status(m.get('_id', ''))
        return mult if pumping else 1.0
    if p == 'royal':
        return 1 + 0.08 * max(0, total_monsters - 1)
    if p == 'influencer':
        return 1.0        # bonusPerReferral handled server-side
    return PERSONALITY_PROD.get(p, 1.0)


def prod_per_hour(m, total_monsters=1, use_pump_avg=False, boost_mult=1.0):
    """Estimated LUMIS/hour for a monster at current vitals."""
    if m.get('is_sleeping') or is_expired(m):
        return 0.0
    base = m.get('base_production_rate', 0)
    lm = LEVEL_MULT.get(m.get('level', 1), 1)
    vit = m.get('vitals', {})
    f, h, e = vit.get('food', 0), vit.get('hygiene', 0), vit.get('energy', 0)
    fm = 1.25 if f >= 80 else 1.0 if f >= 50 else 0.5 if f >= 20 else 0.01
    hm = 1.0 if h >= 30 else 0.5
    em = 1.0 if e >= 30 else 0.6 if e >= 1 else 0.01
    pm = personality_prod_mult(m, total_monsters, use_pump_avg)
    return base * lm * fm * hm * em * pm * boost_mult



# ─── Per-account state ───────────────────────────────────────────────
@dataclass
class AccState:
    account: Account
    api: MonsterAPI = None
    user: dict = None
    status: str = 'pending'
    last_bootstrap: float = 0
    last_cycle: float = 0
    cycles: int = 0
    error: str = ''
    chat_done: dict = field(default_factory=dict)   # monster_id -> msgs today
    streak_day: str = ''
    lumis_start: int = None
    started: float = field(default_factory=time.time)
    egg_price: int = EGG_BASE_COST      # live mystery-egg price for this account
    saving_for_egg: bool = False        # True => level-ups paused, saving for egg
    ascended: int = 0                   # miners turned into mentors this session

    @property
    def lumis(self):
        return (self.user or {}).get('profile', {}).get('lumis', 0)

    @property
    def monsters(self):
        return (self.user or {}).get('monsters', [])

    @property
    def eggs(self):
        return (self.user or {}).get('eggs', [])

    @property
    def miners(self):
        """Live, non-egg, non-mentor monsters — the ones that actually produce."""
        return [m for m in self.monsters
                if not m.get('is_egg') and not m.get('is_mentor')
                and m.get('vitals') and m.get('name')]

    @property
    def mentors(self):
        return [m for m in self.monsters if m.get('is_mentor')]

    @property
    def boost_mult(self):
        """profile_boost: +2%/day up to +10% permanent mining."""
        pb = (self.user or {}).get('profile', {}).get('profile_boost') or {}
        return 1 + (pb.get('percent') or 0) / 100.0

    def prod_total(self):
        n = len(self.miners)
        return sum(prod_per_hour(m, n, boost_mult=self.boost_mult) for m in self.miners)

    def prod_total_avg(self):
        """Same but using the cryptomon cycle average instead of the instant value."""
        n = len(self.miners)
        return sum(prod_per_hour(m, n, use_pump_avg=True, boost_mult=self.boost_mult)
                   for m in self.miners)


# ─── Farm engine ─────────────────────────────────────────────────────
class FarmEngine:
    def __init__(self, accounts, log, interval=240, do_chat=True):
        self.states = [AccState(account=a, api=MonsterAPI(a, log=lambda m: None))
                       for a in accounts]
        self.log = log
        self.interval = interval
        self.do_chat = do_chat
        self.lock = threading.Lock()
        self.stop = threading.Event()

    # ── farming primitives ──
    def _buy_cheapest(self, api, mid, items, vital, acc):
        for item_id, price in items:
            r = api.vitals_purchase(mid, item_id)
            if r.status_code == 200:
                d = r.json()
                new_l = d.get('newLumis', d.get('lumis'))
                self.log(acc, f'BOUGHT {item_id} (-{price}) for {vital} | lumis={new_l if new_l is not None else "?"}')
                return True, new_l
            body = r.json() if 'json' in r.headers.get('content-type', '') else {}
            err = str(body.get('error', '')).lower()
            if 'not enough' in err or 'insufficient' in err:
                continue
            self.log(acc, f'buy {item_id} failed: {r.status_code} {str(body)[:100]}')
            return False, None
        return False, None

    def _use_inv(self, api, mid, inv, vital, acc):
        for item_id in INV_ITEMS.get(vital, []):
            if inv.get(item_id, 0) > 0:
                r = api.vitals_use(mid, item_id)
                if r.status_code == 200:
                    self.log(acc, f'USED inv {item_id} for {vital}')
                    return True
        return False

    def maintain_monster(self, api, m, inv, lumis, acc, rush=False, st=None):
        # ghost entry (egg stub / incubating placeholder): no vitals, no name —
        # server rejects purchases for it with 403 'not owned by user'. Skip.
        if not m.get('vitals') or not m.get('name'):
            return lumis
        # mentors don't mine and don't consume vitals (they eat from the pantry)
        if m.get('is_mentor'):
            return lumis
        mid = m['_id']
        # LIFESPAN GUARD: an expired miner produces 0 forever. Feeding it burns
        # ~700-1000 LUMIS/h for nothing. Ascend it into a Mentor instead.
        if is_expired(m):
            if st is not None and not m.get('_ascend_tried'):
                m['_ascend_tried'] = True
                r = api.ascend(mid)
                if r.status_code == 200:
                    st.ascended += 1
                    self.log(acc, f'⬆ ASCENDED {m.get("name", mid)} L{m.get("level")} '
                                  f'-> MENTOR (lifespan over)')
                else:
                    body = r.json() if 'json' in r.headers.get('content-type', '') else {}
                    self.log(acc, f'ascend {r.status_code}: {str(body)[:90]}')
            else:
                self.log(acc, f'EXPIRED {m.get("name", mid)} — skipping upkeep (prod=0)')
            return lumis
        vit = m.get('vitals', {})
        reserve = RUSH_RESERVE if rush else LUMIS_RESERVE
        food_thresh = RUSH_FOOD_BUY if rush else FOOD_BUY
        # EMERGENCY: if any vital is critical (<20), ignore reserve — a dying
        # monster produces ~0, so survival spending always beats saving
        critical = min(vit.get('food', 0), vit.get('hygiene', 0), vit.get('energy', 0)) < 20
        if critical:
            reserve = 0
        # cryptomon pump awareness: boost vitals to max zone before pump window
        is_pump_mon = (m.get('personality') or '').lower() == 'cryptomon'
        if is_pump_mon:
            pumping, next_in, mult = pump_status(mid)
            if pumping:
                self.log(acc, f'🔥 PUMP {m.get("name",mid)} ~{mult:.0f}x ACTIVE')
            elif next_in is not None and next_in < 300:
                food_thresh = FOOD_BUY  # force max zone before pump
        if m.get('is_sleeping'):
            r = api.sleep(mid, 'wake_up')
            if r.status_code == 200:
                self.log(acc, f'woke {m.get("name", mid)} up')
        if vit.get('food', 0) < food_thresh:
            if not self._use_inv(api, mid, inv, 'food', acc) and lumis > reserve:
                ok, new_l = self._buy_cheapest(api, mid, FOOD_ITEMS, 'food', acc)
                if new_l is not None: lumis = new_l
        if vit.get('hygiene', 0) < HYG_BUY:
            if not self._use_inv(api, mid, inv, 'hygiene', acc) and lumis > reserve:
                ok, new_l = self._buy_cheapest(api, mid, HYG_ITEMS, 'hygiene', acc)
                if new_l is not None: lumis = new_l
        if vit.get('energy', 0) < EN_BUY:
            if not self._use_inv(api, mid, inv, 'energy', acc) and lumis > reserve:
                ok, new_l = self._buy_cheapest(api, mid, EN_ITEMS, 'energy', acc)
                if new_l is not None: lumis = new_l
        # ── level up ──────────────────────────────────────────────────
        # Gated by three rules, in order:
        #  1. survival first — never level while a vital is critical
        #  2. egg priority — if a slot is free and we're within EGG_HOLD_RATIO of
        #     the egg price, hold the LUMIS (egg EV ~1.76M >> one level)
        #  3. payback — never buy a level the pet won't live to repay
        lvl = m.get('level', 1)
        base_rate = m.get('base_production_rate', 1549)
        cost = level_up_cost(lvl, base_rate)
        if critical or lumis < cost + reserve:
            return lumis
        if st is not None and st.saving_for_egg:
            return lumis
        payback = levelup_payback_hours(m)
        left = lifespan_hours_left(m)
        if left is not None and payback * LEVELUP_PAYBACK_MARGIN > left:
            if not m.get('_payback_logged'):
                m['_payback_logged'] = True
                self.log(acc, f'skip L{lvl}->L{lvl+1} {m.get("name",mid)}: payback '
                              f'{payback/24:.1f}d > {left/24:.1f}d left')
            return lumis
        r = api.level_up(mid)
        if r.status_code == 200:
            dd = r.json()
            self.log(acc, f'LEVEL UP {m.get("name",mid)} -> L{dd.get("newLevel")} '
                          f'| -{cost:,} (payback {payback/24:.1f}d)')
            lumis = dd.get('newLumis', dd.get('lumis', lumis))
        else:
            body = r.json() if 'json' in r.headers.get('content-type', '') else {}
            if 'xp' not in str(body.get('error', '')).lower():
                self.log(acc, f'level_up {r.status_code}: {str(body)[:100]}')
        return lumis

    def egg_pipeline(self, api, st, lumis):
        d = st.user
        profile = d.get('profile', {})
        slots = profile.get('monster_slots', 1)
        inv = d.get('inventory', {})
        # Live price for THIS account: 50k * 1.5^(purchases so far), capped 3M.
        st.egg_price = mystery_egg_price(profile.get('mystery_egg_lumis_purchases'))
        # Slot accounting: expired miners still occupy a slot until ascended;
        # mentors move to the Sanctuary and free theirs.
        occupying = [m for m in st.monsters if not m.get('is_mentor')]
        owned = len(occupying) + len(st.eggs)
        free_slot = owned < slots
        # hatch finished eggs — NB: incubating eggs live in the monsters
        # array as is_egg stubs (user.eggs is null), so scan both
        candidates = list(st.eggs) + [m for m in st.monsters if m.get('is_egg')]
        for egg in candidates:
            inc = egg.get('incubation', {}) or {}
            ready = egg.get('ready_to_hatch') or inc.get('completed') or inc.get('ready')
            if not ready and inc.get('ends_at'):
                try:
                    ends = datetime.fromisoformat(str(inc['ends_at']).replace('Z', '+00:00'))
                    ready = datetime.now(ends.tzinfo) >= ends
                except Exception:
                    pass
            if ready:
                r = api.hatch_egg(egg['_id'])
                if r.status_code == 200:
                    self.log(st.account, f'HATCHED egg!')
                else:
                    body = r.json() if 'json' in r.headers.get('content-type', '') else {}
                    self.log(st.account, f'hatch {r.status_code}: {str(body)[:100]}')
        # FREE eggs first: 5 streak fragments -> 1 egg. Celestial EV ~$11.28,
        # golden ~$4.76 — always claim before spending LUMIS on a mystery egg.
        for egg_type in EGG_FRAGMENT_TYPES:
            if inv.get(f'{egg_type}_fragment', 0) >= FRAGMENTS_PER_EGG:
                r = api.claim_egg_from_fragments(egg_type)
                if r.status_code == 200:
                    self.log(st.account, f'🥚 CLAIMED FREE {egg_type} from 5 fragments')
                    inv[egg_type] = inv.get(egg_type, 0) + 1
                else:
                    body = r.json() if 'json' in r.headers.get('content-type', '') else {}
                    self.log(st.account, f'fragments {r.status_code}: {str(body)[:90]}')
        if not free_slot:
            st.saving_for_egg = False   # nothing to save for; let levels proceed
            return lumis
        # incubate owned eggs (best rarity odds first)
        for egg_type in ['celestial_egg', 'golden_egg', 'mystery_egg']:
            if inv.get(egg_type, 0) > 0:
                r = api.incubate_egg(egg_type)
                if r.status_code == 200:
                    self.log(st.account, f'INCUBATING {egg_type}')
                    st.saving_for_egg = False
                    return lumis
        # buy mystery egg at the real escalating price
        price = st.egg_price
        if lumis >= price + LUMIS_RESERVE:
            r = api.buy_egg('mystery_egg')
            if r.status_code == 200:
                self.log(st.account, f'BOUGHT mystery_egg -{price:,}')
                lumis -= price
                st.saving_for_egg = False
            else:
                body = r.json() if 'json' in r.headers.get('content-type', '') else {}
                err = str(body.get('error', ''))
                if not any(k in err.lower() for k in ('slot', 'ads', 'mentor')):
                    self.log(st.account, f'buy_egg {r.status_code}: {err[:100]}')
        else:
            # Not yet affordable. Once we're close, stop burning LUMIS on levels.
            was = st.saving_for_egg
            st.saving_for_egg = lumis >= price * EGG_HOLD_RATIO
            if st.saving_for_egg and not was:
                self.log(st.account, f'💰 SAVING for mystery_egg: {lumis:,}/{price:,} '
                                     f'— level-ups paused')
        return lumis


    def chat_xp(self, api, st):
        if not self.do_chat:
            return
        for m in st.miners:
            if is_expired(m):
                continue
            mid = m['_id']
            today_msgs = (m.get('xp_tracking', {}) or {}).get('chat_messages_today', 0)
            if max(st.chat_done.get(mid, 0), today_msgs) >= CHAT_DAILY_CAP:
                st.chat_done[mid] = CHAT_DAILY_CAP
                continue
            if m.get('vitals', {}).get('energy', 0) < 35:
                continue
            done = api.chat(mid, random.choice(CHAT_MSGS))
            if done and done.get('xp', {}).get('granted'):
                xp = done['xp']
                st.chat_done[mid] = xp.get('messages_today', st.chat_done.get(mid, 0) + 1)
                self.log(st.account, f'CHAT {m.get("name","?")} +{xp.get("amount")} XP '
                                     f'({xp.get("messages_today")}/{xp.get("max_messages")})')
            elif done and not done.get('xp', {}).get('granted'):
                st.chat_done[mid] = CHAT_DAILY_CAP

    # NOTE: tiny_drop is NOT a free XP action.
    # The constant XP_SPECIAL_ACTIONS.TINY_DROP = 50 made it look like chat XP,
    # but in the app it is invoked as showAd({action:'tiny_drop'}) — i.e. the
    # /api/ads/create-task -> watch -> /api/ads/complete pipeline. There is no
    # /api/xp route that accepts action='tiny_drop' (it answers 400 "Missing
    # required fields"). Automating it would mean faking ad views, so it is
    # deliberately NOT implemented. Chat XP below is genuinely free.

    # ── per-account cycle ──
    def run_account_cycle(self, st):
        api = st.api
        acc = st.account
        now = time.time()
        # bootstrap / re-auth
        if not api.initialized or now - st.last_bootstrap > REBOOTSTRAP_SEC:
            st.status = 'auth'
            api.bootstrap()
            st.last_bootstrap = now
        st.status = 'farming'
        # fresh state
        st.user = api.get_user()
        if st.lumis_start is None:
            st.lumis_start = st.lumis
        profile = st.user.get('profile', {})
        inv = st.user.get('inventory', {})
        lumis = profile.get('lumis', 0)
        # streak claim once/day
        today = datetime.now().strftime('%Y-%m-%d')
        dss = profile.get('daily_streak_state', {})
        if st.streak_day != today and not dss.get('streak_reward_claimed_today'):
            r = api.claim_streak()
            if r.status_code == 200:
                st.streak_day = today
                st.chat_done = {}
                self.log(acc, f'STREAK claimed: {str(r.json())[:120]}')
        # egg pipeline (sets st.egg_price / st.saving_for_egg for the level gate)
        lumis = self.egg_pipeline(api, st, lumis)
        # maintain all monsters (smart rush: auto-enable if max level < threshold)
        max_lvl = max((mm.get('level', 1) for mm in st.miners), default=1)
        is_rush = max_lvl < RUSH_LEVEL_THRESHOLD
        for m in st.monsters:
            lumis = self.maintain_monster(api, m, inv, lumis, acc, rush=is_rush, st=st)
        # free XP: chat only (12 XP x10/day). tiny_drop is ad-gated — see note above.
        self.chat_xp(api, st)
        # refresh final state
        st.user = api.get_user()
        st.cycles += 1
        st.last_cycle = time.time()
        st.error = ''

    def worker(self, once=False):
        while not self.stop.is_set():
            for st in self.states:
                if self.stop.is_set():
                    break
                try:
                    self.run_account_cycle(st)
                except Exception as e:
                    st.status = 'error'
                    st.error = str(e)[:200]
                    self.log(st.account, f'ERROR: {st.error}')
                    # force re-bootstrap next cycle on auth errors
                    if 'auth' in st.error.lower() or 'turnstile' in st.error.lower() \
                            or 'init' in st.error.lower():
                        st.api.initialized = False
            if once:
                break
            self.stop.wait(self.interval)


# ─── Dashboard ───────────────────────────────────────────────────────
console = Console()
ACTIVITY = deque(maxlen=60)
ACT_LOCK = threading.Lock()


def activity_log(acc, msg):
    ts = datetime.now().strftime('%H:%M:%S')
    with ACT_LOCK:
        ACTIVITY.append((ts, acc.name, msg))
    print(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] [{acc.name}] {msg}', flush=True)


def vital_bar(val, width=12):
    val = max(0, min(100, val))
    filled = int(val / 100 * width)
    color = 'green' if val >= 60 else 'yellow' if val >= 30 else 'red'
    bar = Text()
    bar.append('█' * filled, style=color)
    bar.append('░' * (width - filled), style='grey30')
    return bar


def vital_cell(val, target, width=6):
    """Compact vital: mini bar + number, colored by distance from target zone."""
    val = max(0.0, min(999.0, float(val or 0)))
    color = 'green' if val >= target else 'yellow' if val >= target * 0.6 else 'red'
    bars = max(0, width - 4)
    filled = int(min(100.0, val) / 100 * bars)
    t = Text()
    t.append('█' * filled, style=color)
    t.append('░' * (bars - filled), style='grey30')
    t.append(f'{val:>4.0f}', style=color)
    return t


def fmt_lumis(v):
    """Short LUMIS: 51,585 -> 51.6k, 2,507,273 -> 2.51M."""
    v = v or 0
    if abs(v) >= 1_000_000:
        return f'{v/1_000_000:.2f}M'
    if abs(v) >= 10_000:
        return f'{v/1000:.1f}k'
    return f'{v:,}'


def fmt_left(hours):
    if hours is None:
        return '?'
    if hours <= 0:
        return 'DEAD'
    if hours < 48:
        return f'{hours:.0f}h'
    return f'{hours/24:.0f}d'


def accounts_table(engine, tier) -> Table:
    """One row per account — never truncates, scales to any account count."""
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False, expand=True,
              header_style='bold grey62', row_styles=['', 'on grey11'])
    narrow = (tier == 'narrow')
    wide = (tier == 'wide')
    t.add_column('Acct', style='bold cyan', width=8 if narrow else 9, no_wrap=True)
    t.add_column('St', width=3 if narrow else 4, no_wrap=True)
    t.add_column('LUMIS', justify='right', width=8, no_wrap=True)
    if not narrow:
        t.add_column('USD', justify='right', width=7, no_wrap=True)
        t.add_column('Rank', width=9, no_wrap=True)
        t.add_column('Sk', justify='right', width=3, no_wrap=True)
    t.add_column('Slot', justify='center', width=4, no_wrap=True)
    t.add_column('Prod/h', justify='right', width=7, no_wrap=True)
    t.add_column('Sess', justify='right', width=8, no_wrap=True)
    if wide:
        t.add_column('Next egg', justify='right', width=15, no_wrap=True)
        t.add_column('Cyc', justify='right', width=4, no_wrap=True)
        t.add_column('Auth', justify='right', width=5, no_wrap=True)

    for st in engine.states:
        prof = (st.user or {}).get('profile', {})
        lumis = prof.get('lumis', 0)
        slots = prof.get('monster_slots', 1)
        occupying = [m for m in st.monsters if not m.get('is_mentor')]
        owned = len(occupying) + len(st.eggs)
        stat = {'farming': ('OK', 'bold green'), 'auth': ('AUTH', 'yellow'),
                'error': ('ERR', 'bold red'), 'pending': ('...', 'grey50')}
        stxt, sstyle = stat.get(st.status, (st.status[:4], 'white'))
        if narrow:
            stxt = stxt[:3]
        delta = (lumis - st.lumis_start) if st.lumis_start is not None else 0
        row = [
            st.account.name,
            Text(stxt, style=sstyle),
            Text(fmt_lumis(lumis), style='bold gold1'),
        ]
        if not narrow:
            rank = str(prof.get('rank', '?'))
            if st.boost_mult > 1.0:
                rank += f' +{(st.boost_mult-1)*100:.0f}%'
            row += [f'${lumis/LUMIS_PER_USD:.4f}', rank,
                    str((prof.get('daily_streak_state') or {}).get('days', 0))]
        row += [
            Text(f'{owned}/{slots}', style='green' if owned < slots else 'grey58'),
            Text(f'{st.prod_total():,.0f}', style='green'),
            Text(f'{delta:+,}', style='green' if delta >= 0 else 'red'),
        ]
        if wide:
            if owned < slots:
                pct = min(100, lumis / max(1, st.egg_price) * 100)
                egg = Text(f'{fmt_lumis(lumis)}/{fmt_lumis(st.egg_price)} {pct:.0f}%',
                           style='bold yellow' if st.saving_for_egg else 'grey58')
            else:
                egg = Text('slots full', style='grey39')
            left = max(0, REBOOTSTRAP_SEC - (time.time() - st.last_bootstrap)) \
                if st.last_bootstrap else 0
            row += [egg, str(st.cycles), f'{left/60:.0f}m']
        t.add_row(*row)
    return t


def monsters_table(engine, tier) -> Table:
    """One row per monster across all accounts."""
    narrow = (tier == 'narrow')
    wide = (tier == 'wide')
    t = Table(box=box.SIMPLE_HEAD, pad_edge=False, expand=True,
              header_style='bold grey62')
    t.add_column('Acct', style='cyan', width=8 if narrow else 9, no_wrap=True)
    t.add_column('Monster', style='bold', width=9 if narrow else 10, no_wrap=True)
    t.add_column('Lv', justify='right', width=2 if narrow else 3, no_wrap=True)
    # header must fit the column or Rich ellipsizes the header itself
    t.add_column('Personality' if wide else 'Pers',
                 width=8 if narrow else (10 if tier == 'compact' else 12), no_wrap=True)
    vw = 4 if narrow else (6 if tier == 'compact' else 13)
    t.add_column('Fd' if narrow else 'Food', justify='right' if narrow else 'left',
                 width=vw, no_wrap=True)
    t.add_column('Hy' if narrow else 'Hyg', justify='right' if narrow else 'left',
                 width=vw, no_wrap=True)
    t.add_column('En' if narrow else 'Ener', justify='right' if narrow else 'left',
                 width=vw, no_wrap=True)
    t.add_column('Prod/h', justify='right', width=6 if narrow else 7, no_wrap=True)
    if not narrow:
        t.add_column('XP', justify='right', width=10, no_wrap=True)
    t.add_column('Life', justify='right', width=4 if narrow else 5, no_wrap=True)
    if wide:
        t.add_column('Payback', justify='right', width=8, no_wrap=True)

    def cell(v, tgt):
        if narrow:
            v = float(v or 0)
            color = 'green' if v >= tgt else 'yellow' if v >= tgt * 0.6 else 'red'
            return Text(f'{v:.0f}', style=color)
        if tier == 'compact':
            return vital_cell(v, tgt, 6)
        return _wide_vital(v, tgt)

    def novitals():
        """Vital columns for rows that have no vitals (eggs / mentors)."""
        dash = Text('-', style='grey39')
        return [dash, dash.copy(), dash.copy()]

    any_row = False
    for st in engine.states:
        n_miners = len(st.miners)
        for m in st.monsters:
            any_row = True
            vit = m.get('vitals') or {}
            lvl = m.get('level', 1)
            name = m.get('name') or '(egg)'
            pers = (m.get('personality') or '-')
            left_h = lifespan_hours_left(m) if m.get('hatched_at') else None

            if m.get('is_egg'):
                inc = m.get('incubation') or {}
                eta = '?'
                if inc.get('ends_at'):
                    try:
                        dt = datetime.fromisoformat(str(inc['ends_at']).replace('Z', '+00:00'))
                        hh = (dt - datetime.now(dt.tzinfo)).total_seconds() / 3600
                        eta = 'READY' if hh <= 0 else f'{hh:.0f}h'
                    except Exception:
                        pass
                etype = str(inc.get('egg_type', 'egg')).replace('_egg', '')
                note = eta if narrow else f'hatch {eta}'
                row = [st.account.name,
                       Text(f'🥚{etype}'[:9 if narrow else 10], style='yellow'),
                       Text('-', style='grey39'),
                       Text(note, style='yellow')]
                row += novitals()
                row += [Text('-', style='grey39')]
                if not narrow:
                    row.append(Text('-', style='grey39'))
                row.append(Text('-', style='grey39'))
                if wide:
                    row.append(Text('-', style='grey39'))
                t.add_row(*row)
                continue

            if m.get('is_mentor'):
                row = [st.account.name,
                       Text(f'🎓{name}'[:9 if narrow else 10], style='magenta'),
                       f'{lvl}',
                       Text('mentor', style='magenta')]
                row += novitals()
                row += [Text('-', style='grey39')]
                if not narrow:
                    row.append(Text('-', style='grey39'))
                row.append(Text('∞', style='magenta'))
                if wide:
                    row.append(Text('-', style='grey39'))
                t.add_row(*row)
                continue

            expired = is_expired(m)
            if m.get('is_sleeping') and not narrow:
                name += ' 💤'
            if pers.lower() == 'cryptomon':
                pumping, next_in, mult = pump_status(m['_id'])
                if pumping:
                    pers = f'🔥{mult:.0f}x'
                elif next_in is not None:
                    pers = f'{next_in/60:.0f}m' if narrow else f'pump {next_in/60:.0f}m'
            row = [
                st.account.name,
                Text(name[:9 if narrow else 10], style='red' if expired else 'bold'),
                Text(f'{lvl}', style='red' if expired else 'white'),
                pers,
                cell(vit.get('food', 0), FOOD_BUY),
                cell(vit.get('hygiene', 0), HYG_BUY),
                cell(vit.get('energy', 0), EN_BUY),
                Text(f'{prod_per_hour(m, n_miners, boost_mult=st.boost_mult):,.0f}',
                     style='red' if expired else 'green'),
            ]
            if not narrow:
                row.append(f'{m.get("experience",0)}/{math.floor(500 * 1.25 ** (lvl - 1))}')
            row.append(Text(fmt_left(left_h), style='bold red' if expired
                            else 'yellow' if (left_h or 999) < 72 else 'grey58'))
            if wide:
                pb = levelup_payback_hours(m)
                row.append('∞' if pb == float('inf') else f'{pb/24:.1f}d')
            t.add_row(*row)
    if not any_row:
        n_cols = len(t.columns)
        t.add_row('—', 'waiting for cycle 1', *[''] * (n_cols - 2))
    return t


def _wide_vital(val, target):
    b = vital_bar(val or 0, 8)
    color = 'green' if (val or 0) >= target else 'yellow' if (val or 0) >= target * 0.6 else 'red'
    b.append(f'{val or 0:>4.0f}', style=color)
    return b


# Panel chrome: 2 border lines + SIMPLE_HEAD header + separator + 1 blank
TABLE_PANEL_OVERHEAD = 5
# Minimum console width each tier needs to render without Rich shrinking cells.
# Measured from the real tables (sum of column widths + per-column padding +
# panel border/padding); verified in test_logic.py which fails on any '…'.
TIER_MIN_WIDTH = {'compact': 105, 'wide': 139}


def render_dashboard(engine: FarmEngine) -> Layout:
    width = console.size.width or 80
    height = console.size.height or 40
    tier = ('wide' if width >= TIER_MIN_WIDTH['wide']
            else 'compact' if width >= TIER_MIN_WIDTH['compact']
            else 'narrow')
    n_mon = sum(len(st.monsters) for st in engine.states) or 1
    n_acc = len(engine.states)

    acc_h = n_acc + TABLE_PANEL_OVERHEAD
    mon_h = n_mon + TABLE_PANEL_OVERHEAD
    act_h = max(5, min(14, height - 3 - acc_h - mon_h))

    layout = Layout()
    layout.split_column(
        Layout(name='header', size=3),
        Layout(name='accounts', size=acc_h),
        Layout(name='monsters', size=mon_h),
        Layout(name='activity', size=act_h),
    )

    total_lumis = sum(st.lumis for st in engine.states)
    total_prod = sum(st.prod_total() for st in engine.states)
    total_avg = sum(st.prod_total_avg() for st in engine.states)
    n_ok = sum(1 for st in engine.states if st.status == 'farming')
    exp = sum(1 for st in engine.states for m in st.miners if is_expired(m))
    h = Text(no_wrap=True, overflow='ellipsis')
    h.append(' MONSTERLAND ', style='bold white on purple4')
    h.append(f' {n_ok}/{n_acc}ok ', style='bold')
    h.append(f'{fmt_lumis(total_lumis)}L', style='bold gold1')
    h.append(f' ${total_lumis/LUMIS_PER_USD:.3f}', style='grey58')
    h.append(f'  {total_prod:,.0f}/h', style='bold green')
    if tier != 'narrow':
        h.append(f' (avg {total_avg:,.0f})', style='grey58')
    h.append(f'  {total_avg*24/1000:.0f}k/d', style='green')
    if exp:
        h.append(f'  ⚠{exp} EXPIRED', style='bold red')
    h.append(f'  {datetime.now():%H:%M:%S}', style='grey50')
    layout['header'].update(Panel(h, border_style='purple4', padding=(0, 1)))

    layout['accounts'].update(Panel(accounts_table(engine, tier),
                                   title='[bold]Accounts', title_align='left',
                                   border_style='blue', padding=(0, 1)))
    layout['monsters'].update(Panel(monsters_table(engine, tier),
                                   title='[bold]Monsters', title_align='left',
                                   border_style='cyan', padding=(0, 1)))

    with ACT_LOCK:
        entries = list(ACTIVITY)[-(act_h - 2):]
    at = Table(box=None, pad_edge=False, expand=True, show_header=False)
    at.add_column('time', style='grey50', width=8, no_wrap=True)
    at.add_column('acc', style='cyan', width=8 if tier == 'narrow' else 9, no_wrap=True)
    at.add_column('msg', overflow='ellipsis', no_wrap=True)
    for ts, name, msg in reversed(entries):
        style = 'red' if ('ERROR' in msg or 'EXPIRED' in msg) else \
                'bold yellow' if ('LEVEL UP' in msg or 'ASCENDED' in msg
                                  or 'HATCHED' in msg or 'CLAIMED' in msg) else ''
        at.add_row(ts, name, Text(msg, style=style))
    layout['activity'].update(Panel(at, title='[bold]Activity', title_align='left',
                                   border_style='grey39', padding=(0, 1)))
    return layout



# ─── Main ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--accounts', help='comma-separated account names (default: all enabled)')
    ap.add_argument('--interval', type=int, default=240, help='cycle sleep seconds (default 240)')
    ap.add_argument('--no-chat', action='store_true', help='disable chat XP')
    ap.add_argument('--width', type=int, default=0,
                    help='force render width (default: auto-detect terminal)')
    ap.add_argument('--once', action='store_true', help='run one cycle then exit')
    args = ap.parse_args()

    global console
    if args.width:
        console = Console(width=args.width)

    accounts = load_accounts()
    if not accounts:
        print('No accounts found. Run: python add_account.py <name> <phone>')
        sys.exit(1)
    if args.accounts:
        wanted = {x.strip() for x in args.accounts.split(',')}
        accounts = [a for a in accounts if a.name in wanted]
    accounts = [a for a in accounts if a.enabled]
    if not accounts:
        print('No matching/enabled accounts.')
        sys.exit(1)

    print(f'Starting Monsterland farm: {", ".join(a.name for a in accounts)} '
          f'(interval {args.interval}s, chat={"off" if args.no_chat else "on"}, '
          f'render width {console.size.width})')

    engine = FarmEngine(accounts, log=activity_log, interval=args.interval,
                        do_chat=not args.no_chat)
    worker = threading.Thread(target=engine.worker, kwargs={'once': args.once}, daemon=True)
    worker.start()

    if args.once:
        worker.join()
        for st in engine.states:
            print(f'[{st.account.name}] status={st.status} lumis={st.lumis} '
                  f'monsters={len(st.monsters)} err={st.error}')
        return

    try:
        with Live(render_dashboard(engine), console=console,
                  refresh_per_second=1, screen=False) as live:
            while not engine.stop.is_set():
                time.sleep(2)
                live.update(render_dashboard(engine))
    except KeyboardInterrupt:
        engine.stop.set()
        print('\nStopped.')


if __name__ == '__main__':
    main()
