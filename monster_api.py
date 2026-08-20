#!/usr/bin/env python3
"""monster_api.py — Monsterland API client (multi-account).

Auth: header 'Authorization: tma ***'  (NOT Bearer!)
Initialize: first call needs x-app-signal:initialize + cf-turnstile-response.
Turnstile solved via local captcha solver (http://localhost:8001).

Multi-account: accounts registered in accounts.json, each with its own
Telethon session file. fetch_initdata(account) gets fresh initData via
RequestWebView. initData TTL is short (~5-10 min) so bootstrap() must be
re-run periodically (daemon does every 30 min).
"""
import json, re, time, requests
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import unquote

HOME = Path.home()
BOT_DIR = Path(__file__).parent
B = 'https://lets.playmonsterland.com'
SITEKEY = '0x4AAAAAADdQlvzwXRHPB_GW'
SOLVER = 'http://localhost:8001'
ACCOUNTS_FILE = BOT_DIR / 'accounts.json'
UA = ('Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Version/4.0 Chrome/126.0.0.0 Mobile Safari/537.36')


# ─── Account registry ────────────────────────────────────────────────
@dataclass
class Account:
    name: str
    session: str          # absolute path to .session file (Telethon)
    phone: str = ''
    api_id: int = 0
    api_hash: str = ''
    enabled: bool = True

    def session_path(self) -> Path:
        p = Path(self.session).expanduser()
        # Telethon appends .session if missing
        return p.with_suffix('') if p.suffix == '.session' else p


def load_creds():
    creds = {}
    for line in (HOME / '.tg_cred.env').read_text().splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.strip().split('=', 1)
            creds[k.strip()] = v.strip()
    return creds


def load_accounts():
    """Load accounts.json; auto-migrates legacy single-account setup."""
    if ACCOUNTS_FILE.exists():
        data = json.loads(ACCOUNTS_FILE.read_text())
        return [Account(**a) for a in data.get('accounts', [])]
    # legacy migration: main account from ~/.tg_cred.env + ~/.tma_session.session
    try:
        c = load_creds()
        acc = Account(
            name='main',
            session=str(HOME / '.tma_session.session'),
            phone=c.get('TG_PHONE', ''),
            api_id=int(c['TG_API_ID']),
            api_hash=c['TG_API_HASH'],
        )
        save_accounts([acc])
        return [acc]
    except Exception:
        return []


def save_accounts(accounts):
    ACCOUNTS_FILE.write_text(json.dumps(
        {'accounts': [asdict(a) for a in accounts]}, indent=2))
    ACCOUNTS_FILE.chmod(0o600)


def register_account(name, session_path, phone, api_id, api_hash):
    accs = load_accounts()
    accs = [a for a in accs if a.name != name]
    accs.append(Account(name=name, session=str(session_path), phone=phone,
                        api_id=int(api_id), api_hash=api_hash))
    save_accounts(accs)
    return accs[-1]


# ─── initData fetch (Telethon WebView) ───────────────────────────────
def fetch_initdata(account: Account):
    """Fresh initData via Telethon RequestWebView for a given account."""
    import asyncio
    from telethon import TelegramClient, functions

    async def _get():
        cl = TelegramClient(str(account.session_path()),
                            account.api_id, account.api_hash)
        await cl.connect()
        try:
            bot = await cl.get_entity('monsterland_bot')
            result = await cl(functions.messages.RequestWebViewRequest(
                peer=bot, bot=bot, platform='android', from_bot_menu=False,
                url='https://lets.playmonsterland.com/'))
            m = re.search(r'tgWebAppData=([^&]+)', result.url)
            return unquote(m.group(1)) if m else None
        finally:
            await cl.disconnect()
    return asyncio.run(_get())


def solve_turnstile(retries=3):
    """Solve Turnstile via local solver. Returns token or None."""
    for attempt in range(retries):
        try:
            task = requests.get(f'{SOLVER}/turnstile', params={
                'url': B + '/', 'sitekey': SITEKEY}, timeout=15).json()
            tid = task.get('task_id')
            if not tid:
                continue
            for _ in range(60):
                r = requests.get(f'{SOLVER}/result', params={'id': tid}, timeout=15).json()
                if r.get('status') == 'success':
                    return r['value']
                if r.get('status') == 'failure':
                    break
                time.sleep(2)
        except Exception:
            pass
        time.sleep(2)
    return None


# ─── API client ──────────────────────────────────────────────────────
class MonsterAPI:
    def __init__(self, account: Account = None, log=print):
        self.log = log
        self.account = account
        self.init = None
        self.token = None
        self.initialized = False
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Origin': B, 'Referer': B + '/', 'User-Agent': UA})

    def _headers(self, extra=None):
        h = {'Authorization': 'tma ' + self.init}
        if extra:
            h.update(extra)
        return h

    def bootstrap(self):
        """Fetch initData + solve turnstile + initialize session."""
        if self.account is None:
            raise RuntimeError('no account configured')
        self.log(f'[{self.account.name}] fetching initData...')
        self.init = fetch_initdata(self.account)
        if not self.init:
            raise RuntimeError('failed to fetch initData')
        self.log(f'[{self.account.name}] initData ok ({len(self.init)} chars)')
        self.log(f'[{self.account.name}] solving turnstile...')
        self.token = solve_turnstile()
        if not self.token:
            raise RuntimeError('failed to solve turnstile')
        self.log(f'[{self.account.name}] turnstile ok ({len(self.token)} chars)')
        r = self.session.get(B + '/api/user?include=monsters', headers=self._headers({
            'x-app-signal': 'initialize', 'cf-turnstile-response': self.token}), timeout=25)
        if r.status_code != 200:
            raise RuntimeError(f'initialize failed: {r.status_code} {r.text[:150]}')
        self.initialized = True
        self.log(f'[{self.account.name}] session initialized')
        return r.json()

    def get_user(self):
        r = self.session.get(B + '/api/user?include=monsters',
                             headers=self._headers(), timeout=25)
        if r.status_code == 403 and 'TURNSTILE_REQUIRED' in r.text:
            self.token = solve_turnstile()
            r = self.session.get(B + '/api/user?include=monsters', headers=self._headers({
                'x-app-signal': 'initialize', 'cf-turnstile-response': self.token}), timeout=25)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = self.session.post(B + path, headers=self._headers(), json=body, timeout=25)
        return r

    def vitals_purchase(self, monster_id, item_id):
        return self._post('/api/vitals', {'monsterId': monster_id, 'itemId': item_id, 'action': 'purchase'})

    def vitals_use(self, monster_id, item_id):
        return self._post('/api/vitals', {'monsterId': monster_id, 'itemId': item_id, 'action': 'use_inventory'})

    def sleep(self, monster_id, action):
        return self._post('/api/sleep', {'monsterId': monster_id, 'action': action})

    def level_up(self, monster_id):
        return self._post('/api/xp', {'action': 'level_up', 'monsterId': monster_id})

    def claim_streak(self):
        return self._post('/api/daily-streak', {'action': 'claim'})

    # --- egg pipeline (monster #2+) ---
    def buy_egg(self, egg_type='mystery_egg'):
        """Buy egg with LUMIS via store. body: {type,itemId,paymentMethod,quantity}"""
        return self._post('/api/store', {
            'type': 'egg', 'itemId': egg_type,
            'paymentMethod': 'lumis', 'quantity': 1})

    def incubate_egg(self, egg_type='mystery_egg'):
        """Start incubation of an owned egg. body: {egg_type}"""
        return self._post('/api/eggs/incubate', {'egg_type': egg_type})

    def hatch_egg(self, egg_id):
        """Hatch a finished egg. body: {egg_id}"""
        return self._post('/api/eggs/hatch', {'egg_id': egg_id})

    def chat(self, monster_id, message):
        """Send chat to pet. Streams SSE; returns parsed 'done' event dict or None."""
        import json as _json
        try:
            r = self.session.post(B + '/api/chat',
                headers=self._headers({'Content-Type': 'application/json'}),
                json={'monster_id': monster_id, 'message': message},
                stream=True, timeout=90)
            if r.status_code != 200:
                return None
            done = None
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith('data: '):
                    try:
                        ev = _json.loads(line[6:])
                    except Exception:
                        continue
                    if ev.get('type') == 'done':
                        done = ev
                        break
            r.close()
            return done
        except Exception as e:
            self.log(f'chat error: {e}')
            return None
