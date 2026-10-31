"""xvpn node agent: управляет локальным Xray по командам панели.

Эндпоинты (все требуют заголовок Authorization: Bearer <secret>):
  GET  /info     - параметры подключения ноды (домен, путь, ключи Reality)
  POST /sync     - список пользователей {"users":[{"email":..,"uuid":..}]}
  GET  /stats    - трафик пользователей с момента прошлого запроса (со сбросом)
  GET  /status   - CPU/RAM/диск/сеть/аптайм/состояние Xray
  POST /restart  - перезапуск Xray
"""
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
XRAY_BIN = os.environ.get("XRAY_BIN", "/usr/local/bin/xray")
XRAY_CONFIG = os.environ.get("XRAY_CONFIG", "/usr/local/etc/xray/config.json")
XRAY_SERVICE = os.environ.get("XRAY_SERVICE", "xray")
API_ADDR = "127.0.0.1:10085"
XHTTP_SOCK = "/dev/shm/xvpn-xhttp.sock"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def load_node() -> dict:
    with open(NODE_FILE) as f:
        return json.load(f)


def auth(authorization: str = Header(default="")):
    secret = load_node()["agent_secret"]
    if not hmac.compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
        raise HTTPException(401, "unauthorized")


def build_config(node: dict, users: list[dict]) -> dict:
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


def apply_users(users: list[dict]) -> bool:
    """Пишет конфиг и перезапускает Xray, только если что-то поменялось."""
    users = sorted(({"email": u["email"], "uuid": u["uuid"]} for u in users), key=lambda u: u["email"])
    cfg = build_config(load_node(), users)
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
    return {
        "domain": n["domain"], "ip": n["ip"], "xhttp_path": n["xhttp_path"],
        "reality": {"public_key": r["public_key"], "short_id": r["short_id"],
                    "sni_tcp": r["sni_tcp"], "sni_xhttp": r["sni_xhttp"],
                    "xhttp_path": r["xhttp_path"]},
    }


@app.post("/sync", dependencies=[Depends(auth)])
def sync(body: dict):
    changed = apply_users(body.get("users", []))
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
    return {
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
