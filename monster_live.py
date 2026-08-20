#!/usr/bin/env python3
"""monster_live.py — Monsterland multi-account farm bot with Rich TUI dashboard.

Run:  cd /home/ubuntu/bot/monsterland && /home/ubuntu/bot/urkocoin/.venv/bin/python monster_live.py
Stop: Ctrl+C

Features:
  - Multi-account (accounts.json, each with own Telethon session)
  - Auto vitals maintenance (max multiplier zone: food>=80, hyg>=30, energy>=30)
  - Auto level-up (dynamic cost, reserve-guarded)
  - Auto streak claim, egg pipeline (buy/incubate/hatch), chat XP (10/day)
  - Full dashboard: lumis, rank, streak, monsters, vitals bars, production, eggs

Usage:
  python monster_live.py                    # all enabled accounts
  python monster_live.py --accounts main    # only 'main'
  python monster_live.py --interval 300     # 5-min cycles
  python monster_live.py --no-chat          # skip chat XP
  python monster_live.py --once             # single cycle, then exit
"""
import argparse, math, random, sys, threading, time, traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

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
PERSONALITY_PROD = {'zoomer': 1.5, 'overlord': 3.0}  # alwaysBonus / selfMultiplier
LUMIS_PER_USD = 1_000_000
EGG_COST = 50_000
LUMIS_RESERVE = 1_000
RUSH_RESERVE = 500       # lower reserve for rush mode (accounts below RUSH_LEVEL_THRESHOLD)
RUSH_LEVEL_THRESHOLD = 3 # accounts with max monster level < this enter rush mode automatically
REBOOTSTRAP_SEC = 1800   # initData TTL is short; re-auth every 30 min

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


def prod_per_hour(m):
    """Estimated LUMIS/hour for a monster at current vitals."""
    base = m.get('base_production_rate', 0)
    lm = LEVEL_MULT.get(m.get('level', 1), 1)
    vit = m.get('vitals', {})
    f, h, e = vit.get('food', 0), vit.get('hygiene', 0), vit.get('energy', 0)
    fm = 1.25 if f >= 80 else 1.0 if f >= 50 else 0.5 if f >= 20 else 0.01
    hm = 1.0 if h >= 30 else 0.5
    em = 1.0 if e >= 30 else 0.6 if e >= 1 else 0.01
    pm = PERSONALITY_PROD.get((m.get('personality') or '').lower(), 1.0)
    if m.get('is_sleeping'):
        return 0.0
    return base * lm * fm * hm * em * pm


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

    @property
    def lumis(self):
        return (self.user or {}).get('profile', {}).get('lumis', 0)

    @property
    def monsters(self):
        return (self.user or {}).get('monsters', [])

    @property
    def eggs(self):
        return (self.user or {}).get('eggs', [])

    def prod_total(self):
        return sum(prod_per_hour(m) for m in self.monsters)


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
                self.log(acc, f'BOUGHT {item_id} (-{price}) for {vital} | lumis={d.get("lumis","?")}')
                return True, d.get('lumis')
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

    def maintain_monster(self, api, m, inv, lumis, acc, rush=False):
        mid = m['_id']
        vit = m.get('vitals', {})
        reserve = RUSH_RESERVE if rush else LUMIS_RESERVE
        food_thresh = RUSH_FOOD_BUY if rush else FOOD_BUY
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
        # level up
        lvl = m.get('level', 1)
        base_rate = m.get('base_production_rate', 1549)
        cost = level_up_cost(lvl, base_rate)
        if lumis >= cost + reserve:
            r = api.level_up(mid)
            if r.status_code == 200:
                dd = r.json()
                self.log(acc, f'LEVEL UP {m.get("name",mid)} -> L{dd.get("newLevel")} | -{cost:,}')
                lumis = dd.get('lumis', lumis)
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
        owned = len(st.monsters) + len(st.eggs)
        if owned >= slots:
            return lumis
        # hatch finished eggs
        for egg in st.eggs:
            inc = egg.get('incubation', {}) or {}
            if egg.get('ready_to_hatch') or inc.get('completed') or inc.get('ready'):
                r = api.hatch_egg(egg['_id'])
                if r.status_code == 200:
                    self.log(st.account, f'HATCHED egg!')
                else:
                    body = r.json() if 'json' in r.headers.get('content-type', '') else {}
                    self.log(st.account, f'hatch {r.status_code}: {str(body)[:100]}')
        # incubate owned eggs
        for egg_type in ['mystery_egg', 'golden_egg', 'celestial_egg']:
            if inv.get(egg_type, 0) > 0:
                r = api.incubate_egg(egg_type)
                if r.status_code == 200:
                    self.log(st.account, f'INCUBATING {egg_type}')
                    return lumis
        # buy mystery egg if affordable + slot free
        if lumis >= EGG_COST + LUMIS_RESERVE and owned < slots:
            r = api.buy_egg('mystery_egg')
            if r.status_code == 200:
                self.log(st.account, f'BOUGHT mystery_egg -{EGG_COST:,}')
                lumis -= EGG_COST
            else:
                body = r.json() if 'json' in r.headers.get('content-type', '') else {}
                err = str(body.get('error', ''))
                if not any(k in err.lower() for k in ('slot', 'ads', 'mentor')):
                    self.log(st.account, f'buy_egg {r.status_code}: {err[:100]}')
        return lumis

    def chat_xp(self, api, st):
        if not self.do_chat:
            return
        for m in st.monsters:
            mid = m['_id']
            today_msgs = (m.get('xp_tracking', {}) or {}).get('chat_messages_today', 0)
            if max(st.chat_done.get(mid, 0), today_msgs) >= 10:
                st.chat_done[mid] = 10
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
                st.chat_done[mid] = 10

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
        # egg pipeline
        lumis = self.egg_pipeline(api, st, lumis)
        # maintain all monsters (smart rush: auto-enable if max level < threshold)
        max_lvl = max((mm.get('level', 1) for mm in st.monsters), default=1)
        is_rush = max_lvl < RUSH_LEVEL_THRESHOLD
        for m in st.monsters:
            lumis = self.maintain_monster(api, m, inv, lumis, acc, rush=is_rush)
        # chat XP
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
ACTIVITY = deque(maxlen=30)
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


def render_account(st: AccState) -> Panel:
    acc = st.account
    u = st.user or {}
    prof = u.get('profile', {})

    # header line
    lumis = prof.get('lumis', 0)
    usd = lumis / LUMIS_PER_USD
    rank = prof.get('rank', '?')
    plvl = prof.get('level', '?')
    dss = prof.get('daily_streak_state', {})
    streak = dss.get('days', 0)
    slots = prof.get('monster_slots', 1)
    owned = len(st.monsters) + len(st.eggs)

    status_style = {'farming': 'bold green', 'auth': 'yellow',
                    'error': 'bold red', 'pending': 'grey50'}.get(st.status, 'white')
    head = Text()
    head.append(f' {acc.name} ', style='bold white on dark_blue')
    head.append(f'  {acc.phone or ""} ', style='grey58')
    head.append(f'[{st.status.upper()}]', style=status_style)
    if st.error:
        head.append(f'  {st.error[:60]}', style='red')

    body = Table.grid(padding=(0, 1))
    body.add_column(ratio=1)

    # summary row
    s = Text()
    s.append('LUMIS ', style='grey58')
    s.append(f'{lumis:,}', style='bold gold1')
    s.append(f' (${usd:.4f})', style='grey58')
    s.append(f'   Rank {rank} L{plvl}', style='cyan')
    s.append(f'   Streak {streak}d', style='magenta')
    s.append(f'   Slots {owned}/{slots}', style='blue')
    prod = st.prod_total()
    s.append(f'   ~{prod:,.0f}/h', style='green')
    if st.lumis_start is not None:
        delta = lumis - st.lumis_start
        s.append(f'   sess {delta:+,}', style='green' if delta >= 0 else 'red')
    body.add_row(s)

    # monsters table
    if st.monsters:
        mt = Table(box=None, pad_edge=False, expand=True)
        mt.add_column('Monster', style='bold', width=16)
        mt.add_column('Lvl', width=4)
        mt.add_column('Personality', width=11)
        mt.add_column('Food', width=17)
        mt.add_column('Hygiene', width=17)
        mt.add_column('Energy', width=17)
        mt.add_column('Prod/h', justify='right', width=9)
        mt.add_column('XP', justify='right', width=10)
        for m in st.monsters:
            vit = m.get('vitals', {})
            lvl = m.get('level', 1)
            xp = m.get('experience', 0)
            xp_req = math.floor(500 * 1.25 ** (lvl - 1))
            name = m.get('name', '?')
            if m.get('is_sleeping'):
                name += ' 💤'
            fb = vital_bar(vit.get('food', 0)); fb.append(f' {vit.get("food",0):.0f}', style='grey58')
            hb = vital_bar(vit.get('hygiene', 0)); hb.append(f' {vit.get("hygiene",0):.0f}', style='grey58')
            eb = vital_bar(vit.get('energy', 0)); eb.append(f' {vit.get("energy",0):.0f}', style='grey58')
            mt.add_row(
                name,
                f'L{lvl}',
                (m.get('personality') or '?')[:11],
                fb, hb, eb,
                f'{prod_per_hour(m):,.0f}',
                f'{xp}/{xp_req}',
            )
        body.add_row(mt)

    # eggs
    if st.eggs:
        e = Text()
        e.append('Eggs: ', style='bold')
        for egg in st.eggs:
            inc = egg.get('incubation', {}) or {}
            etype = egg.get('type', egg.get('egg_type', 'egg'))
            ends = inc.get('ends_at') or inc.get('completed_at')
            e.append(f'{etype} ', style='yellow')
            if egg.get('ready_to_hatch') or inc.get('completed'):
                e.append('(READY) ', style='bold green')
            elif ends:
                try:
                    dt = datetime.fromisoformat(str(ends).replace('Z', '+00:00'))
                    left = (dt - datetime.now(dt.tzinfo)).total_seconds() / 3600
                    e.append(f'({left:.1f}h left) ', style='grey58')
                except Exception:
                    pass
        body.add_row(e)

    # footer: cycle info
    f = Text()
    f.append(f'cycles {st.cycles}', style='grey50')
    if st.last_cycle:
        f.append(f'  last {datetime.fromtimestamp(st.last_cycle):%H:%M:%S}', style='grey50')
    if st.last_bootstrap:
        left = max(0, REBOOTSTRAP_SEC - (time.time() - st.last_bootstrap))
        f.append(f'  re-auth in {left/60:.0f}m', style='grey50')
    body.add_row(f)

    border = {'farming': 'green', 'auth': 'yellow', 'error': 'red'}.get(st.status, 'grey39')
    return Panel(body, title=head, border_style=border, padding=(0, 1))


def render_dashboard(engine: FarmEngine) -> Layout:
    layout = Layout()
    layout.split_column(Layout(name='header', size=3),
                        Layout(name='accounts'),
                        Layout(name='activity', size=12))

    # header
    total_lumis = sum(st.lumis for st in engine.states)
    total_prod = sum(st.prod_total() for st in engine.states)
    n_ok = sum(1 for st in engine.states if st.status == 'farming')
    h = Text()
    h.append(' MONSTERLAND FARM ', style='bold white on purple4')
    h.append(f'  {len(engine.states)} accounts ({n_ok} farming)  ', style='bold')
    h.append(f'TOTAL {total_lumis:,} LUMIS', style='bold gold1')
    h.append(f' (${total_lumis/LUMIS_PER_USD:.4f})', style='grey58')
    h.append(f'  ~{total_prod:,.0f}/h', style='bold green')
    h.append(f'  ~{total_prod*24:,.0f}/day', style='green')
    h.append(f'   {datetime.now():%a %d %b %H:%M:%S}', style='grey50')
    layout['header'].update(Panel(h, border_style='purple4'))

    # account panels
    if len(engine.states) == 1:
        layout['accounts'].update(render_account(engine.states[0]))
    else:
        panels = [render_account(st) for st in engine.states]
        rows = [panels[i:i+2] for i in range(0, len(panels), 2)]
        if len(rows) == 1:
            grid = Layout()
            grid.split_row(*[Layout(p) for p in rows[0]])
        else:
            row_layouts = []
            for row in rows:
                rl = Layout()
                rl.split_row(*[Layout(p) for p in row])
                row_layouts.append(rl)
            grid = Layout()
            grid.split_column(*row_layouts)
        layout['accounts'].update(grid)

    # activity
    with ACT_LOCK:
        entries = list(ACTIVITY)[-10:]
    at = Table(box=None, pad_edge=False, expand=True, show_header=False)
    at.add_column('time', style='grey50', width=8)
    at.add_column('acc', style='cyan', width=10)
    at.add_column('msg')
    for ts, name, msg in reversed(entries):
        at.add_row(ts, name, msg[:100])
    layout['activity'].update(Panel(at, title='Activity', border_style='blue', padding=(0, 1)))
    return layout


# ─── Main ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--accounts', help='comma-separated account names (default: all enabled)')
    ap.add_argument('--interval', type=int, default=240, help='cycle sleep seconds (default 240)')
    ap.add_argument('--no-chat', action='store_true', help='disable chat XP')
    ap.add_argument('--once', action='store_true', help='run one cycle then exit')
    args = ap.parse_args()

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
          f'(interval {args.interval}s, chat={"off" if args.no_chat else "on"})')

    engine = FarmEngine(accounts, log=activity_log,
                        interval=args.interval, do_chat=not args.no_chat)
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
