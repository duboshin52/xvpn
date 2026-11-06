"""xvpn node agent: управляет локальным Xray по командам панели.

Эндпоинты (все требуют заголовок Authorization: Bearer <secret>):
  GET  /info     - параметры подключения ноды (домен, путь, ключи Reality)
  POST /sync     - список пользователей {"users":[{"email":..,"uuid":..}]}
                   мосту дополнительно {"upstreams":[{"tag":"exit-…","outbound":{…}}]}
  GET  /stats    - трафик пользователей с момента прошлого запроса (со сбросом)
  GET  /status   - CPU/RAM/диск/сеть/аптайм/состояние Xray; у моста ещё живость выходов
  POST /restart  - перезапуск Xray

Роли ноды (node.json → role):
  exit   - обычная нода: XHTTP через Cloudflare + Reality + XHTTP-Reality, трафик выходит в интернет
  bridge - мост каскада (РФ): VLESS+Reality+Vision для клиентов, российские сайты напрямую,
           остальное через выходы (несколько — пользователи закреплены за выходами панелью
           по нагрузке, внутри выхода leastPing, при падении — перекрёстный резерв; проверка каждые 30 с)
"""
import concurrent.futures
import contextlib
import urllib.request
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import parse_qsl, unquote, urlsplit

import psutil
from fastapi import Depends, FastAPI, Header, HTTPException

BASE = os.environ.get("XVPN_AGENT_DIR", "/opt/xvpn/agent")
NODE_FILE = os.path.join(BASE, "node.json")
USERS_FILE = os.path.join(BASE, "users.json")
UPSTREAMS_FILE = os.path.join(BASE, "upstreams.json")
EXIT_DOMAINS_FILE = os.path.join(BASE, "exit_domains.json")
XRAY_BIN = os.environ.get("XRAY_BIN", "/usr/local/bin/xray")
XRAY_CONFIG = os.environ.get("XRAY_CONFIG", "/usr/local/etc/xray/config.json")
XRAY_SERVICE = os.environ.get("XRAY_SERVICE", "xray")
API_ADDR = "127.0.0.1:10085"
METRICS_ADDR = "127.0.0.1:10086"
PROBE_URL = "https://www.gstatic.com/generate_204"
RU_DOMAINS = [r"regexp:\.ru$", r"regexp:\.su$", r"regexp:\.xn--p1ai$",
              "domain:yandex.net", "domain:yandex.com", "domain:yastatic.net", "domain:ya.ru",
              "domain:vk.com", "domain:vk.me", "domain:userapi.com", "domain:vkuser.net",
              "domain:mycdn.me", "domain:okcdn.ru", "domain:max.ru"]
XHTTP_SOCK = "/dev/shm/xvpn-xhttp.sock"
WATCHDOG_FILE = os.path.join(BASE, "watchdog.json")
WATCH_EVERY = int(os.environ.get("XVPN_WATCH_EVERY", 20))  # проверка входа моста, секунд
WATCH_LIMIT = int(os.environ.get("XVPN_WATCH_LIMIT", 3))   # висящих рукопожатий — перезапуск Xray
WATCH_GAP = int(os.environ.get("XVPN_WATCH_GAP", 900))     # перезапуск не чаще, секунд

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def load_node() -> dict:
    with open(NODE_FILE) as f:
        return json.load(f)


def save_node(node: dict) -> None:
    with open(NODE_FILE, "w") as f:
        json.dump(node, f, indent=2, ensure_ascii=False)


def auth(authorization: str = Header(default="")):
    secret = load_node()["agent_secret"]
    if not hmac.compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
        raise HTTPException(401, "unauthorized")


def role(node: dict) -> str:
    return node.get("role", "exit")


def _common(node: dict) -> dict:
    return {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "services": ["StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True, "handshake": 4, "connIdle": 300}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
    }


def load_exit_domains() -> list[str]:
    if os.path.exists(EXIT_DOMAINS_FILE):
        with open(EXIT_DOMAINS_FILE) as f:
            return json.load(f)
    return []


def build_bridge(node: dict, users: list[dict], upstreams: list[dict]) -> dict:
    r = node["reality"]
    exit_domains = load_exit_domains()
    vision = [{"id": u["uuid"], "email": u["email"], "flow": "xtls-rprx-vision"} for u in users]
    exits = []
    by_srv: dict[str, list[str]] = {}  # теги exit-<id>-x|t, сгруппированные по серверу
    for u in upstreams:
        o = json.loads(json.dumps(u["outbound"]))
        o["tag"] = u["tag"]
        exits.append(o)
        sid = u["tag"].split("-")[1] if u["tag"].count("-") >= 2 else u["tag"]
        by_srv.setdefault(sid, []).append(u["tag"])
    srv_ids = sorted(by_srv, key=lambda s: int(s) if s.isdigit() else 0)
    nsrv = len(srv_ids)
    last = {"type": "field", "network": "tcp,udp"}
    if not exits:
        last["outboundTag"] = "block"  # выходов нет — за рубеж не пускаем, РФ-сайты работают
    elif nsrv == 1:
        if len(exits) > 1:
            last["balancerTag"] = "exits"
        else:
            last["outboundTag"] = exits[0]["tag"]
    else:
        last["balancerTag"] = f"exits-{srv_ids[0]}"
    forced = []  # заблокированные сайты в .ru: только через выход, раньше правила «РФ напрямую»
    if exit_domains and exits:
        forced = [{"type": "field", "domain": exit_domains,
                   **{k: v for k, v in last.items() if k in ("outboundTag", "balancerTag")}}]
    # распределение пользователей по выходам: панель закрепляет каждый ключ за своим
    # сервером (по реальной нагрузке, липко); без привязки — по номеру ключа,
    # при падении сервера группа уходит на соседний
    user_rules = []
    if nsrv > 1:
        groups: dict[str, list[str]] = {sid: [] for sid in srv_ids}
        for u in users:
            ex = str(u.get("exit") or "")
            if ex in groups:
                groups[ex].append(u["email"])
            else:
                m = re.search(r"\d+", u.get("email") or "")
                groups[srv_ids[int(m.group()) % nsrv if m else 0]].append(u["email"])
        for sid in srv_ids:
            if groups[sid]:
                user_rules.append({"type": "field", "user": groups[sid], "balancerTag": f"exits-{sid}"})
    cfg = _common(node) | {
        "inbounds": [
            {"tag": "api", "listen": "127.0.0.1", "port": 10085,
             "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"}},
            {"tag": "bridge-in", "listen": "0.0.0.0", "port": int(node.get("port", 443)), "protocol": "vless",
             "settings": {"clients": vision, "decryption": "none"},
             "streamSettings": {"network": "tcp", "security": "reality",
                                # dest — свой сайт на этом же сервере, если мост маскируется под него
                                "realitySettings": {"dest": r.get("dest") or f"{r['sni_tcp']}:443",
                                                    "serverNames": [r["sni_tcp"]],
                                                    "privateKey": r["private_key"],
                                                    "shortIds": [r["short_id"]]}},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}},
        ],
        "outbounds": exits + [{"tag": "direct", "protocol": "freedom"},
                              {"tag": "block", "protocol": "blackhole"}],
        # AsIs: мост не резолвит чужие домены, иначе DNS хостера в РФ видит их все,
        # а подменённый ответ на заблокированный сайт уводит его в «РФ напрямую»
        "routing": {"domainStrategy": "AsIs", "rules": [
            {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
            {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
            *forced,
            {"type": "field", "domain": RU_DOMAINS, "outboundTag": "direct"},
            {"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"},
            *user_rules,
            last,
        ]},
    }
    fwd = node.get("main_fwd") or {}
    if fwd.get("port") and fwd.get("target") and exits:
        # прокси перед главным (Caddy на этом мосту) ходит к нему через туннель к выходу:
        # прямое TLS из РФ на зарубежный хостинг замерзает после ~20 КБ ответа.
        # Порт только для локальных контейнеров: снаружи — блок, мост не светит связь с главным
        host, _, port = str(fwd["target"]).rpartition(":")
        cfg["inbounds"].append(
            {"tag": "main-fwd", "listen": fwd.get("listen", "0.0.0.0"), "port": int(fwd["port"]),
             "protocol": "dokodemo-door", "settings": {"address": host, "port": int(port), "network": "tcp"}})
        cfg["routing"]["rules"][1:1] = [
            {"type": "field", "inboundTag": ["main-fwd"], "source": ["127.0.0.0/8", "172.16.0.0/12"],
             **{k: v for k, v in last.items() if k in ("outboundTag", "balancerTag")}},
            {"type": "field", "inboundTag": ["main-fwd"], "outboundTag": "block"}]
    if node.get("ru_cdn") and node.get("xhttp_path"):
        # вход через российский CDN: клиент → CDN → nginx моста → этот сокет → выходы.
        # Для сети клиента мост не виден: оператор видит только адрес CDN
        cfg["inbounds"].append(
            {"tag": "bridge-cdn", "listen": f"{XHTTP_SOCK},0666", "protocol": "vless",
             "settings": {"clients": [{"id": u["uuid"], "email": u["email"]} for u in users], "decryption": "none"},
             "streamSettings": {"network": "xhttp",
                                "xhttpSettings": {"path": node["xhttp_path"], "mode": "auto",
                                                  "uplinkDataPlacement": "auto"}},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}})
    if exits:
        cfg["metrics"] = {"tag": "metrics", "listen": METRICS_ADDR}
        cfg["observatory"] = {"subjectSelector": ["exit-"], "probeURL": PROBE_URL,
                              "probeInterval": "30s", "enableConcurrency": True}
    if nsrv > 1:
        # по балансировщику на сервер + перекрёстный резерв по кольцу
        cfg["routing"]["balancers"] = [
            {"tag": f"exits-{sid}", "selector": [f"exit-{sid}-"],
             "strategy": {"type": "leastPing"},
             "fallbackTag": by_srv[srv_ids[(i + 1) % nsrv]][0]}
            for i, sid in enumerate(srv_ids)]
    elif len(exits) > 1:
        cfg["routing"]["balancers"] = [{"tag": "exits", "selector": ["exit-"],
                                        "strategy": {"type": "leastPing"},
                                        "fallbackTag": exits[0]["tag"]}]
    return cfg


def build_config(node: dict, users: list[dict], upstreams: list[dict] | None = None) -> dict:
    if role(node) == "bridge":
        return build_bridge(node, users, upstreams or [])
    r = node["reality"]
    plain = [{"id": u["uuid"], "email": u["email"]} for u in users]
    vision = [{"id": u["uuid"], "email": u["email"], "flow": "xtls-rprx-vision"} for u in users]
    sniff = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
    cfg = {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "services": ["StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True, "handshake": 4, "connIdle": 300}},
            "system": {"statsInboundUplink": True, "statsInboundDownlink": True},
        },
        "inbounds": [
            {"tag": "api", "listen": "127.0.0.1", "port": 10085,
             "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"}},
            # 1) VLESS + XHTTP за nginx/Cloudflare (TLS терминирует nginx)
            {"tag": "xhttp-cdn", "listen": f"{XHTTP_SOCK},0666", "protocol": "vless",
             "settings": {"clients": plain, "decryption": "none"},
             "streamSettings": {"network": "xhttp",
                                "xhttpSettings": {"path": node["xhttp_path"], "mode": "auto"}},
             "sniffing": sniff},
            # 2) VLESS + Reality + Vision (напрямую, резерв)
            {"tag": "reality-tcp", "listen": "127.0.0.1", "port": 4431, "protocol": "vless",
             "settings": {"clients": vision, "decryption": "none"},
             "streamSettings": {"network": "tcp", "security": "reality",
                                "realitySettings": {"dest": f"{r['sni_tcp']}:443",
                                                    "serverNames": [r["sni_tcp"]],
                                                    "privateKey": r["private_key"],
                                                    "shortIds": [r["short_id"]]}},
             "sniffing": sniff},
            # 3) VLESS + XHTTP + Reality (напрямую)
            {"tag": "reality-xhttp", "listen": "127.0.0.1", "port": 4432, "protocol": "vless",
             "settings": {"clients": plain, "decryption": "none"},
             "streamSettings": {"network": "xhttp", "security": "reality",
                                "xhttpSettings": {"path": r["xhttp_path"], "mode": "auto"},
                                "realitySettings": {"dest": f"{r['sni_xhttp']}:443",
                                                    "serverNames": [r["sni_xhttp"]],
                                                    "privateKey": r["private_key"],
                                                    "shortIds": [r["short_id"]]}},
             "sniffing": sniff},
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"},
                      {"tag": "block", "protocol": "blackhole"}],
        "routing": {"domainStrategy": "AsIs", "rules": [
            {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
            {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
        ]},
    }
    if not node.get("domain") and not node.get("ru_cdn"):
        # нода без домена: только Reality и XHTTP-Reality (nginx разводит их по SNI на 443)
        cfg["inbounds"] = [i for i in cfg["inbounds"] if i["tag"] != "xhttp-cdn"]
    if node.get("ru_cdn"):
        # российские CDN режут POST: клиенты шлют данные GET-запросами в заголовках,
        # сервер принимает данные из заголовков, cookie и тела
        for i in cfg["inbounds"]:
            if i["tag"] == "xhttp-cdn":
                i["streamSettings"]["xhttpSettings"]["uplinkDataPlacement"] = "auto"
    w = node.get("warp")
    if w:
        # IPv6-only сервер: IPv4-сайты идут через Cloudflare WARP
        cfg["outbounds"][0]["settings"] = {"domainStrategy": "UseIPv6"}
        cfg["outbounds"].insert(1, {"tag": "warp", "protocol": "wireguard", "settings": {
            "secretKey": w["secret_key"], "address": w["address"], "mtu": 1280, "noKernelTun": True,
            "peers": [{"publicKey": w["public_key"], "endpoint": w["endpoint"],
                       "allowedIPs": ["0.0.0.0/0", "::/0"]}]}})
        cfg["routing"]["domainStrategy"] = "IPIfNonMatch"
        cfg["routing"]["rules"].append({"type": "field", "ip": ["0.0.0.0/0"], "outboundTag": "warp"})
    return cfg


def run(cmd: list[str], timeout=20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def load_upstreams() -> list[dict]:
    if os.path.exists(UPSTREAMS_FILE):
        with open(UPSTREAMS_FILE) as f:
            return json.load(f)
    return []


def apply_users(users: list[dict], upstreams: list[dict] | None = None) -> bool:
    """Пишет конфиг и перезапускает Xray, только если что-то поменялось."""
    users = sorted(({"email": u["email"], "uuid": u["uuid"], "exit": u.get("exit")} for u in users),
                   key=lambda u: u["email"])
    node = load_node()
    if role(node) == "bridge":
        if upstreams is None:
            upstreams = load_upstreams()
        upstreams = [u for u in upstreams if str(u.get("tag", "")).startswith("exit-") and u.get("outbound")]
        with open(UPSTREAMS_FILE, "w") as f:
            json.dump(upstreams, f)
    cfg = build_config(node, users, upstreams)
    data = json.dumps(cfg, indent=2, ensure_ascii=False)
    old = ""
    if os.path.exists(XRAY_CONFIG):
        with open(XRAY_CONFIG) as f:
            old = f.read()
    if hashlib.sha256(old.encode()).digest() == hashlib.sha256(data.encode()).digest():
        return False
    tmp = XRAY_CONFIG[:-5] + ".new.json" if XRAY_CONFIG.endswith(".json") else XRAY_CONFIG + ".new.json"
    with open(tmp, "w") as f:
        f.write(data)
    t = run([XRAY_BIN, "run", "-test", "-c", tmp])
    if t.returncode != 0:
        raise HTTPException(500, f"xray config test failed: {t.stdout[-500:]}{t.stderr[-500:]}")
    os.replace(tmp, XRAY_CONFIG)
    with open(USERS_FILE, "w") as f:
        json.dump(users, f)
    run(["systemctl", "restart", XRAY_SERVICE])
    return True


@app.get("/info", dependencies=[Depends(auth)])
def info():
    n = load_node()
    r = n["reality"]
    if role(n) == "bridge":
        return {"role": "bridge", "name": n.get("name", ""), "ip": n["ip"], "port": int(n.get("port", 443)),
                "ru_cdn": n.get("ru_cdn"), "xhttp_path": n.get("xhttp_path"),
                "standalone": bool(n.get("standalone")),
                "reality": {"public_key": r["public_key"], "short_id": r["short_id"], "sni_tcp": r["sni_tcp"]}}
    return {
        "role": "exit", "name": n.get("name", ""), "ru_cdn": n.get("ru_cdn"),
        "domain": n.get("domain") or None, "ip": n["ip"], "xhttp_path": n["xhttp_path"],
        "reality": {"public_key": r["public_key"], "short_id": r["short_id"],
                    "sni_tcp": r["sni_tcp"], "sni_xhttp": r["sni_xhttp"],
                    "xhttp_path": r["xhttp_path"]},
    }


def apply_cluster(cluster: dict | None) -> bool:
    """Кластер мостов: общая Reality-идентичность (одна ссылка на все мосты).

    cluster=dict — принять кластерные ключи (свои сохраняем в reality_own);
    cluster=None — вернуть собственные ключи. Возвращает True, если ключи сменились."""
    node = load_node()
    if role(node) != "bridge" or node.get("standalone"):  # отдельный мост живёт со своими ключами
        return False
    if cluster:
        new = {"private_key": cluster["private_key"], "public_key": cluster["public_key"],
               "short_id": cluster["short_id"], "sni_tcp": cluster["sni_tcp"]}
        cur = node["reality"]
        if all(cur.get(k) == v for k, v in new.items()):
            return False
        if "reality_own" not in node:
            node["reality_own"] = dict(cur)
        node["reality"] = cur | new
        save_node(node)
        return True
    if "reality_own" in node:
        node["reality"] = node.pop("reality_own")
        save_node(node)
        return True
    return False


@app.post("/sync", dependencies=[Depends(auth)])
def sync(body: dict):
    keys_changed = False
    if "cluster" in body:
        keys_changed = apply_cluster(body["cluster"])
    if "exit_domains" in body:
        with open(EXIT_DOMAINS_FILE, "w") as f:
            json.dump(body["exit_domains"], f)
    changed = apply_users(body.get("users", []), body.get("upstreams"))
    return {"ok": True, "changed": changed or keys_changed}


def inbound_stats() -> dict[str, dict]:
    """Трафик по входам Xray с прошлого запроса (со сбросом): так видно долю CDN и каждого входа."""
    p = run([XRAY_BIN, "api", "statsquery", f"--server={API_ADDR}", "-pattern", "inbound>>>", "-reset"])
    out: dict[str, dict] = {}
    if p.returncode != 0:
        return out
    for s in (json.loads(p.stdout or "{}").get("stat") or []):
        parts = s["name"].split(">>>")  # inbound>>>tag>>>traffic>>>uplink
        if len(parts) == 4 and parts[1] != "api":
            d = out.setdefault(parts[1], {"up": 0, "down": 0})
            d["up" if parts[3] == "uplink" else "down"] += int(s.get("value", 0))
    return out


@app.get("/stats", dependencies=[Depends(auth)])
def stats():
    p = run([XRAY_BIN, "api", "statsquery", f"--server={API_ADDR}", "-pattern", "user>>>", "-reset"])
    if p.returncode != 0:
        raise HTTPException(502, "xray api unavailable")
    out: dict[str, dict] = {}
    for s in (json.loads(p.stdout or "{}").get("stat") or []):
        parts = s["name"].split(">>>")  # user>>>email>>>traffic>>>uplink
        if len(parts) != 4:
            continue
        d = out.setdefault(parts[1], {"up": 0, "down": 0})
        d["up" if parts[3] == "uplink" else "down"] += int(s.get("value", 0))
    return {"users": out, "inbounds": inbound_stats()}


def exits_health() -> list[dict]:
    """Живость выходов моста по данным observatory (Xray metrics)."""
    ups = load_upstreams()
    obs = {}
    try:
        with urllib.request.urlopen(f"http://{METRICS_ADDR}/debug/vars", timeout=3) as r:
            obs = json.load(r).get("observatory") or {}
    except Exception:
        pass
    out = []
    for u in ups:
        o = obs.get(u["tag"]) or {}
        alive = bool(o.get("alive"))
        out.append({"tag": u["tag"], "alive": alive,
                    "delay": int(o["delay"]) if alive and o.get("delay") else None,
                    "checked": int(o.get("last_try_time") or 0)})
    return out


def stalled_handshakes(port: int, newest: int = 300) -> int:
    """Клиенты на входе, которые прислали рукопожатие и не получили ни байта.

    Reality ждёт ответ маскировочного сайта: если тот молчит, клиент висит
    «подключённым» без интернета. Берём только свежие (10 с — newest с): старые
    зависшие соединения держатся часами и о текущем сбое не говорят."""
    out = run(["ss", "-tinH", "state", "established", f"( sport = :{port} )"]).stdout
    n = 0
    for line in out.splitlines():
        if "bytes_received:" in line and "bytes_sent:" not in line:
            m = re.search(r"lastrcv:(\d+)", line)
            if m and 10000 <= int(m.group(1)) <= newest * 1000:
                n += 1
    return n


def load_watchdog() -> dict:
    try:
        with open(WATCHDOG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def watchdog() -> None:
    """Сторож моста: Xray иногда перестаёт отвечать на рукопожатия части клиентов.

    Канарейке с её адресов это не видно, а у клиента «подключено» без интернета;
    перезапуск снимает. Здоровый Reality отвечает за миллисекунды, поэтому
    WATCH_LIMIT висящих рукопожатий две проверки подряд — повод перезапустить."""
    strikes = 0
    while True:
        time.sleep(WATCH_EVERY)
        try:
            node = load_node()
            if role(node) != "bridge":
                return
            n = stalled_handshakes(int(node.get("port", 443)), newest=600)
            strikes = strikes + 1 if n >= WATCH_LIMIT else 0
            last = load_watchdog()
            if strikes < 2 or time.time() - last.get("t", 0) < WATCH_GAP:
                continue
            ok = run(["systemctl", "restart", XRAY_SERVICE]).returncode == 0
            rec = {"t": int(time.time()), "stalled": n, "ok": ok, "count": last.get("count", 0) + 1}
            with open(WATCHDOG_FILE + ".tmp", "w") as f:
                json.dump(rec, f)
            os.replace(WATCHDOG_FILE + ".tmp", WATCHDOG_FILE)
            print(f"watchdog: {n} рукопожатий без ответа, перезапуск Xray: {ok}", flush=True)
            strikes = 0
        except Exception as e:
            print(f"watchdog: {e}", flush=True)


threading.Thread(target=watchdog, daemon=True).start()


@app.get("/status", dependencies=[Depends(auth)])
def status():
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    # только внешние интерфейсы: на выходах весь трафик ещё раз идёт через lo (nginx ↔ Xray)
    nics = [n for name, n in psutil.net_io_counters(pernic=True).items() if name != "lo"]
    active = run(["systemctl", "is-active", XRAY_SERVICE]).stdout.strip()
    ver = run([XRAY_BIN, "version"]).stdout.split("\n")[0]
    users = []
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            users = json.load(f)
    node = load_node()
    extra = {"role": role(node)}
    if role(node) == "bridge":
        extra["exits"] = exits_health()
        extra["stalled"] = stalled_handshakes(int(node.get("port", 443)))
        extra["watchdog"] = load_watchdog()
        # маскировка под свой сайт на этом сервере: сертификат может быть самодельным
        extra["local_mask"] = str((node.get("reality") or {}).get("dest", "")).startswith("127.")
    return extra | {
        "cpu": psutil.cpu_percent(interval=0.3), "cores": psutil.cpu_count(),
        "load": os.getloadavg(),
        "ram": {"used": vm.total - vm.available, "total": vm.total},
        "disk": {"used": du.used, "total": du.total},
        "net": {"sent": sum(n.bytes_sent for n in nics), "recv": sum(n.bytes_recv for n in nics)},
        "uptime": int(time.time() - psutil.boot_time()),
        "xray": {"active": active == "active", "version": ver, "users": len(users)},
    }


@app.post("/restart", dependencies=[Depends(auth)])
def restart():
    p = run(["systemctl", "restart", XRAY_SERVICE])
    return {"ok": p.returncode == 0}


# ---------------------------------------------------------------- проба входов из этой сети
PROBE_SMALL = "https://www.gstatic.com/generate_204"
PROBE_BIG = "https://speed.cloudflare.com/__down?bytes=300000"  # больше ~20 КБ: ловит «замерзание» TLS


def probe_outbound(link: str) -> dict | None:
    """vless://…?security=reality&type=tcp → outbound Xray; другие виды ссылок не проверяем."""
    u = urlsplit(link)
    p = dict(parse_qsl(u.query))
    if u.scheme != "vless" or p.get("security") != "reality" or p.get("type", "tcp") != "tcp":
        return None
    user = {"id": unquote(u.username or ""), "encryption": "none"}
    if p.get("flow"):
        user["flow"] = p["flow"]
    return {"protocol": "vless",
            "settings": {"vnext": [{"address": u.hostname, "port": u.port or 443, "users": [user]}]},
            "streamSettings": {"network": "tcp", "security": "reality",
                               "realitySettings": {"serverName": p.get("sni", ""), "publicKey": p.get("pbk", ""),
                                                   "shortId": p.get("sid", ""),
                                                   "fingerprint": p.get("fp", "chrome")}}}


def probe_one(link: str, port: int) -> dict:
    name = link.split("#", 1)[1] if "#" in link else ""
    name = unquote(name).split(" | ")[0]
    ob = probe_outbound(link)
    if not ob:
        return {"name": name, "ok": None, "err": "вид ссылки не проверяется"}
    cfg = {"log": {"loglevel": "none"},
           "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"udp": False}}],
           "outbounds": [ob]}
    path = f"/dev/shm/xvpn-probe-{port}.json"
    with open(path, "w") as f:
        json.dump(cfg, f)
    proc = subprocess.Popen([XRAY_BIN, "run", "-c", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1)
        out = {"name": name, "host": urlsplit(link).hostname}
        px = f"socks5h://127.0.0.1:{port}"
        r = run(["curl", "-s", "-o", "/dev/null", "-x", px, "--max-time", "10",
                 "-w", "%{http_code} %{time_total}", PROBE_SMALL], timeout=15).stdout.split()
        if not r or r[0] != "204":
            return out | {"ok": False, "err": f"нет связи (код {r[0] if r else '—'})"}
        out["ms"] = int(float(r[1]) * 1000)
        r = run(["curl", "-s", "-o", "/dev/null", "-x", px, "--max-time", "20",
                 "-w", "%{size_download} %{time_total}", PROBE_BIG], timeout=25).stdout.split()
        got = int(r[0]) if r else 0
        if got < 300000:
            return out | {"ok": False, "err": f"скачалось {got // 1024} КБ из 293"}
        return out | {"ok": True, "kbps": int(got / 1024 / max(float(r[1]), 0.001))}
    except Exception as e:
        return {"name": name, "ok": False, "err": f"{type(e).__name__} {e}"}
    finally:
        proc.kill()
        with contextlib.suppress(OSError):
            os.remove(path)


@app.post("/probe", dependencies=[Depends(auth)])
def probe(body: dict):
    """Проверить ссылки подписки из сети этого сервера: связь и скачивание 300 КБ через каждую."""
    links = [str(l) for l in body.get("links", [])][:12]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda a: probe_one(a[1], 20800 + a[0]), enumerate(links)))
    return {"results": res}
