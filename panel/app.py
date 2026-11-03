"""xvpn — главный сервер: ключи, подписки, синхронизация нод.

Интерфейсы: команда xvpn в SSH (всегда, через локальный API с X-CLI-Token),
Telegram-бот (если задан BOT_TOKEN) и мини-апп (если ещё и DOMAIN)."""
import asyncio
import base64
import concurrent.futures
import contextlib
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import socket
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
# fingerprint клиента на входе в мост: рукопожатие chrome больше одного TCP-пакета, и у части
# клиентов вторая часть не доходит. ios с нашими мостами не работает (проверено 02.11)
BRIDGE_FP = os.environ.get("BRIDGE_FP", "safari")
OFFLINE_AFTER = 3  # столько синков подряд без ответа — нода считается недоступной
REBALANCE_GAP = 6 * 3600  # автоматический переезд ключа — не чаще раза за столько секунд
# заблокированные в РФ сайты в .ru: напрямую с моста они не откроются
DEFAULT_EXIT_DOMAINS = ["domain:novayagazeta.ru", "domain:echo.msk.ru", "domain:grani.ru",
                        "domain:tvrain.ru", "domain:ej.ru", "domain:kasparov.ru", "domain:rferl.ru"]

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
CREATE TABLE IF NOT EXISTS entry_traffic(day TEXT, sid INTEGER, inbound TEXT, up INTEGER DEFAULT 0,
  down INTEGER DEFAULT 0, PRIMARY KEY(day, sid, inbound));
""")
# миграции: каскад (role = exit|bridge, cert — закреплённый сертификат агента моста,
# bridge_uuid — служебный пользователь моста на выходах, cascade — нода работает выходом для мостов)
_cols = {r[1] for r in db.execute("PRAGMA table_info(servers)")}
for _c, _d in (("role", "TEXT DEFAULT 'exit'"), ("cert", "TEXT"), ("bridge_uuid", "TEXT"),
               ("cascade", "INTEGER DEFAULT 1")):
    if _c not in _cols:
        db.execute(f"ALTER TABLE servers ADD COLUMN {_c} {_d}")
# load_bytes — скользящая нагрузка ключа (затухает), used_bytes — накопленный трафик
if "load_bytes" not in {r[1] for r in db.execute("PRAGMA table_info(keys)")}:
    db.execute("ALTER TABLE keys ADD COLUMN load_bytes INTEGER DEFAULT 0")
    db.execute("UPDATE keys SET load_bytes=used_bytes")
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


# ---------------------------------------------------------------- кластер мостов
def cluster_conf() -> dict | None:
    """Настройки кластера мостов (общая Reality-идентичность) или None, если выключен."""
    if setting("cluster", "0") != "1":
        return None
    host = setting("cluster_host", "")
    try:
        r = json.loads(setting("cluster_reality", "") or "{}")
    except ValueError:
        r = {}
    if host and r.get("private_key"):
        return {"host": host, **r}
    return None


def cluster_enable(host: str) -> dict:
    """Включить кластер мостов: сгенерировать общую Reality-идентичность, если её ещё нет."""
    import re as _re
    import subprocess
    host = host.strip().lower()
    if not _re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host):
        raise HTTPException(400, "Нужен домен кластера, например ru.example.com")
    try:
        r = json.loads(setting("cluster_reality", "") or "{}")
    except ValueError:
        r = {}
    if not r.get("private_key"):
        kp = subprocess.run(["/usr/local/bin/xray", "x25519"], capture_output=True, text=True)
        lines = [l for line in kp.stdout.splitlines() if (l := line.strip())]
        # формат вывода менялся между версиями: значения — после двоеточия в первых двух строках
        if len(lines) >= 2 and ":" in lines[0] and ":" in lines[1]:
            r_priv, r_pub = lines[0].rsplit(":", 1)[-1].strip(), lines[1].rsplit(":", 1)[-1].strip()
        else:
            r_priv = r_pub = ""
        if not r_priv or not r_pub:
            raise HTTPException(500, "Не удалось сгенерировать ключи: " + kp.stdout[:200])
        r = {"private_key": r_priv, "public_key": r_pub}
        b = q("SELECT info FROM servers WHERE role='bridge' ORDER BY id LIMIT 1")
        sni = "ya.ru"
        if b:
            with contextlib.suppress(Exception):
                sni = json.loads(b[0]["info"] or "{}")["reality"]["sni_tcp"]
        r = {"private_key": r_priv, "public_key": r_pub,
             "short_id": secrets.token_hex(8), "sni_tcp": sni}
        set_setting("cluster_reality", json.dumps(r))
    set_setting("cluster_host", host)
    set_setting("cluster", "1")
    return {"host": host, **r}


# ---------------------------------------------------------------- keys
_ru_cdn_cache = {"t": 0.0, "host": ""}


def main_ru_cdn() -> str:
    """Хост российского CDN главного сервера (если настроен xvpn rucdn).

    Через него доступна подписка, когда Cloudflare из сети клиента тормозит."""
    now = time.time()
    if now - _ru_cdn_cache["t"] < 60:
        return _ru_cdn_cache["host"]
    host = ""
    local = os.environ.get("LOCAL_AGENT_URL", "")
    if local:
        try:
            r = q("SELECT info FROM servers WHERE url=?", (local,))
            if r:
                host = (json.loads(r[0]["info"] or "{}").get("ru_cdn") or {}).get("host", "")
        except Exception:
            pass
    _ru_cdn_cache.update(t=now, host=host)
    return host


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
    # sub_host — публичный адрес подписки, по которому главный сервер не виден
    rc = setting("sub_host", "") or main_ru_cdn()
    k["sub_url_ru"] = f"https://{rc}{SUB_PATH}/{k['sub_token']}" if rc else None
    k["online"] = k["last_active"] > time.time() - 180
    return k


def create_key(name: str, days: float, gb: float, by: int) -> dict:
    kid = qx("INSERT INTO keys(name,uuid,sub_token,limit_bytes,expire_at,created_at,created_by) "
             "VALUES(?,?,?,?,?,?,?)",
             (name, str(uuidlib.uuid4()), secrets.token_urlsafe(16), int(gb * GB),
              int(time.time() + days * DAY) if days else 0, int(time.time()), by))
    return q("SELECT * FROM keys WHERE id=?", (kid,), one=True)


def ru_cdn_link(k: dict, info: dict, name: str) -> str:
    """Ссылка через российский CDN: packet-up, отправка GET с данными в заголовках (POST там режут).

    Пакеты до 1500 байт: крупнее не влезают в заголовки и получают 431."""
    host = info["ru_cdn"]["host"]
    extra = quote(json.dumps({"uplinkHTTPMethod": "GET", "uplinkDataPlacement": "header",
                              "scMaxEachPostBytes": 1500, "scMinPostsIntervalMs": 10},
                             separators=(",", ":")), safe="")
    return (f"vless://{k['uuid']}@{host}:443?encryption=none&security=tls&sni={host}"
            f"&alpn=h2%2Chttp%2F1.1&fp=chrome&type=xhttp&host={host}"
            f"&path={quote(info['xhttp_path'], safe='')}&mode=packet-up&extra={extra}#{name}")


def server_links(srv: dict, k: dict, cdn_only: bool = False) -> list[str]:
    info = json.loads(srv["info"] or "{}")
    if not info:
        return []
    u, n, r = k["uuid"], srv["name"], info["reality"]
    tag = lambda t: quote(f"{n} {t} | {k['name']}")
    h = f"[{info['ip']}]" if ":" in info["ip"] else info["ip"]  # IPv6 в ссылке — в скобках
    if info.get("role") == "bridge":
        return [f"vless://{u}@{h}:{info.get('port', 443)}?encryption=none&security=reality"
                f"&sni={r['sni_tcp']}&fp={BRIDGE_FP}&pbk={r['public_key']}&sid={r['short_id']}"
                f"&type=tcp&flow=xtls-rprx-vision#{tag('Каскад')}"]
    cdn = []
    rc = info.get("ru_cdn")
    if rc and rc.get("host"):
        cdn.append(ru_cdn_link(k, info, tag("РФ-CDN")))
    if info.get("domain") and (cdn_on() or cdn_only):
        # в аварийном режиме (cdn_only) CF-ссылки добавляем даже при выключенном CDN:
        # медленный канал лучше, чем никакого
        cdn += [f"vless://{u}@{info['domain']}:443?encryption=none&security=tls&sni={info['domain']}"
               f"&alpn=h2%2Chttp%2F1.1&fp=chrome&type=xhttp&host={info['domain']}"
               # packet-up: каждый кусок — отдельный запрос; через Cloudflare работает и без gRPC
               # (в stream-up Cloudflare без gRPC буферизует отправку — «нет обратного трафика»)
               f"&path={quote(info['xhttp_path'], safe='')}&mode=packet-up#{tag('XHTTP-CDN')}"]
    if cdn_only:
        return cdn
    return cdn + [
        f"vless://{u}@{h}:443?encryption=none&security=reality&sni={r['sni_tcp']}"
        f"&fp=chrome&pbk={r['public_key']}&sid={r['short_id']}&type=tcp&flow=xtls-rprx-vision"
        f"#{tag('Reality')}",
        f"vless://{u}@{h}:443?encryption=none&security=reality&sni={r['sni_xhttp']}"
        f"&fp=chrome&pbk={r['public_key']}&sid={r['short_id']}&type=xhttp"
        f"&path={quote(r['xhttp_path'], safe='')}&mode=auto#{tag('XHTTP-Reality')}",
    ]


def cluster_link(cl: dict, k: dict, label: str = "Кластер RU") -> str:
    return (f"vless://{k['uuid']}@{cl['host']}:443?encryption=none&security=reality"
            f"&sni={cl['sni_tcp']}&fp={BRIDGE_FP}&pbk={cl['public_key']}&sid={cl['short_id']}"
            f"&type=tcp&flow=xtls-rprx-vision#{quote(label + ' | ' + k['name'])}")


def key_links(k: dict) -> list[str]:
    # В подписке — только каскад: прямые ссылки на выходы не светим,
    # чтобы клиенты не ходили мимо российских мостов (меньше поводов для блокировки).
    # Недоступные ноды (online=0 по итогам sync) из подписок убираем.
    enabled = q("SELECT * FROM servers WHERE enabled=1 ORDER BY role='bridge' DESC, id")
    servers = [s for s in enabled if s["online"]]
    cl = cluster_conf()
    # главный может не видеть мосты, когда клиенты их видят: online здесь не решает
    isolated = [s for s in enabled if s["role"] == "bridge" and json.loads(s["info"] or "{}").get("standalone")]
    all_bridges = [s for s in enabled if s["role"] == "bridge" and s not in isolated]
    bridges = [s for s in all_bridges if s["online"]] or all_bridges
    if cl and len(all_bridges) >= 2:
        # кластер: одна ссылка на все мосты (DNS отдаёт адреса всех живых мостов)
        out = [cluster_link(cl, k, "Wi-Fi")]
    elif bridges:
        out = [l for s in bridges for l in server_links(s, k)]
    else:
        # мостов нет — прямые ссылки на выходы
        out = []
        for s in servers:
            out += server_links(s, k)
    # второй профиль — для мобильной сети, где оператор режет IP мостов: один вход через
    # российский CDN, мост с CDN приоритетнее (трафик идёт каскадом), иначе выход
    # отдельный мост со своей маскировкой — запасной конфиг для выбранных ключей
    site_keys = setting("site_bridge_keys", "").split()
    if "*" in site_keys or k["name"] in site_keys:
        for s in isolated:
            out += [l.split("#")[0] + "#" + quote(f"Резерв | {k['name']}") for l in server_links(s, k)]
    # Если задан mobile_bridge — это мост в сети, которой доверяет оператор: вход прямо на его IP
    mob = next((s for s in all_bridges if s["name"] == setting("mobile_bridge", "")), None)
    if mob and cl:
        out.append(cluster_link(cl | {"host": json.loads(mob["info"] or "{}")["ip"]}, k, "Мобильный"))
    else:
        for s in servers:
            info = json.loads(s["info"] or "{}")
            if (info.get("ru_cdn") or {}).get("host") and info.get("xhttp_path"):
                out.append(ru_cdn_link(k, info, quote(f"Мобильный | {k['name']}")))
                break
    if setting("emergency", "0") == "1":
        # аварийный канал: CDN-ссылки на выходы. IP серверов за CDN не светятся —
        # единственный путь, если адреса мостов оказались в блоке. Обычно скрыт.
        have = {l.split("#")[0] for l in out}  # РФ-CDN уже есть в профиле «Мобильный»
        out += [l for s in servers if s["role"] == "exit" for l in server_links(s, k, cdn_only=True)
                if l.split("#")[0] not in have]
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


def exit_map_get() -> dict[int, int]:
    """Привязки ключей к выходам каскада: {id ключа: id выхода}."""
    try:
        return {int(k): int(v) for k, v in json.loads(setting("exit_map", "") or "{}").items()}
    except (ValueError, TypeError):
        return {}


def exit_pins_get() -> set[int]:
    """Ключи, которым выход назначен вручную: автоматика их не двигает."""
    try:
        return {int(k) for k in json.loads(setting("exit_pins", "") or "[]")}
    except (ValueError, TypeError):
        return set()


def exit_ids() -> list[int]:
    return [s["id"] for s in q("SELECT id FROM servers WHERE role='exit' AND cascade=1 ORDER BY id")]


def assign_exits(users: list[dict], sids: list[int]):
    """Умное закрепление ключей за выходами каскада: липко и по реальной нагрузке.

    Вызывается один раз за синк. Нагрузка — скользящая (keys.load_bytes), а каждый
    ключ добавляет к выходу ещё 1 ГБ, иначе новые ключи с нулём трафика копятся
    на одном выходе. Переезд при перекосе — не чаще раза в REBALANCE_GAP и только
    если он реально уменьшает максимум: так ключ не прыгает туда-обратно
    (каждый переезд перезапускает Xray на мостах и рвёт соединения).
    Привязки хранятся в settings.exit_map: {id ключа: id выхода}."""
    if not sids:
        return
    m = exit_map_get()
    pins = exit_pins_get()
    kids = {int(u["email"][1:]) for u in users
            if u.get("email", "").startswith("k") and u["email"][1:].isdigit()}
    old = dict(m)
    m = {k: v for k, v in m.items() if k in kids and v in sids}
    weight = {r["id"]: r["load_bytes"] for r in q("SELECT id, load_bytes FROM keys")}

    def load(sid: int) -> int:
        return sum(weight.get(k, 0) + GB for k, v in m.items() if v == sid)

    for kid in sorted(kids - set(m)):
        m[kid] = min(sids, key=lambda s: (load(s), sids.index(s)))
    now = time.time()
    if len(sids) > 1 and now - float(setting("exit_rebalance_ts", "0") or 0) >= REBALANCE_GAP:
        loads = {s: load(s) for s in sids}
        heavy, light = max(loads, key=loads.get), min(loads, key=loads.get)
        gap = loads[heavy] - loads[light]
        # перекос больше чем вдвое и хотя бы на 20 ГБ; ключ тяжелее разрыва только поменяет выходы местами
        if loads[heavy] > 2 * loads[light] + 20 * GB:
            cand = [k for k, v in m.items() if v == heavy and k not in pins and weight.get(k, 0) + GB < gap]
            if cand:
                m[min(cand, key=lambda k: abs(gap - 2 * (weight.get(k, 0) + GB)))] = light
                set_setting("exit_rebalance_ts", str(int(now)))
    if m != old:
        set_setting("exit_map", json.dumps(m))


def with_exits(users: list[dict]) -> list[dict]:
    m = exit_map_get()
    out = []
    for u in users:
        e = u.get("email", "")
        if e.startswith("k") and e[1:].isdigit() and int(e[1:]) in m:
            out.append(u | {"exit": m[int(e[1:])]})
        else:
            out.append(u)
    return out


def exit_domains() -> list[str]:
    """Домены, которые мост всегда ведёт через выход: заблокированные сайты в .ru и т. п."""
    try:
        return json.loads(setting("exit_domains", "") or "null") or DEFAULT_EXIT_DOMAINS
    except ValueError:
        return DEFAULT_EXIT_DOMAINS


def sync_body(srv: dict, users: list[dict]) -> dict:
    if srv["role"] == "bridge":
        c = cluster_conf()
        return {"users": with_exits(users), "upstreams": exit_upstreams(srv), "exit_domains": exit_domains(),
                "cluster": {k: c[k] for k in ("private_key", "public_key", "short_id", "sni_tcp")} if c else None}
    return {"users": users + bridge_users()}


async def sync_server(srv: dict, users: list[dict]):
    st = await agent(srv, "GET", "/stats")
    now = int(time.time())
    for email, t in st["users"].items():
        total = t["up"] + t["down"]
        if total and email.startswith("k") and email[1:].isdigit():
            qx("UPDATE keys SET used_bytes=used_bytes+?, load_bytes=load_bytes+?, last_active=? WHERE id=?",
               (total, total, now, int(email[1:])))
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    for tag, t in (st.get("inbounds") or {}).items():
        if t["up"] or t["down"]:
            qx("INSERT INTO entry_traffic(day,sid,inbound,up,down) VALUES(?,?,?,?,?) ON CONFLICT(day,sid,inbound) "
               "DO UPDATE SET up=up+excluded.up, down=down+excluded.down", (day, srv["id"], tag, t["up"], t["down"]))
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
    # нагрузка затухает с полупериодом около 3 суток
    if time.time() - _load_decay["t"] >= 3600:
        _load_decay["t"] = time.time()
        qx("UPDATE keys SET load_bytes=CAST(load_bytes*0.99 AS INTEGER)")
    assign_exits(users, exit_ids())
    res = await asyncio.gather(*(sync_server(s, users) for s in servers), return_exceptions=True)
    for s, r in zip(servers, res):
        ok = not isinstance(r, Exception)
        if not ok:
            log.warning("sync %s failed: %r", s["name"], r)
        # один сбой синка — ещё не падение: online влияет на подписки и алерты
        _sync_fails[s["id"]] = 0 if ok else _sync_fails.get(s["id"], 0) + 1
        if ok != bool(s["online"]) and (ok or _sync_fails[s["id"]] >= OFFLINE_AFTER):
            qx("UPDATE servers SET online=? WHERE id=?", (int(ok), s["id"]))
            await notify_admins(f"{'✅' if ok else '🔴'} Сервер <b>{esc(s['name'])}</b> "
                                f"{'снова в строю' if ok else 'недоступен'}")
    # после сбора статистики — проверить лимиты/сроки, уведомить и переотправить
    newly_off = [k for k in q("SELECT * FROM keys WHERE notified=0")
                 if key_state(k) in ("expired", "limit")]
    for k in newly_off:
        qx("UPDATE keys SET notified=1 WHERE id=?", (k["id"],))
        await notify_admins(f"⛔ Ключ <b>{esc(k['name'])}</b> отключён: "
                            f"{'истёк срок' if key_state(k) == 'expired' else 'исчерпан трафик'}")
    if newly_off:
        users = active_users()
        await asyncio.gather(*(agent(s, "POST", "/sync", sync_body(s, users), timeout=40)
                               for s in servers), return_exceptions=True)


_sync_lock = asyncio.Lock()
_sync_fails: dict[int, int] = {}
_load_decay = {"t": time.time()}


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


def esc(s) -> str:
    """Экранирование для parse_mode=HTML: символ < в имени ломает всё сообщение."""
    return html.escape(str(s), quote=False)


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
        # основная — через РФ-CDN: Cloudflare у части операторов РФ заблокирован
        main_url, spare = (k["sub_url_ru"], k["sub_url"]) if k.get("sub_url_ru") else (k["sub_url"], "")
        ru = f"\nЗапасная (Cloudflare):\n<code>{spare}</code>" if spare else ""
        return await m.answer(f"Ключ <b>{esc(k['name'])}</b> создан.\nПодписка:\n<code>{main_url}</code>{ru}",
                              parse_mode="HTML")
    links = "\n\n".join(f"<code>{esc(l)}</code>" for l in key_links(k))
    await m.answer(f"Ключ <b>{esc(k['name'])}</b> создан.\n\n{links or 'Серверов пока нет'}", parse_mode="HTML")


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
    cl = cluster_conf()
    return {"servers": servers, "cdn": cdn_on(), "keys_total": len(keys), "keys_active": states.count("active"),
            "keys_online": sum(k["last_active"] > time.time() - 180 for k in keys),
            "traffic_total": sum(k["used_bytes"] for k in keys),
            "cluster": {"host": cl["host"], "addrs": cluster_dns(cl["host"])} if cl else None,
            "mobile_bridge": setting("mobile_bridge", ""),
            "entries": entry_stats(),
            "balancer": balancer_info(),
            "canary": canary_info()}


def entry_stats() -> list[dict]:
    """Трафик по точкам входа клиентов: сегодня, за месяц (UTC) и всего.

    Reality на выходах не считаем: это трафик мостов, он уже учтён на входе."""
    servers = {s["id"]: s for s in q("SELECT id, name, info FROM servers")}
    mobile = setting("mobile_bridge", "")
    today = time.strftime("%Y-%m-%d", time.gmtime())
    entries = {n: {"name": n, "today": 0, "month": 0, "total": 0, "servers": {}}
               for n in ("Wi-Fi", "Резерв", "Мобильный", "CDN")}
    for r in q("SELECT day, sid, inbound, up + down AS bytes FROM entry_traffic"):
        s = servers.get(r["sid"])
        if not s:
            continue
        if r["inbound"] in ("xhttp-cdn", "bridge-cdn"):
            e = entries["CDN"]
        elif r["inbound"] == "bridge-in":
            standalone = json.loads(s["info"] or "{}").get("standalone")
            e = entries["Резерв" if standalone else "Мобильный" if s["name"] == mobile else "Wi-Fi"]
        else:
            continue
        e["total"] += r["bytes"]
        if r["day"][:7] == today[:7]:
            e["month"] += r["bytes"]
            e["servers"][s["name"]] = e["servers"].get(s["name"], 0) + r["bytes"]
        if r["day"] == today:
            e["today"] += r["bytes"]
    return list(entries.values())


def cluster_dns(host: str) -> list[str]:
    """Актуальные A-записи домена кластера (точки входа)."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            infos = ex.submit(socket.getaddrinfo, host, 443, socket.AF_INET).result(timeout=3)
        return sorted({i[4][0] for i in infos})
    except Exception:
        return []


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


def canary_info():
    """Маскировка точек входа по данным канарейки: {ip: {"ok": bool, "ts": ts}}."""
    try:
        st = json.load(open("/etc/xvpn/canary.json", encoding="utf-8"))
        return {"mask": st.get("mask") or {}, "ts": st.get("ts", 0)}
    except FileNotFoundError:
        return None


@app.get(P + "/api/keys")
def api_keys(u=Depends(tg_user)):
    m = exit_map_get()
    names = {s["id"]: s["name"] for s in q("SELECT id, name FROM servers WHERE role='exit'")}
    out = []
    for k in q("SELECT * FROM keys ORDER BY id DESC"):
        ko = key_out(k)
        ko["exit"] = m.get(k["id"])
        ko["exit_name"] = names.get(ko["exit"]) if ko["exit"] else None
        out.append(ko)
    return out


@app.post(P + "/api/keys/{kid}/exit")
async def api_key_exit(kid: int, body: dict, u=Depends(tg_user)):
    """Принудительная смена выхода каскада для ключа (exit=null — автоназначение)."""
    if not q("SELECT 1 AS x FROM keys WHERE id=?", (kid,), one=True):
        raise HTTPException(404, "нет такого ключа")
    sid = body.get("exit")
    m = exit_map_get()
    pins = exit_pins_get()
    if sid is None:
        m.pop(kid, None)
        pins.discard(kid)
    else:
        ok = q("SELECT 1 AS x FROM servers WHERE id=? AND role='exit' AND cascade=1 AND enabled=1",
               (int(sid),), one=True)
        if not ok:
            raise HTTPException(400, "такого активного выхода каскада нет")
        m[kid] = int(sid)
        pins.add(kid)
    set_setting("exit_map", json.dumps(m))
    set_setting("exit_pins", json.dumps(sorted(pins)))
    await sync_now()
    return {"ok": True, "exit": sid}


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
    ko = key_out(k)
    return {"sub_url": ko["sub_url"], "sub_url_ru": ko["sub_url_ru"], "links": key_links(k)}


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
    c = cluster_conf()
    return {"cdn": cdn_on(), "cluster": bool(c), "cluster_host": c["host"] if c else "",
            "emergency": setting("emergency", "0") == "1"}


@app.patch(P + "/api/settings")
async def api_settings_edit(body: dict, u=Depends(owner)):
    if "cdn" in body:
        set_setting("cdn", "1" if body["cdn"] else "0")
    if "emergency" in body:
        set_setting("emergency", "1" if body["emergency"] else "0")
    if "sub_host" in body:
        set_setting("sub_host", str(body["sub_host"]).strip().lower())
    if "mobile_bridge" in body:
        set_setting("mobile_bridge", str(body["mobile_bridge"]))
    if "site_bridge_keys" in body:
        set_setting("site_bridge_keys", str(body["site_bridge_keys"]))
    if "exit_domains" in body:
        set_setting("exit_domains", json.dumps([str(d) for d in body["exit_domains"]]))
        asyncio.create_task(sync_now())
    if "cluster" in body:
        if body["cluster"]:
            host = str(body.get("cluster_host") or setting("cluster_host", ""))
            cluster_enable(host)
        else:
            set_setting("cluster", "0")
        await sync_now()  # мосты принимают общие ключи или возвращают свои
    c = cluster_conf()
    return {"cdn": cdn_on(), "cluster": bool(c), "cluster_host": c["host"] if c else "",
            "emergency": setting("emergency", "0") == "1"}


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
