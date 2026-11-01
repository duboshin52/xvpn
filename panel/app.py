"""xvpn — главный сервер: ключи, подписки, синхронизация нод.

Интерфейсы: команда xvpn в SSH (всегда, через локальный API с X-CLI-Token),
Telegram-бот (если задан BOT_TOKEN) и мини-апп (если ещё и DOMAIN)."""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import ssl
import threading
import time
import uuid as uuidlib
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp,
                           Message, WebAppInfo)
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

log = logging.getLogger("xvpn")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID") or 0)
DOMAIN = os.environ.get("DOMAIN", "")
CLI_TOKEN = os.environ.get("CLI_TOKEN", "")
PANEL_PATH = "/" + os.environ["PANEL_PATH"].strip("/")
SUB_PATH = "/" + os.environ["SUB_PATH"].strip("/")
DB_PATH = os.environ.get("DB_PATH", "/opt/xvpn/panel/xvpn.db")
SUB_TITLE = os.environ.get("SUB_TITLE", "xvpn")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "60"))
WEBAPP_URL = f"https://{DOMAIN}{PANEL_PATH}/" if DOMAIN else ""
HERE = os.path.dirname(os.path.abspath(__file__))
GB = 1024 ** 3
DAY = 86400

# ---------------------------------------------------------------- DB
_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS admins(tg_id INTEGER PRIMARY KEY, name TEXT, is_owner INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS servers(id INTEGER PRIMARY KEY, name TEXT, url TEXT, secret TEXT,
  info TEXT, enabled INTEGER DEFAULT 1, status TEXT, last_seen INTEGER DEFAULT 0, online INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS keys(id INTEGER PRIMARY KEY, name TEXT, uuid TEXT, sub_token TEXT UNIQUE,
  limit_bytes INTEGER DEFAULT 0, used_bytes INTEGER DEFAULT 0, expire_at INTEGER DEFAULT 0,
  enabled INTEGER DEFAULT 1, created_at INTEGER, created_by INTEGER, last_active INTEGER DEFAULT 0,
  notified INTEGER DEFAULT 0);
""")
# миграции: каскад (role = exit|bridge, cert — закреплённый сертификат агента моста,
# bridge_uuid — служебный пользователь моста на выходах, cascade — нода работает выходом для мостов)
_cols = {r[1] for r in db.execute("PRAGMA table_info(servers)")}
for _c, _d in (("role", "TEXT DEFAULT 'exit'"), ("cert", "TEXT"), ("bridge_uuid", "TEXT"),
               ("cascade", "INTEGER DEFAULT 1")):
    if _c not in _cols:
        db.execute(f"ALTER TABLE servers ADD COLUMN {_c} {_d}")
if OWNER_ID:
    db.execute("INSERT OR IGNORE INTO admins(tg_id,name,is_owner) VALUES(?,?,1)", (OWNER_ID, "owner"))
db.commit()


def q(sql, args=(), one=False):
    with _lock:
        cur = db.execute(sql, args)
        rows = cur.fetchall()
        db.commit()
    rows = [dict(r) for r in rows]
    return (rows[0] if rows else None) if one else rows


def qx(sql, args=()):
    with _lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.lastrowid


def setting(key: str, default: str = "") -> str:
    r = q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default


def set_setting(key: str, value: str):
    qx("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def cdn_on() -> bool:
    """XHTTP через Cloudflare в подписках. В РФ Cloudflare часто замедляют — тогда выключают."""
    return setting("cdn", "1") == "1"


# ---------------------------------------------------------------- keys
def key_state(k: dict) -> str:
    now = time.time()
    if not k["enabled"]:
        return "disabled"
    if k["expire_at"] and k["expire_at"] < now:
        return "expired"
    if k["limit_bytes"] and k["used_bytes"] >= k["limit_bytes"]:
        return "limit"
    return "active"


def key_out(k: dict) -> dict:
    k = dict(k)
    k["state"] = key_state(k)
    k["sub_url"] = f"https://{DOMAIN}{SUB_PATH}/{k['sub_token']}" if DOMAIN else None
    k["online"] = k["last_active"] > time.time() - 180
    return k


def create_key(name: str, days: float, gb: float, by: int) -> dict:
    kid = qx("INSERT INTO keys(name,uuid,sub_token,limit_bytes,expire_at,created_at,created_by) "
             "VALUES(?,?,?,?,?,?,?)",
             (name, str(uuidlib.uuid4()), secrets.token_urlsafe(16), int(gb * GB),
              int(time.time() + days * DAY) if days else 0, int(time.time()), by))
    return q("SELECT * FROM keys WHERE id=?", (kid,), one=True)


def server_links(srv: dict, k: dict) -> list[str]:
    info = json.loads(srv["info"] or "{}")
    if not info:
        return []
    u, n, r = k["uuid"], srv["name"], info["reality"]
    tag = lambda t: quote(f"{n} {t} | {k['name']}")
    h = f"[{info['ip']}]" if ":" in info["ip"] else info["ip"]  # IPv6 в ссылке — в скобках
    if info.get("role") == "bridge":
        return [f"vless://{u}@{h}:{info.get('port', 443)}?encryption=none&security=reality"
                f"&sni={r['sni_tcp']}&fp=chrome&pbk={r['public_key']}&sid={r['short_id']}"
                f"&type=tcp&flow=xtls-rprx-vision#{tag('Каскад')}"]
    cdn = []
    rc = info.get("ru_cdn")
    if rc and rc.get("host"):
        # российский CDN: packet-up, отправка GET с данными в заголовках (POST там режут)
        # пакеты до 1500 байт: крупнее не влезают в заголовки и получают 431
        extra = quote(json.dumps({"uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header",
                                  "scMaxEachPostBytes": 1500, "scMinPostsIntervalMs": 10},
                                 separators=(",", ":")), safe="")
        cdn.append(f"vless://{u}@{rc['host']}:443?encryption=none&security=tls&sni={rc['host']}"
                   f"&alpn=h2%2Chttp%2F1.1&fp=chrome&type=xhttp&host={rc['host']}"
                   f"&path={quote(info['xhttp_path'], safe='')}&mode=packet-up&extra={extra}"
                   f"#{tag('РФ-CDN')}")
    if info.get("domain") and cdn_on():
        cdn += [f"vless://{u}@{info['domain']}:443?encryption=none&security=tls&sni={info['domain']}"
               f"&alpn=h2%2Chttp%2F1.1&fp=chrome&type=xhttp&host={info['domain']}"
               # packet-up: каждый кусок — отдельный запрос; через Cloudflare работает и без gRPC
               # (в stream-up Cloudflare без gRPC буферизует отправку — «нет обратного трафика»)
               f"&path={quote(info['xhttp_path'], safe='')}&mode=packet-up#{tag('XHTTP-CDN')}"]
    return cdn + [
        f"vless://{u}@{h}:443?encryption=none&security=reality&sni={r['sni_tcp']}"
        f"&fp=chrome&pbk={r['public_key']}&sid={r['short_id']}&type=tcp&flow=xtls-rprx-vision"
        f"#{tag('Reality')}",
        f"vless://{u}@{h}:443?encryption=none&security=reality&sni={r['sni_xhttp']}"
        f"&fp=chrome&pbk={r['public_key']}&sid={r['short_id']}&type=xhttp"
        f"&path={quote(r['xhttp_path'], safe='')}&mode=auto#{tag('XHTTP-Reality')}",
    ]


def key_links(k: dict) -> list[str]:
    out = []
    # мосты (каскад) первыми: они работают и при белых списках
    for s in q("SELECT * FROM servers WHERE enabled=1 ORDER BY role='bridge' DESC, id"):
        out += server_links(s, k)
    return out


# ---------------------------------------------------------------- nodes
_ctx_cache: dict[str, ssl.SSLContext] = {}


def pinned_ctx(pem: str) -> ssl.SSLContext:
    """TLS-контекст, доверяющий ровно одному самоподписанному сертификату агента моста."""
    if pem not in _ctx_cache:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=pem)
        _ctx_cache[pem] = ctx
    return _ctx_cache[pem]


def fetch_pinned_cert(url: str, fp: str) -> str:
    """Скачивает сертификат агента и сверяет SHA-256 отпечаток из кода подключения."""
    u = urlsplit(url)
    pem = ssl.get_server_certificate((u.hostname, u.port or 443), timeout=10)
    got = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    if not hmac.compare_digest(got, fp.lower().replace(":", "")):
        raise ValueError("отпечаток сертификата не совпадает — возможна подмена")
    return pem


def parse_code(code: str) -> dict:
    """Код подключения моста: base64(JSON {u: url, s: secret, f: sha256 отпечаток})."""
    raw = code.strip()
    raw += "=" * (-len(raw) % 4)
    d = json.loads(base64.urlsafe_b64decode(raw))
    return {"url": d["u"], "secret": d["s"], "fp": d.get("f", "")}


async def agent(srv: dict, method: str, path: str, body=None, timeout=15):
    verify = pinned_ctx(srv["cert"]) if srv.get("cert") else True
    async with httpx.AsyncClient(timeout=timeout, verify=verify) as c:
        r = await c.request(method, srv["url"].rstrip("/") + path, json=body,
                            headers={"Authorization": f"Bearer {srv['secret']}"})
        r.raise_for_status()
        return r.json()


def active_users() -> list[dict]:
    return [{"email": f"k{k['id']}", "uuid": k["uuid"]}
            for k in q("SELECT * FROM keys") if key_state(k) == "active"]


def bridge_users() -> list[dict]:
    """Служебные пользователи мостов: с ними мосты ходят на выходы."""
    return [{"email": f"b{s['id']}", "uuid": s["bridge_uuid"]}
            for s in q("SELECT * FROM servers WHERE role='bridge' AND bridge_uuid IS NOT NULL")]


def exit_upstreams(bridge: dict) -> list[dict]:
    """Выходы для моста: каждая нода-выход даёт два пути (XHTTP-Reality и Reality-TCP)."""
    out = []
    for s in q("SELECT * FROM servers WHERE role='exit' AND cascade=1 ORDER BY id"):
        info = json.loads(s["info"] or "{}")
        if not info.get("ip"):
            continue
        r = info["reality"]
        user = {"id": bridge["bridge_uuid"], "encryption": "none"}
        vnext = lambda usr: [{"address": info["ip"], "port": 443, "users": [usr]}]
        out.append({"tag": f"exit-{s['id']}-x", "outbound": {
            "protocol": "vless", "settings": {"vnext": vnext(user)},
            "streamSettings": {"network": "xhttp", "security": "reality",
                               "xhttpSettings": {"path": r["xhttp_path"], "mode": "auto"},
                               "realitySettings": {"serverName": r["sni_xhttp"], "publicKey": r["public_key"],
                                                   "shortId": r["short_id"], "fingerprint": "chrome"}}}})
        out.append({"tag": f"exit-{s['id']}-t", "outbound": {
            "protocol": "vless", "settings": {"vnext": vnext(user | {"flow": "xtls-rprx-vision"})},
            "streamSettings": {"network": "tcp", "security": "reality",
                               "realitySettings": {"serverName": r["sni_tcp"], "publicKey": r["public_key"],
                                                   "shortId": r["short_id"], "fingerprint": "chrome"}}}})
    return out


def sync_body(srv: dict, users: list[dict]) -> dict:
    if srv["role"] == "bridge":
        return {"users": users, "upstreams": exit_upstreams(srv)}
    return {"users": users + bridge_users()}


async def sync_server(srv: dict, users: list[dict]):
    st = await agent(srv, "GET", "/stats")
    now = int(time.time())
    for email, t in st["users"].items():
        total = t["up"] + t["down"]
        if total and email.startswith("k") and email[1:].isdigit():
            qx("UPDATE keys SET used_bytes=used_bytes+?, last_active=? WHERE id=?", (total, now, int(email[1:])))
    await agent(srv, "POST", "/sync", sync_body(srv, users), timeout=40)
    status = await agent(srv, "GET", "/status")
    with contextlib.suppress(Exception):
        info = await agent(srv, "GET", "/info")
        qx("UPDATE servers SET info=? WHERE id=?", (json.dumps(info), srv["id"]))
    qx("UPDATE servers SET status=?, last_seen=? WHERE id=?", (json.dumps(status), now, srv["id"]))


async def sync_all():
    # синхронизируем все ноды: скрытая из подписок нода может работать выходом для моста
    servers = q("SELECT * FROM servers")
    users = active_users()
    res = await asyncio.gather(*(sync_server(s, users) for s in servers), return_exceptions=True)
    for s, r in zip(servers, res):
        ok = not isinstance(r, Exception)
        if not ok:
            log.warning("sync %s failed: %r", s["name"], r)
        if bool(s["online"]) != ok:
            qx("UPDATE servers SET online=? WHERE id=?", (int(ok), s["id"]))
            await notify_admins(f"{'✅' if ok else '🔴'} Сервер <b>{s['name']}</b> "
                                f"{'снова в строю' if ok else 'недоступен'}")
    # после сбора статистики — проверить лимиты/сроки, уведомить и переотправить
    newly_off = [k for k in q("SELECT * FROM keys WHERE notified=0")
                 if key_state(k) in ("expired", "limit")]
    for k in newly_off:
        qx("UPDATE keys SET notified=1 WHERE id=?", (k["id"],))
        await notify_admins(f"⛔ Ключ <b>{k['name']}</b> отключён: "
                            f"{'истёк срок' if key_state(k) == 'expired' else 'исчерпан трафик'}")
    if newly_off:
        users = active_users()
        await asyncio.gather(*(agent(s, "POST", "/sync", sync_body(s, users), timeout=40)
                               for s in servers), return_exceptions=True)


_sync_lock = asyncio.Lock()


async def sync_now():
    async with _sync_lock:
        await sync_all()


async def sync_loop():
    while True:
        try:
            await sync_now()
        except Exception:
            log.exception("sync loop")
        await asyncio.sleep(SYNC_INTERVAL)


# ---------------------------------------------------------------- bot
bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


def is_admin(uid: int) -> dict | None:
    return q("SELECT * FROM admins WHERE tg_id=?", (uid,), one=True)


async def notify_admins(text: str):
    log.info("notify: %s", text)
    if not bot:
        return
    for a in q("SELECT tg_id FROM admins"):
        with contextlib.suppress(Exception):
            await bot.send_message(a["tg_id"], text, parse_mode="HTML")


async def set_menu(uid: int):
    if not bot or not WEBAPP_URL:
        return
    with contextlib.suppress(Exception):
        await bot.set_chat_menu_button(chat_id=uid, menu_button=MenuButtonWebApp(
            text="Панель", web_app=WebAppInfo(url=WEBAPP_URL)))


def fmt_bytes(n: int) -> str:
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "Б" else f"{n} {u}"
        n /= 1024
    return f"{n:.1f} ПБ"


@dp.message(Command("start"))
async def cmd_start(m: Message):
    if not is_admin(m.from_user.id):
        return await m.answer(f"Нет доступа. Ваш ID: <code>{m.from_user.id}</code>", parse_mode="HTML")
    await set_menu(m.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Открыть панель", web_app=WebAppInfo(url=WEBAPP_URL))]]) if WEBAPP_URL else None
    await m.answer("Управление VPN.\n\n/status — серверы\n/keys — ключи\n"
                   "/newkey имя [дней] [ГБ] — новый ключ\n/admins, /addadmin ID [имя], /deladmin ID",
                   reply_markup=kb)


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"<code>{m.from_user.id}</code>", parse_mode="HTML")


@dp.message(Command("status"))
async def cmd_status(m: Message):
    if not is_admin(m.from_user.id):
        return
    lines = []
    for s in q("SELECT * FROM servers ORDER BY id"):
        st = json.loads(s["status"] or "{}")
        if not s["online"] or not st:
            lines.append(f"🔴 <b>{s['name']}</b> — недоступен")
            continue
        line = (f"{'🟢' if st['xray']['active'] else '🟠'} {'🌉 ' if s['role'] == 'bridge' else ''}"
                f"<b>{s['name']}</b>: CPU {st['cpu']:.0f}% · "
                f"RAM {st['ram']['used'] * 100 // st['ram']['total']}% · ключей {st['xray']['users']}")
        if s["role"] == "bridge":
            ex = st.get("exits") or []
            line += f"\n    выходы: {sum(e['alive'] for e in ex)}/{len(ex)} живы"
        lines.append(line)
    await m.answer("\n".join(lines) or "Серверов нет", parse_mode="HTML")


@dp.message(Command("keys"))
async def cmd_keys(m: Message):
    if not is_admin(m.from_user.id):
        return
    icons = {"active": "🟢", "disabled": "⚪", "expired": "⌛", "limit": "📛"}
    lines = [f"{icons[key_state(k)]} {k['name']} — {fmt_bytes(k['used_bytes'])}"
             for k in q("SELECT * FROM keys ORDER BY id DESC LIMIT 50")]
    await m.answer("\n".join(lines) or "Ключей нет")


@dp.message(Command("newkey"))
async def cmd_newkey(m: Message, command: CommandObject):
    if not is_admin(m.from_user.id):
        return
    args = (command.args or "").split()
    if not args:
        return await m.answer("Формат: /newkey имя [дней, 0=бессрочно] [ГБ, 0=безлимит]")
    try:
        days = float(args[1]) if len(args) > 1 else 30
        gb = float(args[2]) if len(args) > 2 else 0
    except ValueError:
        return await m.answer("Дни и ГБ — числа")
    k = key_out(create_key(args[0], days, gb, m.from_user.id))
    asyncio.create_task(sync_now())
    if k["sub_url"]:
        return await m.answer(f"Ключ <b>{k['name']}</b> создан.\nПодписка:\n<code>{k['sub_url']}</code>",
                              parse_mode="HTML")
    links = "\n\n".join(f"<code>{l}</code>" for l in key_links(k))
    await m.answer(f"Ключ <b>{k['name']}</b> создан.\n\n{links or 'Серверов пока нет'}", parse_mode="HTML")


@dp.message(Command("admins"))
async def cmd_admins(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("\n".join(f"{'👑' if a['is_owner'] else '👤'} <code>{a['tg_id']}</code> {a['name'] or ''}"
                             for a in q("SELECT * FROM admins")), parse_mode="HTML")


@dp.message(Command("addadmin"))
async def cmd_addadmin(m: Message, command: CommandObject):
    a = is_admin(m.from_user.id)
    if not a or not a["is_owner"]:
        return await m.answer("Только владелец")
    args = (command.args or "").split(maxsplit=1)
    if not args or not args[0].isdigit():
        return await m.answer("Формат: /addadmin TG_ID [имя]")
    qx("INSERT OR REPLACE INTO admins(tg_id,name,is_owner) VALUES(?,?,0)",
       (int(args[0]), args[1] if len(args) > 1 else ""))
    await set_menu(int(args[0]))
    await m.answer("Добавлен")


@dp.message(Command("deladmin"))
async def cmd_deladmin(m: Message, command: CommandObject):
    a = is_admin(m.from_user.id)
    if not a or not a["is_owner"]:
        return await m.answer("Только владелец")
    qx("DELETE FROM admins WHERE tg_id=? AND is_owner=0", (int((command.args or "0").strip() or 0),))
    await m.answer("Удалён")


# ---------------------------------------------------------------- web
@contextlib.asynccontextmanager
async def lifespan(_):
    # на сервере с панелью локальная нода регистрируется автоматически
    if os.environ.get("LOCAL_AGENT_URL") and not q("SELECT id FROM servers LIMIT 1"):
        srv = {"name": os.environ.get("LOCAL_NAME", "main"), "url": os.environ["LOCAL_AGENT_URL"],
               "secret": os.environ["LOCAL_AGENT_SECRET"]}
        try:
            info = await agent(srv, "GET", "/info")
            qx("INSERT INTO servers(name,url,secret,info) VALUES(?,?,?,?)",
               (srv["name"], srv["url"], srv["secret"], json.dumps(info)))
        except Exception as e:
            log.warning("local agent not registered: %r", e)
    tasks = [asyncio.create_task(sync_loop())]
    if bot:
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False)))
        for a in q("SELECT tg_id FROM admins"):
            await set_menu(a["tg_id"])
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
_secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()


def tg_user(x_init_data: str = Header(default=""), x_cli_token: str = Header(default="")) -> dict:
    # команда xvpn на этом сервере: токен из /etc/xvpn/panel.env, API слушает только 127.0.0.1
    if CLI_TOKEN and x_cli_token and hmac.compare_digest(x_cli_token, CLI_TOKEN):
        return {"id": 0, "first_name": "cli", "is_owner": True}
    if x_cli_token:
        raise HTTPException(401, "неверный CLI_TOKEN — проверьте /etc/xvpn/panel.env и перезапустите xvpn-panel")
    if not BOT_TOKEN:
        raise HTTPException(401, "bot disabled")
    data = dict(parse_qsl(x_init_data, keep_blank_values=True))
    h = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    calc = hmac.new(_secret_key, check.encode(), hashlib.sha256).hexdigest()
    if not h or not hmac.compare_digest(calc, h):
        raise HTTPException(401, "bad initData")
    if time.time() - int(data.get("auth_date", 0)) > DAY:
        raise HTTPException(401, "initData expired")
    user = json.loads(data.get("user", "{}"))
    a = is_admin(int(user.get("id", 0)))
    if not a:
        raise HTTPException(403, "not admin")
    return {**user, "is_owner": bool(a["is_owner"])}


def owner(u=Depends(tg_user)):
    if not u["is_owner"]:
        raise HTTPException(403, "owner only")
    return u


P = PANEL_PATH


@app.get(P + "/")
def webapp():
    return FileResponse(os.path.join(HERE, "webapp", "index.html"))


@app.get(P + "/api/me")
def api_me(u=Depends(tg_user)):
    return u


@app.get(P + "/api/overview")
def api_overview(u=Depends(tg_user)):
    keys = q("SELECT * FROM keys")
    servers = []
    names = {s["id"]: s["name"] for s in q("SELECT id,name FROM servers")}
    for s in q("SELECT * FROM servers ORDER BY role='bridge' DESC, id"):
        st = json.loads(s["status"] or "null")
        if st and st.get("exits"):
            for e in st["exits"]:  # exit-<id>-x|t → имя ноды и протокол
                parts = e["tag"].split("-")
                sid = int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else 0
                e["server_id"], e["name"] = sid, names.get(sid, e["tag"])
                e["proto"] = {"x": "XHTTP", "t": "TCP"}.get(parts[-1], parts[-1])
        servers.append({"id": s["id"], "name": s["name"], "url": s["url"], "enabled": s["enabled"],
                        "role": s["role"] or "exit", "cascade": s["cascade"],
                        "online": bool(s["online"]) and s["last_seen"] > time.time() - 3 * SYNC_INTERVAL,
                        "last_seen": s["last_seen"], "status": st,
                        "info": json.loads(s["info"] or "null")})
    states = [key_state(k) for k in keys]
    return {"servers": servers, "cdn": cdn_on(), "keys_total": len(keys), "keys_active": states.count("active"),
            "keys_online": sum(k["last_active"] > time.time() - 180 for k in keys),
            "traffic_total": sum(k["used_bytes"] for k in keys),
            "balancer": balancer_info()}


def balancer_info():
    """Состояние балансировщика (если установлен): лимит и сколько отдал каждый выход."""
    try:
        st = json.load(open("/etc/xvpn/balancer.json", encoding="utf-8"))
        limit_gb = 200.0
        try:
            for line in open("/etc/xvpn/balancer.env", encoding="utf-8"):
                if line.startswith("LIMIT_GB="):
                    limit_gb = float(line.split("=", 1)[1].strip().strip("'\""))
        except FileNotFoundError:
            pass
        return {"limit_bytes": int(limit_gb * 1024**3),
                "served": {int(k): v for k, v in (st.get("served") or {}).items()},
                "ts": st.get("ts", 0)}
    except FileNotFoundError:
        return None


@app.get(P + "/api/keys")
def api_keys(u=Depends(tg_user)):
    return [key_out(k) for k in q("SELECT * FROM keys ORDER BY id DESC")]


@app.post(P + "/api/keys")
async def api_key_create(body: dict, u=Depends(tg_user)):
    name = str(body.get("name", "")).strip()[:64]
    if not name:
        raise HTTPException(400, "name required")
    k = create_key(name, float(body.get("days") or 0), float(body.get("gb") or 0), u["id"])
    asyncio.create_task(sync_now())
    return key_out(k)


@app.patch(P + "/api/keys/{kid}")
async def api_key_edit(kid: int, body: dict, u=Depends(tg_user)):
    k = q("SELECT * FROM keys WHERE id=?", (kid,), one=True) or {}
    if not k:
        raise HTTPException(404)
    if "name" in body:
        k["name"] = str(body["name"]).strip()[:64] or k["name"]
    if "enabled" in body:
        k["enabled"] = int(bool(body["enabled"]))
    if "limit_gb" in body:
        k["limit_bytes"] = int(float(body["limit_gb"] or 0) * GB)
    if "add_days" in body:
        base = max(k["expire_at"], time.time()) if k["expire_at"] else time.time()
        k["expire_at"] = int(base + float(body["add_days"]) * DAY)
    if body.get("unlimited_time"):
        k["expire_at"] = 0
    if body.get("reset_traffic"):
        k["used_bytes"] = 0
    if body.get("regen"):
        k["uuid"], k["sub_token"] = str(uuidlib.uuid4()), secrets.token_urlsafe(16)
    k["notified"] = 0 if key_state(k) == "active" else k["notified"]
    qx("UPDATE keys SET name=?,enabled=?,limit_bytes=?,expire_at=?,used_bytes=?,uuid=?,sub_token=?,notified=? "
       "WHERE id=?", (k["name"], k["enabled"], k["limit_bytes"], k["expire_at"], k["used_bytes"],
                      k["uuid"], k["sub_token"], k["notified"], kid))
    asyncio.create_task(sync_now())
    return key_out(k)


@app.delete(P + "/api/keys/{kid}")
async def api_key_del(kid: int, u=Depends(tg_user)):
    qx("DELETE FROM keys WHERE id=?", (kid,))
    asyncio.create_task(sync_now())
    return {"ok": True}


@app.get(P + "/api/keys/{kid}/links")
def api_key_links(kid: int, u=Depends(tg_user)):
    k = q("SELECT * FROM keys WHERE id=?", (kid,), one=True)
    if not k:
        raise HTTPException(404)
    return {"sub_url": key_out(k)["sub_url"], "links": key_links(k)}


@app.post(P + "/api/servers")
async def api_server_add(body: dict, u=Depends(owner)):
    srv = {"name": str(body.get("name", "")).strip()[:32],
           "url": str(body.get("url", "")).strip(), "secret": str(body.get("secret", "")).strip(), "cert": None}
    if srv["url"] and not srv["url"].startswith("http"):  # во второе поле вставили код подключения
        body["code"], srv["url"] = srv["url"], ""
    if body.get("code"):
        try:
            c = parse_code(str(body["code"]))
        except Exception:
            raise HTTPException(400, "код подключения не распознан — скопируйте его целиком")
        srv["url"], srv["secret"] = c["url"], c["secret"]
        if c["fp"]:
            try:
                srv["cert"] = await asyncio.to_thread(fetch_pinned_cert, c["url"], c["fp"])
            except Exception as e:
                raise HTTPException(400, f"не удалось проверить сертификат агента: {e}")
    if not srv["url"] or not srv["secret"]:
        raise HTTPException(400, "нужен код подключения или URL и секрет агента")
    try:
        info = await agent(srv, "GET", "/info", timeout=40)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        hint = {401: "неверный секрет — скопируйте код заново (xvpn code)",
                404: "по этому адресу нет агента — домен указывает на другой сервер?",
                521: "Cloudflare: сервер не принимает подключения на 443",
                522: "Cloudflare: не дождался сервера на 443 — проверьте файрвол хостера",
                525: "Cloudflare: ошибка TLS с сервером", 526: "Cloudflare: сертификат сервера не принят"}
        raise HTTPException(400, f"агент {srv['url']} ответил {code}: {hint.get(code, e.response.text[:120])}")
    except Exception as e:
        # у таймаутов httpx пустой текст — показываем тип ошибки и адрес
        raise HTTPException(400, f"агент {srv['url']} не отвечает: {type(e).__name__} {e}".strip())
    if q("SELECT id FROM servers WHERE url=?", (srv["url"],)):
        raise HTTPException(400, "этот сервер уже добавлен")
    srv["name"] = srv["name"] or str(info.get("name") or "")[:32] or "node"
    srv_role = info.get("role", "exit")
    qx("INSERT INTO servers(name,url,secret,info,role,cert,bridge_uuid) VALUES(?,?,?,?,?,?,?)",
       (srv["name"], srv["url"], srv["secret"], json.dumps(info), srv_role, srv["cert"],
        str(uuidlib.uuid4()) if srv_role == "bridge" else None))
    asyncio.create_task(sync_now())
    return {"ok": True}


@app.patch(P + "/api/servers/{sid}")
async def api_server_edit(sid: int, body: dict, u=Depends(owner)):
    s = q("SELECT * FROM servers WHERE id=?", (sid,), one=True)
    if not s:
        raise HTTPException(404)
    if "name" in body:
        qx("UPDATE servers SET name=? WHERE id=?", (str(body["name"]).strip()[:32] or s["name"], sid))
    if "enabled" in body:
        qx("UPDATE servers SET enabled=? WHERE id=?", (int(bool(body["enabled"])), sid))
    if "cascade" in body:
        qx("UPDATE servers SET cascade=? WHERE id=?", (int(bool(body["cascade"])), sid))
    if body.get("refresh_info"):
        qx("UPDATE servers SET info=? WHERE id=?", (json.dumps(await agent(s, "GET", "/info")), sid))
    asyncio.create_task(sync_now())
    return {"ok": True}


@app.delete(P + "/api/servers/{sid}")
async def api_server_del(sid: int, u=Depends(owner)):
    qx("DELETE FROM servers WHERE id=?", (sid,))
    asyncio.create_task(sync_now())
    return {"ok": True}


@app.post(P + "/api/servers/{sid}/restart")
async def api_server_restart(sid: int, u=Depends(tg_user)):
    s = q("SELECT * FROM servers WHERE id=?", (sid,), one=True)
    if not s:
        raise HTTPException(404)
    return await agent(s, "POST", "/restart")


@app.get(P + "/api/settings")
def api_settings(u=Depends(tg_user)):
    return {"cdn": cdn_on()}


@app.patch(P + "/api/settings")
def api_settings_edit(body: dict, u=Depends(owner)):
    if "cdn" in body:
        set_setting("cdn", "1" if body["cdn"] else "0")
    return {"cdn": cdn_on()}


@app.post(P + "/api/sync")
async def api_sync(u=Depends(tg_user)):
    await sync_now()
    return {"ok": True}


@app.get(P + "/api/admins")
def api_admins(u=Depends(tg_user)):
    return q("SELECT * FROM admins ORDER BY is_owner DESC, tg_id")


@app.post(P + "/api/admins")
async def api_admin_add(body: dict, u=Depends(owner)):
    tid = int(body.get("tg_id") or 0)
    if tid <= 0:
        raise HTTPException(400, "tg_id")
    qx("INSERT OR REPLACE INTO admins(tg_id,name,is_owner) VALUES(?,?,0)", (tid, str(body.get("name", ""))[:64]))
    await set_menu(tid)
    return {"ok": True}


@app.delete(P + "/api/admins/{tid}")
def api_admin_del(tid: int, u=Depends(owner)):
    qx("DELETE FROM admins WHERE tg_id=? AND is_owner=0", (tid,))
    return {"ok": True}


# ---------------------------------------------------------------- subscription
@app.get(SUB_PATH + "/{token}")
def subscription(token: str):
    k = q("SELECT * FROM keys WHERE sub_token=?", (token,), one=True)
    if not k:
        raise HTTPException(404)
    links = key_links(k) if key_state(k) == "active" else []
    body = base64.b64encode("\n".join(links).encode()).decode()
    title = base64.b64encode(f"{SUB_TITLE} | {k['name']}".encode()).decode()
    return PlainTextResponse(body, headers={
        "profile-title": f"base64:{title}",
        "profile-update-interval": "12",
        "subscription-userinfo": f"upload=0; download={k['used_bytes']}; "
                                 f"total={k['limit_bytes']}; expire={k['expire_at']}",
        "content-disposition": 'inline; filename="xvpn"',
    })
