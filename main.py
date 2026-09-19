import asyncio
import base64
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from curl_cffi import requests
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles are often cp1251

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
ACCOUNTS_FILE = BASE_DIR / "accounts.txt"
PROXY_FILE = BASE_DIR / "Proxy.txt"
PROXY_FAIL_LIMIT = 3  # consecutive network failures before a proxy is replaced

LOCALE = os.getenv("LOCALE", "en-US")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Kyiv")

POLL_MIN, POLL_MAX = 15, 30
CHANNEL_GAP_MIN, CHANNEL_GAP_MAX = 2, 5      # pause between channels inside one cycle
ACCOUNT_START_MIN, ACCOUNT_START_MAX = 5, 20  # stagger between accounts at startup
REACT_DELAY_MIN, REACT_DELAY_MAX = 3, 12
HISTORY_LIMIT = 50

API = "https://discord.com/api/v9"
GATEWAY = "wss://gateway.discord.gg/?v=9&encoding=json"

# TLS/HTTP2 fingerprint of this Chrome build is impersonated by curl_cffi;
# UA, sec-ch-ua and super-properties below must describe the same version.
CHROME = "142"
IMPERSONATE = f"chrome{CHROME}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME}.0.0.0 Safari/537.36"
)
FALLBACK_BUILD = 615980

_print_lock = threading.Lock()
_state_lock = threading.Lock()
_proxy_file_lock = threading.Lock()  # guards accounts.txt + Proxy.txt rewrites


def log(label, msg):
    with _print_lock:
        print(f"[{datetime.now():%H:%M:%S}] [{label}] {msg}", flush=True)


class AuthError(Exception):
    pass


# ── Identity of the web client ────────────────────────────────────────────────
def fetch_build_number():
    """client_build_number of the live web client, scraped from its JS bundles."""
    try:
        s = requests.Session(impersonate=IMPERSONATE)
        html = s.get("https://discord.com/login", timeout=20).text
        scripts = re.findall(r"/assets/[\w.\-]+\.js", html)
        scripts.sort(key=lambda p: not p.startswith("/assets/web."))  # entry bundle first
        for path in scripts[:40]:
            m = re.search(r"buildNumber\D{1,6}(\d{5,7})", s.get(f"https://discord.com{path}", timeout=30).text)
            if m:
                return int(m.group(1))
    except Exception as e:
        print(f"build number fetch failed: {e}")
    print(f"using fallback build number {FALLBACK_BUILD}")
    return FALLBACK_BUILD


def make_properties(build):
    return {
        "os": "Windows",
        "browser": "Chrome",
        "device": "",
        "system_locale": LOCALE,
        "has_client_mods": False,
        "browser_user_agent": USER_AGENT,
        "browser_version": f"{CHROME}.0.0.0",
        "os_version": "10",
        "referrer": "",
        "referring_domain": "",
        "referrer_current": "",
        "referring_domain_current": "",
        "release_channel": "stable",
        "client_build_number": build,
        "client_event_source": None,
    }


PROPERTIES = make_properties(fetch_build_number())
SUPER_PROPERTIES = base64.b64encode(json.dumps(PROPERTIES, separators=(",", ":")).encode()).decode()


def make_headers(token):
    return {
        "accept":             "*/*",
        "accept-language":    f"{LOCALE},{LOCALE.split('-')[0]};q=0.9",
        "authorization":      token,
        "content-type":       "application/json",
        "origin":             "https://discord.com",
        "priority":           "u=1, i",
        "referer":            "https://discord.com/channels/@me",
        "sec-ch-ua":          f'"Chromium";v="{CHROME}", "Google Chrome";v="{CHROME}", "Not_A Brand";v="99"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest":     "empty",
        "sec-fetch-mode":     "cors",
        "sec-fetch-site":     "same-origin",
        "user-agent":         USER_AGENT,
        "x-debug-options":    "bugReporterEnabled",
        "x-discord-locale":   LOCALE,
        "x-discord-timezone": TIMEZONE,
        "x-super-properties": SUPER_PROPERTIES,
    }


# ── Config ────────────────────────────────────────────────────────────────────
def fmt_proxy(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    return raw if "://" in raw else f"http://{raw}"


def mask_proxy(raw):
    return raw.rpartition("@")[2] if raw else "none"  # never log credentials


def atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def take_spare_proxy():
    """Removes and returns the first proxy of Proxy.txt (None if the pool is empty)."""
    if not PROXY_FILE.exists():
        return None
    lines = [l.strip() for l in PROXY_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not lines:
        return None
    atomic_write(PROXY_FILE, "".join(l + "\n" for l in lines[1:]))
    return lines[0]


def replace_account_proxy(token, new_proxy):
    """Rewrites the account's line in accounts.txt, keeping comments and order."""
    out = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and s.partition(",")[0].strip() == token:
            line = f"{token},{new_proxy}"
        out.append(line)
    atomic_write(ACCOUNTS_FILE, "\n".join(out) + "\n")


def norm_emoji(name):
    return name.replace("️", "")  # ⚔ and ⚔️ (with variation selector) are the same reaction


def reaction_key(emoji):
    """Comparable key of a reaction's emoji object: custom ones are `name:id`, unicode ones the character."""
    if emoji.get("id"):
        return f"{emoji.get('name')}:{emoji['id']}"
    return norm_emoji(emoji.get("name") or "")


def norm_text(text):
    """Lowercase, no markdown marks, single spaces: so '**Starting in**' or a NBSP still match."""
    return " ".join(re.sub(r"[*_~`|]", "", text).lower().split())


def as_list(value):
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


class Channel:
    def __init__(self, ref, keywords, emoji, react_on_first_run):
        ref = str(ref)
        m = re.search(r"channels/(\d+|@me)/(\d+)", ref)
        if m:
            self.guild_id, self.id = m.group(1), m.group(2)
        elif ref.isdigit():
            self.guild_id, self.id = None, ref
        else:
            sys.exit(f"config.json: cannot read channel from {ref!r}")
        self.keywords = keywords
        self.emojis = [emoji]
        self.react_on_first_run = react_on_first_run

    @property
    def headers(self):
        """Per-request headers: a real client's referer is the channel it is looking at."""
        if not self.guild_id:
            return {}
        return {"referer": f"https://discord.com/channels/{self.guild_id}/{self.id}"}

    def __str__(self):
        return f"{self.id} [{', '.join(self.keywords)}]"


def load_config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        sys.exit(f"cannot read config.json: {e}")
    keywords = [norm_text(k) for k in as_list(cfg.get("keywords")) if k.strip()]
    if not keywords:
        sys.exit("config.json: no keywords")
    emoji = cfg.get("emoji") or "👍"
    if not isinstance(emoji, str):
        sys.exit('config.json: "emoji" must be a single string')
    first_run = cfg.get("react_on_first_run", True)
    channels = [Channel(c, keywords, emoji, first_run) for c in cfg.get("channels", [])]
    if not channels:
        sys.exit("config.json: no channels")
    return channels


def load_accounts():
    """accounts.txt: one `token` or `token,proxy` per line. Falls back to DISCORD_TOKEN."""
    raw = []
    if ACCOUNTS_FILE.exists():
        for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                token, _, proxy = line.partition(",")
                raw.append((token.strip(), proxy.strip() or None))
    elif os.getenv("DISCORD_TOKEN", "").strip():
        raw.append((os.getenv("DISCORD_TOKEN").strip(), None))
    if not raw:
        sys.exit("no accounts: fill accounts.txt (or DISCORD_TOKEN in .env)")
    return [Account(t, p, i + 1) for i, (t, p) in enumerate(raw)]


# ── State: one <channel_id>.json per channel, last message id per account ────
def state_file(channel_id):
    return BASE_DIR / f"{channel_id}.json"


def load_state(channel_id, user_id, legacy_ok=False):
    try:
        data = json.loads(state_file(channel_id).read_text())
    except (OSError, ValueError):
        return None
    try:
        if user_id in data:
            return int(data[user_id])
        if legacy_ok and "last_reacted_id" in data:  # single-account format
            return int(data["last_reacted_id"])
    except (ValueError, TypeError):
        pass
    return None


def save_state(channel_id, user_id, message_id):
    with _state_lock:
        try:
            data = json.loads(state_file(channel_id).read_text())
        except (OSError, ValueError):
            data = {}
        data[user_id] = message_id
        state_file(channel_id).write_text(json.dumps(data, indent=2))


def migrate_legacy_state(channels):
    """state.json from the first single-channel version belongs to the first configured channel id."""
    legacy = BASE_DIR / "state.json"
    if not legacy.exists():
        return
    for ch in channels:
        if not state_file(ch.id).exists():
            legacy.rename(state_file(ch.id))
            print(f"migrated state.json -> {state_file(ch.id).name}")
            return


# ── Gateway ───────────────────────────────────────────────────────────────────
class Gateway:
    """Keeps the account online like a real web client (IDENTIFY, heartbeats, resume)."""

    def __init__(self, account):
        self.account = account
        self.ready = threading.Event()
        self.fatal = None
        self.seq = None
        self.session_id = None
        self.resume_url = None
        self.acked = True
        self._loop = None
        self._ws = None
        self._resetting = False

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    def reset(self):
        """Drop the connection and the session (used after a proxy swap: new IP = fresh IDENTIFY)."""
        self.session_id = self.seq = None
        if self._loop and self._ws:
            self._resetting = True
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    async def _run(self):
        self._loop = asyncio.get_running_loop()
        failures = 0
        while True:
            try:
                await self._connect()
                failures = 0
                self.account.proxy_ok("gateway")
            except Exception as e:
                if self._resetting:  # we closed it ourselves after a proxy swap
                    failures = 0
                else:
                    failures += 1
                    log(self.account.label, f"gateway error: {e!r}")
                    self.account.proxy_failed("gateway")
            self._resetting = False
            if self.fatal:
                return
            self.ready.clear()
            # back off on repeated failures so we never hammer IDENTIFY
            await asyncio.sleep(random.uniform(2, 6) * min(2 ** failures, 30))

    async def _send(self, ws, op, d):
        await ws.send_str(json.dumps({"op": op, "d": d}))

    async def _identify(self, ws):
        await self._send(ws, 2, {
            "token": self.account.token,
            "capabilities": 30717,
            "properties": PROPERTIES,
            "presence": {"status": "online", "since": 0, "activities": [], "afk": False},
            "compress": False,
            "client_state": {"guild_versions": {}},
        })

    async def _heartbeat(self, ws, interval):
        await asyncio.sleep(interval * random.random())  # jittered first beat, per protocol
        while True:
            if not self.acked:
                log(self.account.label, "gateway: heartbeat not acked, reconnecting")
                await ws.close()
                return
            self.acked = False
            await self._send(ws, 1, self.seq)
            await asyncio.sleep(interval)

    async def _connect(self):
        url = f"{self.resume_url}/?v=9&encoding=json" if self.session_id and self.resume_url else GATEWAY
        async with requests.AsyncSession(impersonate=IMPERSONATE) as s:
            ws = await s.ws_connect(
                url,
                headers={"Origin": "https://discord.com", "User-Agent": USER_AGENT,
                         "Accept-Language": f"{LOCALE},{LOCALE.split('-')[0]};q=0.9"},
                proxy=self.account.proxy,
                timeout=20,
                max_message_size=64 * 1024 * 1024,  # user-account READY is >4MB uncompressed
            )
            hb = None
            self._ws = ws
            try:
                hello = json.loads((await ws.recv())[0])
                interval = hello["d"]["heartbeat_interval"] / 1000
                self.acked = True
                hb = asyncio.create_task(self._heartbeat(ws, interval))
                if self.session_id:
                    await self._send(ws, 6, {"token": self.account.token, "session_id": self.session_id, "seq": self.seq})
                else:
                    await self._identify(ws)

                while True:
                    data, _ = await ws.recv()
                    msg = json.loads(data)
                    op = msg["op"]
                    if msg.get("s") is not None:
                        self.seq = msg["s"]
                    if op == 0:
                        if msg["t"] == "READY":
                            d = msg["d"]
                            self.session_id = d["session_id"]
                            self.resume_url = d.get("resume_gateway_url", "").rstrip("/") or None
                            self.ready.set()
                            self.account.proxy_ok("gateway")
                            log(self.account.label, f"gateway: online as {d['user']['username']}")
                        elif msg["t"] == "RESUMED":
                            self.ready.set()
                    elif op == 1:
                        await self._send(ws, 1, self.seq)
                    elif op == 11:
                        self.acked = True
                    elif op == 7:
                        return
                    elif op == 9:
                        if not msg["d"]:
                            self.session_id = self.seq = None
                        await asyncio.sleep(random.uniform(1, 5))
                        return
            except Exception:
                if getattr(ws, "close_code", None) in (4004, 4010, 4011, 4012, 4013, 4014):
                    self.fatal = f"gateway closed with code {ws.close_code}"
                    return
                raise
            finally:
                if hb:
                    hb.cancel()
                await ws.close()


# ── Account ───────────────────────────────────────────────────────────────────
class Account:
    def __init__(self, token, raw_proxy, index):
        self.token = token
        self.raw_proxy = raw_proxy
        self.proxy = fmt_proxy(raw_proxy)
        self.index = index
        self.label = f"#{index}"
        self.user_id = None
        self.session = self._make_session()
        self.gateway = Gateway(self)
        self._channel_errors = {}
        self._fails = {"rest": 0, "gateway": 0}
        self._swap_lock = threading.Lock()

    def _make_session(self):
        s = requests.Session(impersonate=IMPERSONATE, proxy=self.proxy)
        s.headers.update(make_headers(self.token))
        return s

    # ── Proxy ───────────────────────────────────────────────────────────────
    def proxy_ok(self, source):
        self._fails[source] = 0

    def proxy_failed(self, source):
        """Counts consecutive network failures; swaps the proxy once the limit is hit.
        Accounts without a proxy run directly and never get one assigned."""
        if not self.proxy:
            return
        self._fails[source] += 1
        if self._fails[source] >= PROXY_FAIL_LIMIT:
            self._fails = {"rest": 0, "gateway": 0}
            self.swap_proxy()

    def swap_proxy(self):
        with self._swap_lock, _proxy_file_lock:
            old = self.raw_proxy
            new = take_spare_proxy()
            if not new:
                log(self.label, f"proxy {mask_proxy(old)} keeps failing, Proxy.txt has no spare ones")
                return
            replace_account_proxy(self.token, new)
            self.raw_proxy, self.proxy = new, fmt_proxy(new)
            self.session = self._make_session()
            log(self.label, f"proxy {mask_proxy(old)} failed -> replaced with {mask_proxy(new)}")
        self.gateway.reset()

    # ── REST ────────────────────────────────────────────────────────────────
    def request(self, method, path, **kwargs):
        while True:
            try:
                r = self.session.request(method, f"{API}{path}", timeout=20, **kwargs)
            except requests.exceptions.RequestException:
                self.proxy_failed("rest")
                raise
            self.proxy_ok("rest")
            if r.status_code == 429:
                try:
                    wait = r.json().get("retry_after", 5) + 0.5
                except ValueError:
                    wait = 5.5
                log(self.label, f"rate limited, sleeping {wait:.1f}s")
                time.sleep(wait)
                continue
            if r.status_code == 401:
                raise AuthError("401: token is invalid or expired")
            return r

    def fetch_recent(self, channel):
        r = self.request("GET", f"/channels/{channel.id}/messages", params={"limit": HISTORY_LIMIT},
                         headers=channel.headers)
        if r.status_code != 200:
            # log each distinct failure once, not every poll
            if self._channel_errors.get(channel.id) != r.status_code:
                self._channel_errors[channel.id] = r.status_code
                log(self.label, f"channel {channel.id}: fetch failed {r.status_code} {r.text[:120]}")
            return []
        self._channel_errors.pop(channel.id, None)
        return sorted(r.json(), key=lambda m: int(m["id"]), reverse=True)  # newest first

    def react(self, channel, msg_id, emoji):
        emoji = urllib.parse.quote(emoji, safe=":")  # custom emoji is `name:id`
        r = self.request(
            "PUT", f"/channels/{channel.id}/messages/{msg_id}/reactions/{emoji}/@me",
            params={"location": "Message", "type": 0}, headers=channel.headers,
        )
        log(self.label, f"channel {channel.id}: react {emoji} on {msg_id}: {r.status_code}")
        return r.status_code

    # ── Matching ────────────────────────────────────────────────────────────
    @staticmethod
    def text_of(msg):
        parts = [msg.get("content", "")]
        for e in msg.get("embeds", []):
            parts += [e.get("title", ""), e.get("description", "")]
            parts += [(e.get("footer") or {}).get("text", ""), (e.get("author") or {}).get("name", "")]
            for f in e.get("fields", []):
                parts += [f.get("name", ""), f.get("value", "")]
        return norm_text(" ".join(p for p in parts if p))

    def matches(self, channel, msg):
        text = self.text_of(msg)
        return any(k in text for k in channel.keywords)

    # ── Per-channel step ────────────────────────────────────────────────────
    def init_channel(self, channel):
        last = load_state(channel.id, self.user_id, legacy_ok=self.index == 1)
        if last is None:
            # first run: 0 means "the newest keyword message already in the channel is still to be handled"
            # (reacted only if we have not reacted yet); otherwise start from the current newest message
            recent = [] if channel.react_on_first_run else self.fetch_recent(channel)
            last = int(recent[0]["id"]) if recent else 0
            save_state(channel.id, self.user_id, last)
        return last

    def check_channel(self, channel, last_reacted):
        # only the newest message with a keyword, and only once
        target = next((m for m in self.fetch_recent(channel) if self.matches(channel, m)), None)
        if not target or int(target["id"]) <= last_reacted:
            return last_reacted
        mine = {reaction_key(rx.get("emoji") or {}) for rx in target.get("reactions", []) if rx.get("me")}
        todo = [e for e in channel.emojis if norm_emoji(e) not in mine]
        for i, emoji in enumerate(todo):
            time.sleep(random.uniform(REACT_DELAY_MIN, REACT_DELAY_MAX) if i == 0 else random.uniform(1, 3))
            status = self.react(channel, target["id"], emoji)
            # 403/404 never get better by retrying: skip it; other errors retry next poll
            if status not in (200, 204, 403, 404):
                return last_reacted
        last_reacted = int(target["id"])
        save_state(channel.id, self.user_id, last_reacted)
        return last_reacted

    def startup_request(self, method, path):
        for attempt in range(8):  # network errors here feed the proxy failure counter too
            try:
                return self.request(method, path)
            except requests.exceptions.RequestException as e:
                log(self.label, f"network error: {e}")
                time.sleep(random.uniform(3, 8))
        raise AuthError("network unreachable at startup")

    # ── Main loop ───────────────────────────────────────────────────────────
    def run(self, channels):
        try:
            self.gateway.start()
            waited = 0
            while not self.gateway.ready.wait(5):  # gateway retries (and swaps a dead proxy) by itself
                waited += 5
                if self.gateway.fatal:
                    raise AuthError(self.gateway.fatal)
                if waited >= 300:
                    raise AuthError("gateway did not become ready in 5 minutes")
            time.sleep(random.uniform(2, 5))  # a real client loads the UI before touching anything

            me = self.startup_request("GET", "/users/@me")
            if me.status_code != 200:
                raise AuthError(f"auth failed: {me.status_code}")
            self.user_id = me.json()["id"]
            self.label = f"#{self.index} {me.json().get('username')}"
            log(self.label, "logged in, watching: " + "; ".join(str(c) for c in channels))

            last = {}
            for ch in channels:
                try:
                    last[ch.id] = self.init_channel(ch)
                except Exception as e:
                    log(self.label, f"channel {ch.id}: init error: {e!r}")
                    last[ch.id] = None
                time.sleep(random.uniform(CHANNEL_GAP_MIN, CHANNEL_GAP_MAX))

            while True:
                if self.gateway.fatal:
                    raise AuthError(self.gateway.fatal)
                for ch in random.sample(channels, len(channels)):
                    try:
                        if last[ch.id] is None:
                            last[ch.id] = self.init_channel(ch)
                        else:
                            last[ch.id] = self.check_channel(ch, last[ch.id])
                    except AuthError:
                        raise
                    except Exception as e:
                        log(self.label, f"channel {ch.id}: error: {e!r}")
                    time.sleep(random.uniform(CHANNEL_GAP_MIN, CHANNEL_GAP_MAX))
                time.sleep(random.uniform(POLL_MIN, POLL_MAX))
        except AuthError as e:
            log(self.label, f"stopped: {e}")
        except Exception as e:
            log(self.label, f"stopped, unexpected error: {e!r}")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    channels = load_config()
    accounts = load_accounts()
    migrate_legacy_state(channels)
    print(f"{len(accounts)} account(s), {len(channels)} channel(s)")

    threads = []
    for acct in accounts:
        t = threading.Thread(target=acct.run, args=(channels,), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(random.uniform(ACCOUNT_START_MIN, ACCOUNT_START_MAX))
    for t in threads:
        t.join()
    print("all accounts stopped")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
