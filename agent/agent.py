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
           остальное через выходы (несколько — балансировка leastPing с проверкой каждые 30 с)
"""
import urllib.request
import hashlib
import hmac
import json
import os
import subprocess
import time

import psutil
from fastapi import Depends, FastAPI, Header, HTTPException

BASE = os.environ.get("XVPN_AGENT_DIR", "/opt/xvpn/agent")
NODE_FILE = os.path.join(BASE, "node.json")
USERS_FILE = os.path.join(BASE, "users.json")
UPSTREAMS_FILE = os.path.join(BASE, "upstreams.json")
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

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def load_node() -> dict:
    with open(NODE_FILE) as f:
        return json.load(f)


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


def build_bridge(node: dict, users: list[dict], upstreams: list[dict]) -> dict:
    r = node["reality"]
    vision = [{"id": u["uuid"], "email": u["email"], "flow": "xtls-rprx-vision"} for u in users]
    exits = []
    for u in upstreams:
        o = json.loads(json.dumps(u["outbound"]))
        o["tag"] = u["tag"]
        exits.append(o)
    last = {"type": "field", "network": "tcp,udp"}
    if len(exits) > 1:
        last["balancerTag"] = "exits"
    elif exits:
        last["outboundTag"] = exits[0]["tag"]
    else:
        last["outboundTag"] = "block"  # выходов нет — за рубеж не пускаем, РФ-сайты работают
    cfg = _common(node) | {
        "inbounds": [
            {"tag": "api", "listen": "127.0.0.1", "port": 10085,
             "protocol": "dokodemo-door", "settings": {"address": "127.0.0.1"}},
            {"tag": "bridge-in", "listen": "0.0.0.0", "port": int(node.get("port", 443)), "protocol": "vless",
             "settings": {"clients": vision, "decryption": "none"},
             "streamSettings": {"network": "tcp", "security": "reality",
                                "realitySettings": {"dest": f"{r['sni_tcp']}:443",
                                                    "serverNames": [r["sni_tcp"]],
                                                    "privateKey": r["private_key"],
                                                    "shortIds": [r["short_id"]]}},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}},
        ],
        "outbounds": exits + [{"tag": "direct", "protocol": "freedom"},
                              {"tag": "block", "protocol": "blackhole"}],
        "routing": {"domainStrategy": "IPIfNonMatch", "rules": [
            {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
            {"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
            {"type": "field", "domain": RU_DOMAINS, "outboundTag": "direct"},
            {"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"},
            last,
        ]},
    }
    if exits:
        cfg["metrics"] = {"tag": "metrics", "listen": METRICS_ADDR}
        cfg["observatory"] = {"subjectSelector": ["exit-"], "probeURL": PROBE_URL,
                              "probeInterval": "30s", "enableConcurrency": True}
    if len(exits) > 1:
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
    return {
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


def run(cmd: list[str], timeout=20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def load_upstreams() -> list[dict]:
    if os.path.exists(UPSTREAMS_FILE):
        with open(UPSTREAMS_FILE) as f:
            return json.load(f)
    return []


def apply_users(users: list[dict], upstreams: list[dict] | None = None) -> bool:
    """Пишет конфиг и перезапускает Xray, только если что-то поменялось."""
    users = sorted(({"email": u["email"], "uuid": u["uuid"]} for u in users), key=lambda u: u["email"])
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
        return {"role": "bridge", "ip": n["ip"], "port": int(n.get("port", 443)),
                "reality": {"public_key": r["public_key"], "short_id": r["short_id"], "sni_tcp": r["sni_tcp"]}}
    return {
        "role": "exit",
        "domain": n["domain"], "ip": n["ip"], "xhttp_path": n["xhttp_path"],
        "reality": {"public_key": r["public_key"], "short_id": r["short_id"],
                    "sni_tcp": r["sni_tcp"], "sni_xhttp": r["sni_xhttp"],
                    "xhttp_path": r["xhttp_path"]},
    }


@app.post("/sync", dependencies=[Depends(auth)])
def sync(body: dict):
    changed = apply_users(body.get("users", []), body.get("upstreams"))
    return {"ok": True, "changed": changed}


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
    return {"users": out}


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


@app.get("/status", dependencies=[Depends(auth)])
def status():
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    net = psutil.net_io_counters()
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
    return extra | {
        "cpu": psutil.cpu_percent(interval=0.3), "cores": psutil.cpu_count(),
        "load": os.getloadavg(),
        "ram": {"used": vm.total - vm.available, "total": vm.total},
        "disk": {"used": du.used, "total": du.total},
        "net": {"sent": net.bytes_sent, "recv": net.bytes_recv},
        "uptime": int(time.time() - psutil.boot_time()),
        "xray": {"active": active == "active", "version": ver, "users": len(users)},
    }


@app.post("/restart", dependencies=[Depends(auth)])
def restart():
    p = run(["systemctl", "restart", XRAY_SERVICE])
    return {"ok": p.returncode == 0}
