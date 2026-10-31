#!/usr/bin/env bash
# xvpn cascade — каскад «клиент → российский сервер → зарубежный сервер → интернет».
#   bash cascade.sh exit        — выходной сервер (Европа): VLESS+XHTTP+Reality, печатает ссылку для моста
#   bash cascade.sh bridge      — входной сервер (Россия): принимает клиентов, РФ-сайты отдаёт напрямую,
#                                 остальное — через выходной сервер
#   bash cascade.sh exit-add ССЫЛКА [ИМЯ] — (bridge) добавить ещё один выходной сервер (резерв)
#   bash cascade.sh exit-del ИМЯ — (bridge) убрать выходной сервер
#   bash cascade.sh check       — (bridge) проверить каждый выходной сервер
#   bash cascade.sh add ИМЯ     — (bridge) добавить клиента и показать его ссылку
#   bash cascade.sh del ИМЯ     — (bridge) удалить клиента
#   bash cascade.sh links       — показать ссылки ещё раз
#   bash cascade.sh update      — обновить Xray и пересобрать конфиг
# Переменные (чтобы не отвечать на вопросы): UPSTREAM (ссылки vless:// выходных серверов через пробел), SNI, PORT, CLIENT.
# Несколько выходов: мост сам проверяет их каждые 30 с и ведёт трафик через самый быстрый живой (leastPing).
set -Eeuo pipefail
trap 'printf "\033[1;31m✘ Ошибка в строке %s: %s\033[0m\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

DIR=/etc/xvpn-cascade
STATE=$DIR/state.json
XRAY=/usr/local/bin/xray
CONF=/usr/local/etc/xray/config.json
MODE="${1:-}"
ARG="${2:-}"

c() { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
ask() { # ask VAR "вопрос" [default]
  local var=$1 q=$2 def=${3:-} val
  if [[ -n "${!var:-}" ]]; then return; fi
  read -rp "$q${def:+ [$def]}: " val </dev/tty
  printf -v "$var" '%s' "${val:-$def}"
  [[ -n "${!var}" ]] || die "$var обязателен"
}

[[ $EUID -eq 0 ]] || die "Запустите от root (sudo -i)"
[[ "$MODE" =~ ^(exit|bridge|exit-add|exit-del|check|add|del|links|update)$ ]] \
  || die "Использование: bash cascade.sh exit|bridge|exit-add ССЫЛКА [ИМЯ]|exit-del ИМЯ|check|add ИМЯ|del ИМЯ|links|update"

# ---------- сеть ----------

has_v4() { ip -4 route show default 2>/dev/null | grep -q . && curl -4 -fsS --max-time 6 https://api.ipify.org >/dev/null 2>&1; }

public_ip() {
  local ip
  ip=$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)
  [[ -z "$ip" ]] && ip=$(curl -6 -fsS --max-time 8 https://api6.ipify.org 2>/dev/null || curl -6 -fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)
  [[ -n "$ip" ]] || die "Не удалось определить внешний IP"
  echo "$ip"
}

# На IPv6-only сервере GitHub недоступен: временно включаем публичный DNS64/NAT64 (nat64.net)
RESOLV_LINK=""
dns64_on() {
  c "IPv4 нет — временно включаю DNS64 (nat64.net), чтобы скачать Xray с GitHub…"
  if [[ -L /etc/resolv.conf ]]; then RESOLV_LINK=$(readlink /etc/resolv.conf); fi
  cp -L /etc/resolv.conf "$DIR/resolv.conf.bak"
  rm -f /etc/resolv.conf
  printf 'nameserver 2a01:4f8:c2c:123f::1\nnameserver 2a00:1098:2c::1\nnameserver 2a01:4f9:c010:3f02::1\n' >/etc/resolv.conf
}
dns64_off() {
  [[ -f "$DIR/resolv.conf.bak" ]] || return 0
  rm -f /etc/resolv.conf
  if [[ -n "$RESOLV_LINK" ]]; then ln -s "$RESOLV_LINK" /etc/resolv.conf
  else cp "$DIR/resolv.conf.bak" /etc/resolv.conf; fi
  rm -f "$DIR/resolv.conf.bak"
}

port_free() { ! ss -ltnH "sport = :$1" | grep -v '"xray"' | grep -q .; }

# ---------- установка ----------

install_packages() {
  c "Пакеты…"
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
  apt-get update -qq
  apt-get install -y -qq curl openssl jq python3 qrencode iproute2 ca-certificates >/dev/null
}

install_xray() {
  c "Xray-core (официальный установщик XTLS)…"
  local inst
  inst=$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh) \
    || die "Не удалось скачать установщик Xray"
  bash -c "$inst" @ install >>/var/log/xvpn-cascade.log 2>&1 || true
  bash -c "$inst" @ install-geodata >>/var/log/xvpn-cascade.log 2>&1 || true
  [[ -x $XRAY ]] || die "Xray не установился, см. /var/log/xvpn-cascade.log"
  if [[ ! -s /usr/local/share/xray/geoip.dat ]]; then
    mkdir -p /usr/local/share/xray
    curl -fsSL -o /usr/local/share/xray/geoip.dat \
      https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat || true
    curl -fsSL -o /usr/local/share/xray/geosite.dat \
      https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat || true
  fi
  [[ -s /usr/local/share/xray/geoip.dat ]] || die "Не скачался geoip.dat"
  ok "$($XRAY version 2>/dev/null | sed -n 1p)"
}

enable_bbr() {
  cat >/etc/sysctl.d/99-xvpn-cascade.conf <<'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.ipv4.tcp_fastopen=3
EOF
  sysctl --system >/dev/null 2>&1 || true
}

gen_keys() { # → REALITY_PRIV REALITY_PUB
  local keys
  keys=$($XRAY x25519)
  # формат вывода менялся между версиями: берём значения первых двух строк
  REALITY_PRIV=$(echo "$keys" | sed -n 1p | awk -F': *' '{print $2}')
  REALITY_PUB=$(echo "$keys" | sed -n 2p | awk -F': *' '{print $2}')
  [[ -n "$REALITY_PRIV" && -n "$REALITY_PUB" ]] || die "Не удалось сгенерировать ключи Reality"
}

# Cloudflare WARP как выход в IPv4-интернет для IPv6-only сервера
setup_warp() {
  c "Cloudflare WARP для доступа к IPv4-сайтам…"
  local arch url
  case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) die "Неизвестная архитектура $(uname -m)" ;; esac
  url=$(curl -fsSL https://api.github.com/repos/ViRb3/wgcf/releases/latest \
        | jq -r --arg a "linux_$arch" '.assets[].browser_download_url | select(endswith($a))' | head -1)
  [[ -n "$url" ]] || die "Не удалось найти wgcf"
  curl -fsSL -o /usr/local/bin/wgcf "$url" && chmod +x /usr/local/bin/wgcf
  ( cd "$DIR" && { [[ -f wgcf-account.toml ]] || wgcf register --accept-tos >/dev/null; } && wgcf generate >/dev/null ) \
    || die "WARP: регистрация не удалась"
  ok "WARP настроен"
}

# ---------- конфиг и ссылки (python) ----------

py() {
  python3 - "$@" <<'PY'
import json, sys, uuid, urllib.parse as up, os, re

cmd = sys.argv[1]
STATE = "/etc/xvpn-cascade/state.json"

def load():
    with open(STATE) as f:
        s = json.load(f)
    if "upstream" in s:  # старый формат: один выход
        s["upstreams"] = [{"name": "eu1", "link": s.pop("upstream_link", ""), "outbound": s.pop("upstream")}]
    return s

def new_upstream(s, link, name=""):
    ups = s.setdefault("upstreams", [])
    if any(u["link"].split("#")[0] == link.split("#")[0] for u in ups):
        raise SystemExit("Этот выход уже добавлен")
    if not name:
        n = 1
        while any(u["name"] == f"eu{n}" for u in ups):
            n += 1
        name = f"eu{n}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", name):
        raise SystemExit("Имя выхода: латиница, цифры, _ - (до 24 символов)")
    if any(u["name"] == name for u in ups):
        raise SystemExit(f"Выход {name} уже есть")
    ups.append({"name": name, "link": link.strip(), "outbound": parse_vless(link)})
    return name

def exit_outbounds(s):
    outs = []
    for u in s["upstreams"]:
        o = json.loads(json.dumps(u["outbound"]))
        o["tag"] = "exit-" + u["name"]
        outs.append(o)
    return outs

def save(s):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)

def host(ip):
    return f"[{ip}]" if ":" in ip else ip

def parse_vless(link):
    """vless://uuid@host:port?type=..&security=..#name → outbound Xray."""
    u = up.urlsplit(link.strip())
    if u.scheme != "vless" or not u.username or not u.hostname:
        raise SystemExit("Это не ссылка vless://")
    q = {k: v[0] for k, v in up.parse_qs(u.query).items()}
    net = q.get("type", "tcp")
    if net == "splithttp":
        net = "xhttp"
    user = {"id": up.unquote(u.username), "encryption": "none"}
    if q.get("flow"):
        user["flow"] = q["flow"]
    ss = {"network": net}
    if net == "xhttp":
        x = {"path": q.get("path", "/"), "mode": q.get("mode", "auto")}
        if q.get("host"):
            x["host"] = q["host"]
        ss["xhttpSettings"] = x
    elif net == "ws":
        ss["wsSettings"] = {"path": q.get("path", "/"), "headers": {"Host": q.get("host", "")}}
    elif net == "grpc":
        ss["grpcSettings"] = {"serviceName": q.get("serviceName", "")}
    elif net != "tcp":
        raise SystemExit(f"Транспорт {net} не поддерживается")
    sec = q.get("security", "none")
    ss["security"] = sec
    fp = q.get("fp", "chrome")
    if sec == "reality":
        if not q.get("pbk"):
            raise SystemExit("В ссылке нет pbk (публичного ключа Reality)")
        ss["realitySettings"] = {"serverName": q.get("sni", ""), "publicKey": q["pbk"],
                                 "shortId": q.get("sid", ""), "fingerprint": fp,
                                 "spiderX": q.get("spx", "")}
    elif sec == "tls":
        t = {"serverName": q.get("sni") or q.get("host") or u.hostname, "fingerprint": fp}
        if q.get("alpn"):
            t["alpn"] = q["alpn"].split(",")
        ss["tlsSettings"] = t
    return {"tag": "exit", "protocol": "vless",
            "settings": {"vnext": [{"address": u.hostname, "port": u.port or 443, "users": [user]}]},
            "streamSettings": ss}

def warp_outbound(v6only):
    # wgcf-profile.conf повторяет ключи (AllowedIPs), поэтому разбираем построчно
    sec, i, p, addrs = "", {}, {}, []
    with open("/etc/xvpn-cascade/wgcf-profile.conf") as f:
        for line in f:
            line = line.strip()
            if line.startswith("["):
                sec = line.strip("[]").lower()
            elif "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                if sec == "interface" and k == "Address":
                    addrs += [a.strip() for a in v.split(",") if a.strip()]
                else:
                    (i if sec == "interface" else p)[k] = v
    if not i.get("PrivateKey") or not p.get("PublicKey") or not addrs:
        raise SystemExit("Профиль WARP неполный")
    ep = p.get("Endpoint", "engage.cloudflareclient.com:2408")
    if v6only:
        ep = "[2606:4700:d0::a29f:c001]:2408"
    return {"tag": "warp", "protocol": "wireguard",
            "settings": {"secretKey": i["PrivateKey"], "address": addrs, "mtu": 1280, "noKernelTun": True,
                         "peers": [{"publicKey": p["PublicKey"], "endpoint": ep,
                                    "allowedIPs": ["0.0.0.0/0", "::/0"]}]}}

def build(s):
    sniff = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": True}
    reality = {"dest": f"{s['sni']}:443", "serverNames": [s["sni"]],
               "privateKey": s["priv"], "shortIds": [s["sid"]]}
    block = {"tag": "block", "protocol": "blackhole"}
    if s["role"] == "exit":
        clients = [{"id": c["uuid"], "email": c["name"]} for c in s["clients"]]
        inbound = {"tag": "in", "listen": "::", "port": s["port"], "protocol": "vless",
                   "settings": {"clients": clients, "decryption": "none"},
                   "streamSettings": {"network": "xhttp", "security": "reality",
                                      "xhttpSettings": {"path": s["path"], "mode": "auto"},
                                      "realitySettings": reality},
                   "sniffing": sniff}
        outs = [{"tag": "direct", "protocol": "freedom",
                 "settings": {"domainStrategy": "UseIPv6" if s.get("warp") else "AsIs"}}, block]
        rules = [{"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
                 {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"}]
        ds = "AsIs"
        if s.get("warp"):
            outs.insert(1, warp_outbound(s.get("v6only", False)))
            rules.append({"type": "field", "ip": ["0.0.0.0/0"], "outboundTag": "warp"})
            ds = "IPIfNonMatch"
    else:
        clients = [{"id": c["uuid"], "email": c["name"], "flow": "xtls-rprx-vision"} for c in s["clients"]]
        inbound = {"tag": "in", "listen": "0.0.0.0", "port": s["port"], "protocol": "vless",
                   "settings": {"clients": clients, "decryption": "none"},
                   "streamSettings": {"network": "tcp", "security": "reality",
                                      "realitySettings": reality},
                   "sniffing": sniff}
        exits = exit_outbounds(s)
        if not exits:
            raise SystemExit("Нет ни одного выходного сервера")
        outs = exits + [{"tag": "direct", "protocol": "freedom"}, block]
        rules = [{"type": "field", "protocol": ["bittorrent"], "outboundTag": "block"},
                 {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"},
                 # российские сайты — напрямую с этого сервера
                 {"type": "field", "domain": [r"regexp:\.ru$", r"regexp:\.su$", r"regexp:\.xn--p1ai$",
                                              "domain:yandex.net", "domain:yandex.com", "domain:yastatic.net",
                                              "domain:vk.com", "domain:vk.me", "domain:userapi.com",
                                              "domain:mycdn.me", "domain:gosuslugi.ru"],
                  "outboundTag": "direct"},
                 {"type": "field", "ip": ["geoip:ru"], "outboundTag": "direct"},
                 {"type": "field", "network": "tcp,udp"}]
        if len(exits) == 1:
            rules[-1]["outboundTag"] = exits[0]["tag"]
        else:
            # несколько выходов: балансировщик выбирает самый быстрый живой, мёртвые исключаются
            rules[-1]["balancerTag"] = "exits"
        ds = "IPIfNonMatch"
    cfg = {"log": {"loglevel": "warning"},
           "inbounds": [inbound], "outbounds": outs,
           "routing": {"domainStrategy": ds, "rules": rules}}
    if s["role"] == "bridge" and len(s["upstreams"]) > 1:
        cfg["routing"]["balancers"] = [{"tag": "exits", "selector": ["exit-"],
                                        "strategy": {"type": "leastPing"},
                                        "fallbackTag": "exit-" + s["upstreams"][0]["name"]}]
        cfg["observatory"] = {"subjectSelector": ["exit-"],
                              "probeURL": "https://www.gstatic.com/generate_204",
                              "probeInterval": "30s", "enableConcurrency": True}
    return cfg

def test_config(s, name, port):
    """Временный конфиг: socks на 127.0.0.1:port → один выход (для проверки)."""
    o = [x for x in exit_outbounds(s) if x["tag"] == "exit-" + name]
    if not o:
        raise SystemExit(f"Выхода {name} нет")
    return {"log": {"loglevel": "none"},
            "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks",
                          "settings": {"udp": False}}],
            "outbounds": o}

def link(s, c):
    name = up.quote(f"{s.get('title', 'cascade')}-{c['name']}")
    if s["role"] == "exit":
        q = {"type": "xhttp", "security": "reality", "sni": s["sni"], "pbk": s["pub"],
             "sid": s["sid"], "fp": "chrome", "path": s["path"], "mode": "auto"}
    else:
        q = {"type": "tcp", "security": "reality", "sni": s["sni"], "pbk": s["pub"],
             "sid": s["sid"], "fp": "chrome", "flow": "xtls-rprx-vision"}
    return f"vless://{c['uuid']}@{host(s['ip'])}:{s['port']}?{up.urlencode(q)}#{name}"

if cmd == "init":            # init ROLE IP PORT SNI PRIV PUB SID TITLE [UPSTREAM_LINK|PATH] [WARP] [V6ONLY]
    role, ip, port, sni, priv, pub, sid, title, extra = sys.argv[2:11]
    s = {"role": role, "ip": ip, "port": int(port), "sni": sni, "priv": priv, "pub": pub,
         "sid": sid, "title": title, "clients": []}
    if role == "exit":
        s["path"] = extra
        s["warp"] = sys.argv[11] == "1"
        s["v6only"] = sys.argv[12] == "1"
        s["clients"].append({"name": "bridge", "uuid": str(uuid.uuid4())})
    else:
        for l in extra.split():
            new_upstream(s, l)
    save(s)
elif cmd == "check-link":    # check-link LINK → печатает адрес выходного сервера
    o = parse_vless(sys.argv[2])
    print(o["settings"]["vnext"][0]["address"])
elif cmd == "render":        # render OUT
    with open(sys.argv[2], "w") as f:
        json.dump(build(load()), f, indent=2, ensure_ascii=False)
elif cmd == "add":
    s = load(); name = sys.argv[2]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", name):
        raise SystemExit("Имя: латиница, цифры, _ . - (до 32 символов)")
    if any(c["name"] == name for c in s["clients"]):
        raise SystemExit(f"Клиент {name} уже есть")
    s["clients"].append({"name": name, "uuid": str(uuid.uuid4())}); save(s)
elif cmd == "del":
    s = load(); n = len(s["clients"])
    s["clients"] = [c for c in s["clients"] if c["name"] != sys.argv[2]]
    if len(s["clients"]) == n:
        raise SystemExit(f"Клиента {sys.argv[2]} нет")
    save(s)
elif cmd == "links":         # links [NAME]
    s = load()
    for c in s["clients"]:
        if len(sys.argv) < 3 or c["name"] == sys.argv[2]:
            print(c["name"] + "\t" + link(s, c))
elif cmd == "exit-add":      # exit-add LINK [NAME] → печатает имя
    s = load(); name = new_upstream(s, sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""); save(s)
    print(name)
elif cmd == "exit-del":
    s = load(); n = len(s["upstreams"])
    s["upstreams"] = [u for u in s["upstreams"] if u["name"] != sys.argv[2]]
    if len(s["upstreams"]) == n:
        raise SystemExit(f"Выхода {sys.argv[2]} нет")
    if not s["upstreams"]:
        raise SystemExit("Нельзя удалить последний выход")
    save(s)
elif cmd == "exits":         # имя \t адрес
    for u in load()["upstreams"]:
        v = u["outbound"]["settings"]["vnext"][0]
        print(f"{u['name']}\t{host(v['address'])}:{v['port']}")
elif cmd == "test-config":   # test-config NAME PORT OUT
    with open(sys.argv[4], "w") as f:
        json.dump(test_config(load(), sys.argv[2], int(sys.argv[3])), f)
elif cmd == "role":
    print(load()["role"])
PY
}

apply_config() {
  local tmp=$CONF.new
  mkdir -p "$(dirname "$CONF")"
  py render "$tmp"
  $XRAY run -test -c "$tmp" >/tmp/xvpn-cascade-test.log 2>&1 \
    || { cat /tmp/xvpn-cascade-test.log; die "Конфиг Xray не прошёл проверку"; }
  mv "$tmp" "$CONF"
  systemctl enable xray >/dev/null 2>&1
  systemctl restart xray
  sleep 2
  systemctl is-active --quiet xray || { journalctl -u xray -n 20 --no-pager; die "Xray не запустился"; }
  ok "Xray запущен"
}

show_links() { # [NAME]
  local role name l
  role=$(py role)
  while IFS=$'\t' read -r name l; do
    echo
    if [[ "$role" == exit ]]; then
      c "Ссылка для моста (вставьте её при запуске «cascade.sh bridge» на российском сервере):"
      echo "$l"
    else
      c "Клиент «$name» — импортируйте в Happ / Hiddify / v2RayTun / v2rayN:"
      echo "$l"
      qrencode -t ansiutf8 "$l" 2>/dev/null || true
    fi
  done < <(py links ${1:+"$1"})
}

# Сохраняем себя как команду xvpn-cascade (при запуске через curl копии на диске нет)
save_self() {
  local me=/usr/local/bin/xvpn-cascade src="${BASH_SOURCE[0]}"
  if [[ -f "$src" && "$src" != "$me" ]]; then cp "$src" "$me"
  elif [[ ! -f "$me" ]]; then
    curl -fsSL -o "$me" "https://raw.githubusercontent.com/${XVPN_REPO:-duboshin52/xvpn}/${XVPN_BRANCH:-main}/cascade.sh" || return 0
  fi
  chmod +x "$me"
}

# Проверка каждого выхода: временный Xray с socks-входом → запрос через выход
check_exits() {
  local name addr port pid code i=0 tmp
  tmp=$(mktemp -d)
  while IFS=$'\t' read -r name addr; do
    port=$((21080 + i)); i=$((i + 1))
    py test-config "$name" "$port" "$tmp/$name.json"
    $XRAY run -c "$tmp/$name.json" >/dev/null 2>&1 & pid=$!
    sleep 1.5
    code=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 12 \
           -x "socks5h://127.0.0.1:$port" https://www.gstatic.com/generate_204 || true)
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null || true
    if [[ "$code" == 204* ]]; then ok "$name ($addr) — работает, ${code#* } с"
    else warn "$name ($addr) — НЕ отвечает"; fi
  done < <(py exits)
  rm -rf "$tmp"
}

warn_v6() { # адрес выхода IPv6, а у моста нет IPv6
  if [[ "$1" == *:* ]] && ! ip -6 route get "$1" >/dev/null 2>&1; then
    warn "Выход $1 — IPv6-only, а у этого сервера нет IPv6: через него трафик не пойдёт."
  fi
}

need_state() { [[ -f "$STATE" ]] || die "Каскад ещё не установлен на этом сервере"; }

# ---------- режимы ----------

do_exit() {
  if [[ -f /etc/xvpn/node.env ]]; then
    warn "Здесь уже стоит нода xvpn — отдельный выход не нужен."
    echo "Создайте в панели ключ «ru-bridge», возьмите из него ссылку XHTTP-Reality (или Reality)"
    echo "и вставьте её при запуске «cascade.sh bridge» на российском сервере."
    exit 0
  fi
  mkdir -p "$DIR" && chmod 700 "$DIR"
  [[ -f "$STATE" ]] && { warn "Выход уже установлен."; show_links; exit 0; }

  local v6only=0 warp=0
  if ! has_v4; then v6only=1; warp=1; fi
  ask PORT "Порт" "443"
  port_free "$PORT" || die "Порт $PORT занят: $(ss -ltnpH "sport = :$PORT" | head -1)"
  ask SNI "SNI для Reality (зарубежный сайт с TLS 1.3, лучше рядом с VPS)" "www.microsoft.com"

  [[ $v6only == 1 ]] && dns64_on
  install_packages
  save_self
  install_xray
  [[ $warp == 1 ]] && setup_warp
  [[ $v6only == 1 ]] && dns64_off
  enable_bbr

  gen_keys
  py init exit "$(public_ip)" "$PORT" "$SNI" "$REALITY_PRIV" "$REALITY_PUB" "$(openssl rand -hex 8)" \
    "${TITLE:-exit}" "/$(openssl rand -hex 6)" "$warp" "$v6only"
  apply_config
  echo
  ok "Выходной сервер готов."
  [[ $v6only == 1 ]] && warn "Сервер IPv6-only: у российского сервера тоже должен быть IPv6."
  show_links
}

do_bridge() {
  mkdir -p "$DIR" && chmod 700 "$DIR"
  [[ -f "$STATE" ]] && { warn "Мост уже установлен. Новый клиент: xvpn-cascade add ИМЯ"; show_links; exit 0; }

  install_packages
  save_self
  if [[ -z "${UPSTREAM:-}" ]]; then
    c "Ссылки vless:// выходных серверов — по одной; несколько = резервирование. Пустая строка — закончить."
    local l n=1
    while :; do
      read -rp "Выход $n: " l </dev/tty
      [[ -z "$l" ]] && { [[ -n "${UPSTREAM:-}" ]] && break; warn "Нужен хотя бы один выход"; continue; }
      py check-link "$l" >/dev/null || { warn "Не похоже на ссылку vless://, ещё раз"; continue; }
      UPSTREAM="${UPSTREAM:-} $l"; n=$((n + 1))
    done
  fi
  local l
  for l in $UPSTREAM; do warn_v6 "$(py check-link "$l")"; done
  ask PORT "Порт" "443"
  port_free "$PORT" || die "Порт $PORT занят: $(ss -ltnpH "sport = :$PORT" | head -1)"
  ask SNI "SNI для Reality (сайт из белого списка)" "ya.ru"
  ask CLIENT "Имя первого клиента" "phone"

  install_xray
  enable_bbr
  gen_keys
  py init bridge "$(public_ip)" "$PORT" "$SNI" "$REALITY_PRIV" "$REALITY_PUB" "$(openssl rand -hex 8)" \
    "${TITLE:-ru}" "$UPSTREAM"
  py add "$CLIENT"
  apply_config
  echo
  ok "Мост готов: клиенты → этот сервер → выходные серверы. Российские сайты идут напрямую."
  c "Проверка выходов…"
  check_exits
  show_links
  echo
  echo "Клиенты: xvpn-cascade add|del ИМЯ · Ссылки: xvpn-cascade links"
  echo "Выходы:  xvpn-cascade exit-add ССЫЛКА · exit-del ИМЯ · check"
  echo "Проверьте, что в группе безопасности облака открыт TCP $PORT."
}

case "$MODE" in
  exit) do_exit ;;
  bridge) do_bridge ;;
  exit-add)
    need_state; [[ -n "$ARG" ]] || die "Укажите ссылку: xvpn-cascade exit-add 'vless://…' [ИМЯ]"
    [[ $(py role) == bridge ]] || die "Выходы добавляют на российском сервере (bridge)"
    warn_v6 "$(py check-link "$ARG")"
    name=$(py exit-add "$ARG" "${3:-}"); apply_config; ok "Выход $name добавлен"; check_exits ;;
  exit-del)
    need_state; [[ -n "$ARG" ]] || die "Укажите имя выхода (список: xvpn-cascade check)"
    py exit-del "$ARG"; apply_config; ok "Выход $ARG удалён" ;;
  check)
    need_state; [[ $(py role) == bridge ]] || die "Проверка выходов — на российском сервере (bridge)"
    check_exits ;;
  add)
    need_state; [[ -n "$ARG" ]] || die "Укажите имя: xvpn-cascade add ИМЯ"
    [[ $(py role) == bridge ]] || die "Клиентов добавляют на российском сервере (bridge)"
    py add "$ARG"; apply_config; show_links "$ARG" ;;
  del)
    need_state; [[ -n "$ARG" ]] || die "Укажите имя: xvpn-cascade del ИМЯ"
    py del "$ARG"; apply_config; ok "Клиент $ARG удалён" ;;
  links) need_state; show_links ;;
  update)
    need_state
    if ! has_v4; then dns64_on; install_xray; dns64_off; else install_xray; fi
    apply_config ;;
esac
