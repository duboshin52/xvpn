#!/usr/bin/env bash
# xvpn — свой VPN на Xray-core: один сервер, несколько серверов, каскад через Россию.
#
#   bash install.sh             — меню: выбрать схему и роль этого сервера
#   bash install.sh server      — главный сервер: ключи + Xray; бот по желанию (BOT=yes|no)
#   bash install.sh panel       — то же, что server с Telegram-ботом и мини-аппом
#   bash install.sh node        — дополнительный сервер-выход (подключается к главному кодом)
#   bash install.sh bridge      — мост каскада на российском VPS (подключается к главному кодом)
#   bash install.sh update      — обновить код и Xray, перезапустить сервисы
#   bash install.sh code        — показать код подключения этого сервера
#   bash install.sh rucdn       — вход через российский CDN (Yandex Cloud CDN и др.) на этом сервере
#   bash install.sh rucdn off   — убрать вход через российский CDN
#   bash install.sh reinstall   — удалить всё и поставить заново (с резервной копией)
#   bash install.sh uninstall   — полностью удалить xvpn и Xray (с резервной копией)
#
# Если xvpn уже установлен, установщик спросит: обновить с сохранением ключей или поставить начисто.
# Без вопросов: REINSTALL=update | clean | no; для uninstall — YES=1.
#
# Ответы можно задать переменными: DOMAIN (пусто = без домена), CF_API_TOKEN, BOT=yes|no, BOT_TOKEN,
# OWNER_ID, SNI_TCP, SNI_XHTTP, NODE_NAME, AGENT_PORT, SUB_TITLE, CERT_FILE, KEY_FILE.
set -Eeuo pipefail
trap 'printf "\033[1;31m✘ Ошибка в строке %s: %s\033[0m\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT=/opt/xvpn
ETC=/etc/xvpn
MODE="${1:-}"

c() { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
rnd() { local s; s=$(openssl rand -hex 32); echo "${s:0:${1:-12}}"; }
ask() { # ask VAR "вопрос" [default] — пустой ответ без default запрещён
  local var=$1 q=$2 def=${3:-} val
  if [[ -n "${!var+x}" && -n "${!var}" ]]; then return; fi
  read -rp "$q${def:+ [$def]}: " val </dev/tty
  printf -v "$var" '%s' "${val:-$def}"
  [[ -n "${!var}" ]] || die "$var обязателен"
}
ask_opt() { # ask_opt VAR "вопрос" — можно оставить пустым; уже заданная (даже пустая) переменная не спрашивается
  local var=$1 q=$2 val
  if [[ -n "${!var+x}" ]]; then return; fi
  read -rp "$q: " val </dev/tty
  printf -v "$var" '%s' "$val"
}
yes_no() { # yes_no VAR "вопрос" default(y|n)
  local var=$1 q=$2 def=$3 val
  if [[ -n "${!var:-}" ]]; then [[ "${!var}" =~ ^(y|yes|1|да)$ ]]; return; fi
  read -rp "$q [$([[ $def == y ]] && echo 'Д/н' || echo 'д/Н')]: " val </dev/tty
  val=${val:-$def}
  if [[ "$val" =~ ^[YyДд] ]]; then printf -v "$var" yes; return 0; fi
  printf -v "$var" no; return 1
}
read_pem() { # read_pem файл "что вставить"
  local out=$1 what=$2 line buf=""
  echo "Вставьте $what (вместе с -----BEGIN/END-----):"
  while IFS= read -r line </dev/tty; do
    buf+="$line"$'\n'
    [[ "$line" == -----END* ]] && break
  done
  printf '%s' "$buf" >"$out"
}

[[ $EUID -eq 0 ]] || die "Запустите от root (sudo -i)"

# Запуск одной командой через curl: скачиваем исходники из репозитория и перезапускаемся из них
REPO="${XVPN_REPO:-duboshin52/xvpn}"
BRANCH="${XVPN_BRANCH:-main}"
if [[ ! -f "$SRC/agent/agent.py" ]]; then
  command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }
  c "Скачиваю xvpn из github.com/$REPO ($BRANCH)…"
  rm -rf "$ROOT/src.new" && mkdir -p "$ROOT/src.new"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$BRANCH" | tar xz -C "$ROOT/src.new" --strip-components=1 \
    || die "Не удалось скачать репозиторий $REPO"
  rm -rf "$ROOT/src" && mv "$ROOT/src.new" "$ROOT/src"
  exec bash "$ROOT/src/install.sh" "$@"
fi

# ---------------------------------------------------------------- меню

menu() {
  cat <<'EOF'

  xvpn — выберите, что ставим на ЭТОТ сервер

  Один сервер
    1) Главный сервер, ключи командой xvpn (без бота)
    2) Главный сервер + Telegram-бот и мини-апп

  Несколько серверов и каскад (сначала поставьте главный сервер — пункт 1 или 2)
    3) Дополнительный сервер-выход (Европа и т. п.)
    4) Российский мост для каскада (Yandex Cloud, VK Cloud…)
       Для резерва входа поставьте два моста — оба попадут в подписку.

  Обслуживание
    5) Обновить xvpn и Xray (ключи и настройки сохранятся)
    6) Показать код подключения этого сервера
    7) Переустановить начисто
    8) Удалить xvpn с сервера

  Дополнительно (на уже установленный сервер)
    9) Веб-панель управления в браузере (HTTPS, логин/пароль, свой порт)
   10) Балансировщик трафика — ротация выходов каскада (на главном сервере)
   11) Канарейка — мониторинг маскировки/DNS/нод с алертами в Telegram (на главном)

EOF
  local n
  read -rp "Номер: " n </dev/tty
  case "$n" in
    1) MODE=server; BOT=${BOT:-no} ;;
    2) MODE=server; BOT=yes ;;
    3) MODE=node ;;
    4) MODE=bridge ;;
    5) MODE=update ;;
    6) MODE=code ;;
    7) MODE=reinstall ;;
    8) MODE=uninstall ;;
    9) MODE=webadmin ;;
    10) MODE=balancer ;;
    11) MODE=canary ;;
    *) die "Нет такого пункта" ;;
  esac
}
[[ -z "$MODE" ]] && menu
[[ "$MODE" == standalone ]] && MODE=server
[[ "$MODE" == info ]] && MODE=code
[[ "$MODE" == panel ]] && { MODE=server; BOT=yes; }
[[ "$MODE" == remove ]] && MODE=uninstall
[[ "$MODE" =~ ^(server|node|bridge|update|code|reinstall|uninstall|rucdn|webadmin|balancer|canary)$ ]] \
  || die "Использование: bash install.sh [server|panel|node|bridge|update|code|rucdn|webadmin|balancer|canary|reinstall|uninstall]"

# ---------------------------------------------------------------- сеть

has_v4() { curl -4 -fsS --max-time 6 https://api.ipify.org >/dev/null 2>&1; }
public_ip() {
  local ip
  ip=$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || curl -4 -fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)
  [[ -z "$ip" ]] && ip=$(curl -6 -fsS --max-time 8 https://api6.ipify.org 2>/dev/null || curl -6 -fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)
  [[ -n "$ip" ]] || die "Не удалось определить внешний IP сервера"
  echo "$ip"
}
url_host() { [[ "$1" == *:* ]] && echo "[$1]" || echo "$1"; }

# IPv6-only: GitHub без IPv6, поэтому на время установки включаем публичный DNS64/NAT64 (nat64.net)
DNS64=0; RESOLV_LINK=""
dns64_on() {
  has_v4 && return 0
  c "У сервера нет IPv4 — на время установки включаю DNS64 (nat64.net)…"
  [[ -L /etc/resolv.conf ]] && RESOLV_LINK=$(readlink /etc/resolv.conf)
  mkdir -p "$ETC"; cp -L /etc/resolv.conf "$ETC/resolv.conf.bak"
  rm -f /etc/resolv.conf
  printf 'nameserver 2a01:4f8:c2c:123f::1\nnameserver 2a00:1098:2c::1\nnameserver 2a01:4f9:c010:3f02::1\n' >/etc/resolv.conf
  DNS64=1
  trap dns64_off EXIT
}
dns64_off() {
  [[ $DNS64 == 1 && -f "$ETC/resolv.conf.bak" ]] || return 0
  rm -f /etc/resolv.conf
  if [[ -n "$RESOLV_LINK" ]]; then ln -s "$RESOLV_LINK" /etc/resolv.conf; else cp "$ETC/resolv.conf.bak" /etc/resolv.conf; fi
  rm -f "$ETC/resolv.conf.bak"; DNS64=0
}

# ---------------------------------------------------------------- установка

install_packages() {
  c "Пакеты…"
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
  apt-get update -qq
  local web="nginx libnginx-mod-stream"
  [[ "$MODE" == bridge ]] && web=""  # мосту nginx не нужен: Xray слушает 443 сам
  # shellcheck disable=SC2086
  apt-get install -y -qq curl openssl jq qrencode $web python3 python3-venv python3-pip ca-certificates >/dev/null
}

install_xray() {
  c "Xray-core (официальный установщик XTLS)…"
  local inst
  inst=$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh) \
    || die "Не удалось скачать установщик Xray"
  # установщик XTLS может вернуть ненулевой код и при успехе — проверяем результат сами
  bash -c "$inst" @ install >>/var/log/xvpn-install.log 2>&1 || true
  bash -c "$inst" @ install-geodata >>/var/log/xvpn-install.log 2>&1 || true
  [[ -x /usr/local/bin/xray ]] || die "Xray не установился, см. /var/log/xvpn-install.log"
  if [[ ! -s /usr/local/share/xray/geoip.dat ]]; then
    mkdir -p /usr/local/share/xray
    curl -fsSL -o /usr/local/share/xray/geoip.dat \
      https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat || true
    curl -fsSL -o /usr/local/share/xray/geosite.dat \
      https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat || true
  fi
  [[ -s /usr/local/share/xray/geoip.dat ]] || die "Не скачался geoip.dat"
  ok "$(/usr/local/bin/xray version 2>/dev/null | sed -n 1p)"
}

enable_bbr() {
  cat >/etc/sysctl.d/99-xvpn.conf <<'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
net.ipv4.tcp_fastopen=3
net.core.rmem_max=16777216
net.core.wmem_max=16777216
EOF
  sysctl --system >/dev/null 2>&1 || true
}

setup_venv() { # dir requirements...
  local dir=$1; shift
  [[ -x "$dir/venv/bin/python" ]] || python3 -m venv "$dir/venv"
  "$dir/venv/bin/pip" install -q --upgrade pip
  "$dir/venv/bin/pip" install -q --upgrade "$@"
}
AGENT_DEPS=(fastapi "uvicorn[standard]" psutil)
PANEL_DEPS=(fastapi "uvicorn[standard]" httpx "aiogram>=3.7,<4")

copy_code() {
  mkdir -p "$ROOT/agent" "$ROOT/panel/webapp"
  cp "$SRC/agent/agent.py" "$ROOT/agent/"
  if [[ -f "$ETC/panel.env" ]]; then
    cp "$SRC/panel/app.py" "$ROOT/panel/"
    cp "$SRC/panel/webapp/index.html" "$ROOT/panel/webapp/"
  fi
  install -m 755 "$SRC/xvpn" /usr/local/bin/xvpn
  if [[ "$SRC" != "$ROOT/src" ]]; then  # копия исходников для «xvpn code» и обновлений
    rm -rf "$ROOT/src" && mkdir -p "$ROOT/src" && cp -r "$SRC"/. "$ROOT/src/" && rm -rf "$ROOT/src/.git"
  fi
}

gen_reality_keys() { # → REALITY_PRIV REALITY_PUB
  local keys
  keys=$(/usr/local/bin/xray x25519)
  # формат вывода менялся между версиями: берём значения первых двух строк
  REALITY_PRIV=$(echo "$keys" | sed -n 1p | awk -F': *' '{print $2}')
  REALITY_PUB=$(echo "$keys" | sed -n 2p | awk -F': *' '{print $2}')
  [[ -n "$REALITY_PRIV" && -n "$REALITY_PUB" ]] || die "Не удалось сгенерировать ключи Reality"
}

# Cloudflare WARP: выход в IPv4-интернет для IPv6-only сервера
setup_warp() {
  [[ -s "$ETC/wgcf-profile.conf" ]] && return 0
  c "Cloudflare WARP для доступа к IPv4-сайтам…"
  local arch url
  case "$(uname -m)" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; *) die "Архитектура $(uname -m) не поддерживается" ;; esac
  url=$(curl -fsSL https://api.github.com/repos/ViRb3/wgcf/releases/latest \
        | jq -r --arg a "linux_$arch" '.assets[].browser_download_url | select(endswith($a))' | head -1)
  [[ -n "$url" ]] || die "Не удалось найти wgcf"
  curl -fsSL -o /usr/local/bin/wgcf "$url" && chmod +x /usr/local/bin/wgcf
  ( cd "$ETC" && { [[ -f wgcf-account.toml ]] || wgcf register --accept-tos >/dev/null; } && wgcf generate >/dev/null ) \
    || die "WARP: регистрация не удалась"
  ok "WARP настроен"
}
warp_json() { # профиль wgcf → JSON для node.json (разбор построчно: AllowedIPs повторяется)
  python3 - "$ETC/wgcf-profile.conf" <<'PY'
import json, sys
sec, i, p, addrs = "", {}, {}, []
for line in open(sys.argv[1]):
    line = line.strip()
    if line.startswith("["):
        sec = line.strip("[]").lower()
    elif "=" in line:
        k, v = (x.strip() for x in line.split("=", 1))
        if sec == "interface" and k == "Address":
            addrs += [a.strip() for a in v.split(",") if a.strip()]
        else:
            (i if sec == "interface" else p)[k] = v
print(json.dumps({"secret_key": i["PrivateKey"], "address": addrs, "public_key": p["PublicKey"],
                  "endpoint": "[2606:4700:d0::a29f:c001]:2408"}))
PY
}

# ---------------------------------------------------------------- Cloudflare

cf_api() { # cf_api METHOD path [json]
  curl -sS -X "$1" "https://api.cloudflare.com/client/v4$2" \
    -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" ${3:+--data "$3"}
}

cf_setup() {
  c "Настраиваю Cloudflare…"
  local zone="" zid="" d="$DOMAIN" r rid csr rtype=A
  [[ "$SERVER_IP" == *:* ]] && rtype=AAAA
  while [[ "$d" == *.* ]]; do  # sub.example.com → example.com
    zid=$(cf_api GET "/zones?name=$d" | jq -r '.result[0].id // empty')
    [[ -n "$zid" ]] && { zone=$d; break; }
    d=${d#*.}
  done
  [[ -n "$zid" ]] || die "Зона для $DOMAIN не найдена в Cloudflare (проверьте токен и что домен добавлен в аккаунт)"
  ok "Зона $zone"

  local body
  body=$(jq -nc --arg t "$rtype" --arg n "$DOMAIN" --arg ip "$SERVER_IP" '{type:$t,name:$n,content:$ip,proxied:true,ttl:1}')
  r=$(cf_api GET "/zones/$zid/dns_records?type=$rtype&name=$DOMAIN")
  rid=$(jq -r '.result[0].id // empty' <<<"$r")
  local cur; cur=$(jq -r '.result[0].content // empty' <<<"$r")
  if [[ -n "$cur" && "$cur" != "$SERVER_IP" ]]; then
    # домен уже занят другим сервером (например, главным) — перезапись его сломает
    warn "$DOMAIN уже указывает на $cur, а этот сервер — $SERVER_IP."
    warn "У каждого сервера должен быть свой домен (например, nl.$zone)."
    local yn; read -rp "Перенаправить $DOMAIN на этот сервер? [д/Н]: " yn </dev/tty
    [[ "$yn" =~ ^[YyДд] ]] || die "Отменено. Запустите заново и укажите другой поддомен."
  fi
  if [[ -n "$rid" ]]; then r=$(cf_api PUT "/zones/$zid/dns_records/$rid" "$body")
  else r=$(cf_api POST "/zones/$zid/dns_records" "$body"); fi
  [[ $(jq -r .success <<<"$r") == true ]] || die "DNS: $(jq -c .errors <<<"$r")"
  ok "DNS: $DOMAIN → $SERVER_IP ($rtype через Cloudflare)"

  r=$(cf_api PATCH "/zones/$zid/settings/ssl" '{"value":"strict"}')
  [[ $(jq -r .success <<<"$r") == true ]] && ok "SSL/TLS: Full (strict)" \
    || warn "SSL Full (strict) не включился: $(jq -c .errors <<<"$r") — включите вручную"
  r=$(cf_api PATCH "/zones/$zid/settings/grpc" '{"value":"on"}')
  [[ $(jq -r .success <<<"$r") == true ]] && ok "gRPC: On" \
    || warn "gRPC не включился через API — включите вручную (Network → gRPC), это не критично"

  # Origin-сертификат на 15 лет; закрытый ключ создаётся здесь и не покидает сервер
  openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
    -keyout "$ETC/key.pem" -out "$ETC/req.csr" -subj "/CN=$DOMAIN" 2>/dev/null
  chmod 600 "$ETC/key.pem"
  csr=$(cat "$ETC/req.csr")
  body=$(jq -nc --arg csr "$csr" --arg n "$DOMAIN" \
    '{csr:$csr,hostnames:[$n],request_type:"origin-ecc",requested_validity:5475}')
  r=$(cf_api POST "/certificates" "$body")
  [[ $(jq -r .success <<<"$r") == true ]] || die "Origin-сертификат: $(jq -c .errors <<<"$r")"
  jq -r .result.certificate <<<"$r" >"$ETC/cert.pem"
  rm -f "$ETC/req.csr"
  ok "Origin-сертификат выпущен (до $(jq -r .result.expires_on <<<"$r"))"
}

get_origin_cert() {
  [[ -s "$ETC/cert.pem" && -s "$ETC/key.pem" ]] && return 0
  echo
  if [[ -z "${CF_API_TOKEN+x}" && -z "${CERT_FILE:-}" ]]; then
    c "Cloudflare можно настроить автоматически (DNS, SSL Full strict, gRPC, Origin-сертификат)."
    c "Нужен API-токен: dash.cloudflare.com/profile/api-tokens → Create Token → Custom token, права:"
    c "  Zone:Read, DNS:Edit, Zone Settings:Edit, SSL and Certificates:Edit (Zone Resources: ваш домен)."
    read -rp "API-токен Cloudflare (Enter — вставить сертификат вручную): " CF_API_TOKEN </dev/tty
  fi
  if [[ -n "${CF_API_TOKEN:-}" ]]; then
    cf_setup
  elif [[ -n "${CERT_FILE:-}" && -n "${KEY_FILE:-}" ]]; then
    cp "$CERT_FILE" "$ETC/cert.pem"; cp "$KEY_FILE" "$ETC/key.pem"
  else
    c "Сертификат Cloudflare Origin: Cloudflare → SSL/TLS → Origin Server → Create Certificate"
    c "(хосты: ваш домен и *.домен, срок 15 лет). Режим SSL/TLS: Full (strict)."
    read_pem "$ETC/cert.pem" "Origin Certificate"
    read_pem "$ETC/key.pem" "Private Key"
  fi
  chmod 600 "$ETC/key.pem"
  openssl x509 -in "$ETC/cert.pem" -noout >/dev/null 2>&1 || die "Сертификат не читается"
  openssl pkey -in "$ETC/key.pem" -noout >/dev/null 2>&1 || die "Ключ не читается"
}

# ---------------------------------------------------------------- роли

# Самоподписанный сертификат агента: панель закрепляет его по отпечатку из кода подключения
agent_tls_cert() {
  [[ -s "$ETC/agent-cert.pem" ]] && return 0
  local san="IP:$SERVER_IP"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -keyout "$ETC/agent-key.pem" -out "$ETC/agent-cert.pem" -subj "/CN=xvpn-agent" \
    -addext "subjectAltName=$san" 2>/dev/null
  chmod 600 "$ETC/agent-key.pem"
}

# Нода-выход. AGENT_MODE: local — агент для панели на этом же сервере; nginx — через домен;
# tls — без домена, на отдельном порту с закреплённым сертификатом
configure_exit() {
  mkdir -p "$ETC" && chmod 700 "$ETC"
  if [[ -f "$ETC/node.env" ]]; then
    c "Найдена существующая конфигурация — используем её."
    # shellcheck disable=SC1091
    source "$ETC/node.env"
  else
    SERVER_IP=$(public_ip)
    echo
    c "Домен через Cloudflare даёт подписки и XHTTP через CDN (IP сервера скрыт)."
    [[ "$MODE" == server && "${BOT:-no}" == yes ]] && c "Для мини-аппа в Telegram домен обязателен; без него будет бот с командами."
    ask_opt DOMAIN "Домен этого сервера — свой для каждого сервера, например nl.example.com (Enter — без домена)"
    ask SNI_TCP "SNI для Reality (зарубежный сайт с TLS 1.3, лучше рядом с VPS)" "www.samsung.com"
    ask SNI_XHTTP "SNI для XHTTP-Reality (другой сайт)" "www.nvidia.com"
    ask NODE_NAME "Название сервера" "$(hostname -s)"
    if [[ "$MODE" == server ]]; then AGENT_MODE=local
    elif [[ -n "$DOMAIN" ]]; then AGENT_MODE=nginx
    else AGENT_MODE=tls; ask AGENT_PORT "Порт агента для главного сервера" "8443"; fi
    gen_reality_keys
    cat >"$ETC/node.env" <<EOF
ROLE='exit'
AGENT_MODE='$AGENT_MODE'
AGENT_PORT='${AGENT_PORT:-}'
DOMAIN='$DOMAIN'
SNI_TCP='$SNI_TCP'
SNI_XHTTP='$SNI_XHTTP'
NODE_NAME='$NODE_NAME'
REALITY_PRIV='$REALITY_PRIV'
REALITY_PUB='$REALITY_PUB'
SHORT_ID='$(openssl rand -hex 8)'
XHTTP_PATH='/$(rnd 10)'
RXHTTP_PATH='/$(rnd 10)'
AGENT_PATH='a-$(rnd 16)'
AGENT_SECRET='$(rnd 40)'
SERVER_IP='$SERVER_IP'
EOF
    chmod 600 "$ETC/node.env"
    # shellcheck disable=SC1091
    source "$ETC/node.env"
  fi
  [[ -n "$DOMAIN" ]] && get_origin_cert
  [[ "$AGENT_MODE" == tls ]] && agent_tls_cert
  local warp=null
  if [[ "$SERVER_IP" == *:* ]]; then setup_warp; warp=$(warp_json); fi
  local rucdn=null  # вход через российский CDN: клиенты → CDN_HOST (CDN) → ORIGIN_HOST (этот сервер)
  if [[ -n "${CDN_HOST:-}" && -n "${ORIGIN_HOST:-}" ]]; then
    rucdn=$(jq -nc --arg h "$CDN_HOST" --arg o "$ORIGIN_HOST" '{host:$h, origin:$o}')
  fi

  mkdir -p "$ROOT/agent"
  jq -n --arg d "$DOMAIN" --arg ip "$SERVER_IP" --arg xp "$XHTTP_PATH" --arg s "$AGENT_SECRET" \
        --arg pk "$REALITY_PRIV" --arg pub "$REALITY_PUB" --arg sid "$SHORT_ID" \
        --arg st "$SNI_TCP" --arg sx "$SNI_XHTTP" --arg rxp "$RXHTTP_PATH" --argjson w "$warp" --arg nm "$NODE_NAME" --argjson rc "$rucdn" \
    '{role:"exit", name:$nm, domain:$d, ip:$ip, xhttp_path:$xp, agent_secret:$s, warp:$w, ru_cdn:$rc,
      reality:{private_key:$pk, public_key:$pub, short_id:$sid, sni_tcp:$st, sni_xhttp:$sx, xhttp_path:$rxp}}' \
    >"$ROOT/agent/node.json"
  chmod 600 "$ROOT/agent/node.json"
}

# Мост каскада: без домена и Cloudflare, Xray слушает 443 сам
configure_bridge() {
  mkdir -p "$ETC" && chmod 700 "$ETC"
  if [[ -f "$ETC/node.env" ]]; then
    c "Найдена существующая конфигурация моста — используем её."
    # shellcheck disable=SC1091
    source "$ETC/node.env"
  else
    SERVER_IP=$(public_ip)
    [[ "$SERVER_IP" == *:* ]] && warn "У моста нет IPv4 — мобильные операторы при белых списках его, скорее всего, не пропустят."
    ask SNI_TCP "SNI для Reality (сайт из белого списка: ya.ru, vk.com…)" "ya.ru"
    ask NODE_NAME "Название моста" "RU-$(hostname -s)"
    ask AGENT_PORT "Порт агента для главного сервера" "8443"
    gen_reality_keys
    cat >"$ETC/node.env" <<EOF
ROLE='bridge'
AGENT_MODE='tls'
SNI_TCP='$SNI_TCP'
NODE_NAME='$NODE_NAME'
AGENT_PORT='$AGENT_PORT'
REALITY_PRIV='$REALITY_PRIV'
REALITY_PUB='$REALITY_PUB'
SHORT_ID='$(openssl rand -hex 8)'
AGENT_SECRET='$(rnd 40)'
SERVER_IP='$SERVER_IP'
EOF
    chmod 600 "$ETC/node.env"
    # shellcheck disable=SC1091
    source "$ETC/node.env"
  fi
  agent_tls_cert
  mkdir -p "$ROOT/agent"
  jq -n --arg ip "$SERVER_IP" --arg s "$AGENT_SECRET" --arg pk "$REALITY_PRIV" --arg pub "$REALITY_PUB" \
        --arg sid "$SHORT_ID" --arg st "$SNI_TCP" --arg nm "$NODE_NAME" \
    '{role:"bridge", name:$nm, ip:$ip, port:443, agent_secret:$s,
      reality:{private_key:$pk, public_key:$pub, short_id:$sid, sni_tcp:$st}}' >"$ROOT/agent/node.json"
  chmod 600 "$ROOT/agent/node.json"
}

# Главный сервер: база ключей, подписки, синхронизация нод; бот — по желанию
configure_panel() {
  if [[ -f "$ETC/panel.env" ]]; then return; fi
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  local bt="" oid=""
  if yes_no BOT "Подключить Telegram-бота для управления ключами?" n; then
    echo
    c "Создайте бота у @BotFather (/newbot) и вставьте токен."
    ask BOT_TOKEN "Токен бота"
    c "Ваш Telegram ID можно узнать у @userinfobot."
    ask OWNER_ID "Telegram ID владельца"
    [[ "$OWNER_ID" =~ ^[0-9]+$ ]] || die "OWNER_ID должен быть числом"
    bt=$BOT_TOKEN; oid=$OWNER_ID
    [[ -z "$DOMAIN" ]] && warn "Без домена мини-апп недоступен — бот будет работать командами (/newkey, /keys, /status)."
  fi
  ask SUB_TITLE "Название VPN в приложениях" "xvpn"
  cat >"$ETC/panel.env" <<EOF
BOT_TOKEN="$bt"
OWNER_ID="$oid"
DOMAIN="$DOMAIN"
CLI_TOKEN="$(rnd 48)"
PANEL_PATH="p-$(rnd 16)"
SUB_PATH="s-$(rnd 12)"
SUB_TITLE="$SUB_TITLE"
DB_PATH="$ROOT/panel/xvpn.db"
LOCAL_AGENT_URL="http://127.0.0.1:8081"
LOCAL_AGENT_SECRET="$AGENT_SECRET"
LOCAL_NAME="$NODE_NAME"
EOF
  chmod 600 "$ETC/panel.env"
}

# ---------------------------------------------------------------- сервисы

write_nginx() {
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  mkdir -p /etc/nginx/stream.d
  local v6listen=""  # слушать IPv6, только если он включён в системе
  [[ -s /proc/net/if_inet6 ]] && v6listen=$'\n    listen [::]:443;'
  local dom_line="" origin_cert=""
  [[ -n "$DOMAIN" ]] && dom_line="    $DOMAIN    127.0.0.1:8443;"
  if [[ -n "${ORIGIN_HOST:-}" && -s "/etc/letsencrypt/live/$ORIGIN_HOST/fullchain.pem" ]]; then
    origin_cert="/etc/letsencrypt/live/$ORIGIN_HOST"
    dom_line+=$'\n'"    $ORIGIN_HOST    127.0.0.1:8443;"
  fi
  # 443 делится по SNI: домен → nginx (Cloudflare/XHTTP/панель), SNI Reality → Xray
  cat >/etc/nginx/stream.d/xvpn.conf <<EOF
map \$ssl_preread_server_name \$xvpn_up {
$dom_line
    $SNI_TCP   127.0.0.1:4431;
    $SNI_XHTTP 127.0.0.1:4432;
    default    127.0.0.1:4431;
}
server {
    listen 443;$v6listen
    ssl_preread on;
    proxy_pass \$xvpn_up;
    proxy_connect_timeout 5s;
}
EOF
  grep -q 'stream.d' /etc/nginx/nginx.conf || \
    printf '\nstream {\n    include /etc/nginx/stream.d/*.conf;\n}\n' >>/etc/nginx/nginx.conf
  rm -f /etc/nginx/sites-enabled/default

  if [[ -z "$DOMAIN" && -z "${ORIGIN_HOST:-}" ]]; then
    rm -f /etc/nginx/sites-enabled/xvpn.conf /etc/nginx/sites-available/xvpn.conf
  else
    local panel_locs="" sub_loc=""
    if [[ -f "$ETC/panel.env" ]]; then
      # shellcheck disable=SC1091
      source "$ETC/panel.env"
      panel_locs="
    location /$PANEL_PATH/ { proxy_pass http://127.0.0.1:8080; include /etc/nginx/xvpn_proxy.conf; }
    location /$SUB_PATH/   { proxy_pass http://127.0.0.1:8080; include /etc/nginx/xvpn_proxy.conf; }"
      # подписка и через российский CDN — на случай, когда Cloudflare из сети клиента тормозит
      sub_loc="
    location /$SUB_PATH/   { proxy_pass http://127.0.0.1:8080; include /etc/nginx/xvpn_proxy.conf; }"
    fi
    local agent_loc=""
    [[ "$AGENT_MODE" == nginx ]] && agent_loc="
    location /$AGENT_PATH/ { proxy_pass http://127.0.0.1:8081/; include /etc/nginx/xvpn_proxy.conf; }"
    cat >/etc/nginx/xvpn_proxy.conf <<'EOF'
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto https;
proxy_read_timeout 60s;
EOF
    mkdir -p /var/www/xvpn /var/www/xvpn-acme
    local dom_block="" origin_block=""
    if [[ -n "$DOMAIN" ]]; then
      dom_block=$(cat <<EOF

server {
    listen 127.0.0.1:8443 ssl http2;
    server_name $DOMAIN;
    ssl_certificate     $ETC/cert.pem;
    ssl_certificate_key $ETC/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:xvpn:10m;
    root /var/www/xvpn;
    server_tokens off;

    location $XHTTP_PATH/ {
        client_max_body_size 0;
        client_body_timeout 1h;
        # packet-up: обычные HTTP-запросы через Cloudflare (gRPC там без отдельной
        # настройки буферизуется). grpc_pass их не принимает — только proxy_pass.
        proxy_pass http://unix:/dev/shm/xvpn-xhttp.sock;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_set_header Host \$host;
        proxy_set_header Connection "";
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }$agent_loc$panel_locs
    location / { try_files \$uri /index.html; }
}
EOF
)
    fi
    # источник для российского CDN: тот же XHTTP, сертификат Let's Encrypt; CDN ходит по HTTP/1.1
    if [[ -n "$origin_cert" ]]; then
      origin_block=$(cat <<EOF

server {
    listen 127.0.0.1:8443 ssl http2;
    server_name $ORIGIN_HOST;
    ssl_certificate     $origin_cert/fullchain.pem;
    ssl_certificate_key $origin_cert/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    root /var/www/xvpn;
    server_tokens off;

    location $XHTTP_PATH/ {
        client_max_body_size 0;
        client_body_timeout 1h;
        proxy_pass http://unix:/dev/shm/xvpn-xhttp.sock;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_set_header Host \$host;
        proxy_set_header Connection "";
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }$sub_loc
    location / { try_files \$uri /index.html; }
}
EOF
)
    fi
    local v6_80=""
    [[ -n "$v6listen" ]] && v6_80=$'\n    listen [::]:80;'
    cat >/etc/nginx/sites-available/xvpn.conf <<EOF
server {
    listen 80;$v6_80
    server_name _;
    location /.well-known/acme-challenge/ { root /var/www/xvpn-acme; }
    location / { return 301 https://\$host\$request_uri; }
}$dom_block$origin_block
EOF
    ln -sf /etc/nginx/sites-available/xvpn.conf /etc/nginx/sites-enabled/xvpn.conf
    [[ -f /var/www/xvpn/index.html ]] || cat >/var/www/xvpn/index.html <<'EOF'
<!doctype html><html><head><meta charset="utf-8"><title>Welcome</title>
<style>body{font-family:system-ui;max-width:640px;margin:80px auto;color:#333}</style></head>
<body><h1>Coming soon</h1><p>This site is under construction.</p></body></html>
EOF
  fi
  nginx -t 2>&1 | tail -2
  systemctl enable --now nginx >/dev/null 2>&1
  systemctl reload nginx || systemctl restart nginx
}

write_agent_service() {
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  local listen="--host 127.0.0.1 --port 8081"
  if [[ "${AGENT_MODE:-}" == tls || "${ROLE:-exit}" == bridge ]]; then
    # «::» только для IPv6-сервера: на ВМ с выключенным IPv6 (Yandex Cloud и др.) uvicorn на «::» не стартует
    local host=0.0.0.0; [[ "${SERVER_IP:-}" == *:* ]] && host="::"
    listen="--host $host --port $AGENT_PORT --ssl-keyfile $ETC/agent-key.pem --ssl-certfile $ETC/agent-cert.pem"
  fi
  cat >/etc/systemd/system/xvpn-agent.service <<EOF
[Unit]
Description=xvpn node agent
After=network.target xray.service

[Service]
WorkingDirectory=$ROOT/agent
ExecStart=$ROOT/agent/venv/bin/uvicorn agent:app $listen --log-level warning
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

write_panel_service() {
  cat >/etc/systemd/system/xvpn-panel.service <<EOF
[Unit]
Description=xvpn main server (keys, subscriptions, bot)
After=network.target xvpn-agent.service

[Service]
WorkingDirectory=$ROOT/panel
EnvironmentFile=$ETC/panel.env
ExecStart=$ROOT/panel/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8080 --proxy-headers --log-level warning
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

bootstrap_xray() {
  # стартовый конфиг без пользователей (дальше его пишет агент по команде главного сервера)
  ( cd "$ROOT/agent" && XVPN_AGENT_DIR="$ROOT/agent" "$ROOT/agent/venv/bin/python" -c \
      'import agent; agent.apply_users([])' ) || die "Конфиг Xray не прошёл проверку"
  systemctl enable xray >/dev/null 2>&1
  systemctl restart xray
}

# Если включён ufw — открыть нужные порты (иначе 443 закрыт и Cloudflare отдаёт 522)
open_firewall() {
  command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active' || return 0
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  c "ufw включён — открываю порты…"
  ufw allow 22/tcp >/dev/null  # не потерять SSH
  ufw allow 443/tcp >/dev/null
  [[ -n "${DOMAIN:-}" || -n "${ORIGIN_HOST:-}" ]] && ufw allow 80/tcp >/dev/null
  # порт агента защищён секретом и TLS; лучше потом сузить до IP главного сервера
  [[ "${AGENT_MODE:-}" == tls ]] && ufw allow "$AGENT_PORT"/tcp >/dev/null
  ok "ufw: открыты 443$([[ -n "${DOMAIN:-}" ]] && echo ', 80')$([[ "${AGENT_MODE:-}" == tls ]] && echo ", $AGENT_PORT")"
}

# ---------------------------------------------------------------- код подключения

connection_code() {
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  local url fp=""
  if [[ "${AGENT_MODE:-}" == nginx ]]; then
    url="https://$DOMAIN/$AGENT_PATH"
  else
    url="https://$(url_host "$SERVER_IP"):$AGENT_PORT"
    fp=$(openssl x509 -in "$ETC/agent-cert.pem" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':' | tr 'A-F' 'a-f')
  fi
  jq -jnc --arg u "$url" --arg s "$AGENT_SECRET" --arg f "$fp" '{u:$u,s:$s} + (if $f != "" then {f:$f} else {} end)' \
    | base64 -w0 | tr '+/' '-_' | tr -d '='
}

show_code() {
  [[ -f "$ETC/node.env" ]] || die "xvpn не установлен"
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  if [[ "${AGENT_MODE:-}" == local ]]; then
    c "Это главный сервер — к нему подключают другие. Добавить сервер: xvpn server add КОД"
    return
  fi
  echo
  c "Код подключения ${NODE_NAME} ($([[ "${ROLE:-exit}" == bridge ]] && echo 'мост' || echo 'выход')):"
  echo
  connection_code
  echo
  echo
  echo "На главном сервере:  xvpn server add <код> ${NODE_NAME}"
  echo "или в мини-аппе: «Серверы» → «Добавить сервер»."
  if [[ "${AGENT_MODE:-}" == tls ]]; then
    echo "Откройте в файрволе/группе безопасности: TCP 443 (клиенты) и TCP $AGENT_PORT (главный сервер)."
    echo "Порт $AGENT_PORT лучше открыть только для IP главного сервера."
  fi
}

# ---------------------------------------------------------------- существующая установка

installed() {
  [[ -d "$ETC" || -d /etc/xvpn-cascade || -f /etc/systemd/system/xvpn-agent.service \
     || -f /etc/systemd/system/xvpn-panel.service || -f /usr/local/bin/xvpn ]]
}

current_role() { # main | exit | bridge | cascade-old | broken
  if [[ -f "$ETC/panel.env" ]]; then echo main
  elif [[ -f "$ETC/node.env" ]] && grep -q "^ROLE='bridge'" "$ETC/node.env"; then echo bridge
  elif [[ -f "$ETC/node.env" ]]; then echo exit
  elif [[ -d /etc/xvpn-cascade ]]; then echo cascade-old
  else echo broken; fi
}
role_name() {
  case "$1" in
    main) echo "главный сервер" ;; exit) echo "сервер-выход" ;; bridge) echo "мост (РФ)" ;;
    cascade-old) echo "старый cascade.sh" ;; *) echo "незавершённая установка" ;;
  esac
}

backup() {
  local f files=()
  for f in etc/xvpn etc/xvpn-cascade opt/xvpn/panel/xvpn.db; do [[ -e "/$f" ]] && files+=("$f"); done
  [[ ${#files[@]} -gt 0 ]] || return 0
  f="/root/xvpn-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  tar czf "$f" -C / "${files[@]}" 2>/dev/null && chmod 600 "$f" && ok "Резервная копия: $f"
}

# Полное удаление. keep_src=1 — сохранить исходники для установки сразу после удаления
do_uninstall() {
  local keep_src=${1:-0}
  if ! installed; then warn "xvpn на этом сервере не найден."; return 0; fi
  if [[ $keep_src == 0 && "${YES:-}" != 1 ]]; then
    local yn
    warn "Будут удалены xvpn, Xray, ключи и настройки этого сервера ($(role_name "$(current_role)"))."
    read -rp "Удалить? Резервная копия сохранится в /root [д/Н]: " yn </dev/tty
    [[ "$yn" =~ ^[YyДд] ]] || die "Отменено"
  fi
  backup
  c "Удаляю xvpn…"
  local u
  for u in xvpn-panel xvpn-agent xray xvpn-backup.timer xvpn-balancer.timer xvpn-canary.timer; do systemctl disable --now "$u" >/dev/null 2>&1 || true; done
  rm -rf /etc/systemd/system/xvpn-panel.service /etc/systemd/system/xvpn-agent.service \
         /etc/systemd/system/xvpn-backup.service /etc/systemd/system/xvpn-backup.timer \
         /etc/systemd/system/xvpn-balancer.service /etc/systemd/system/xvpn-balancer.timer \
         /etc/systemd/system/xvpn-canary.service /etc/systemd/system/xvpn-canary.timer \
         /etc/systemd/system/xray.service /etc/systemd/system/xray@.service \
         /etc/systemd/system/xray.service.d /etc/systemd/system/xray@.service.d
  systemctl daemon-reload
  rm -rf /usr/local/bin/xray /usr/local/etc/xray /usr/local/share/xray /var/log/xray /usr/local/bin/wgcf
  # nginx: убираем только своё
  rm -f /etc/nginx/stream.d/xvpn.conf /etc/nginx/sites-enabled/xvpn.conf /etc/nginx/sites-available/xvpn.conf \
        /etc/nginx/xvpn_proxy.conf
  rm -rf /var/www/xvpn
  if command -v nginx >/dev/null; then
    if [[ -e /etc/nginx/sites-available/default && ! -e /etc/nginx/sites-enabled/default ]]; then
      ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
    fi
    if nginx -t >/dev/null 2>&1; then systemctl reload nginx >/dev/null 2>&1 || true
    else systemctl stop nginx >/dev/null 2>&1 || true; fi
  fi
  rm -f /etc/sysctl.d/99-xvpn.conf /etc/sysctl.d/99-xvpn-cascade.conf
  sysctl --system >/dev/null 2>&1 || true
  rm -f /usr/local/bin/xvpn /usr/local/bin/xvpn-cascade
  rm -rf "$ETC" /etc/xvpn-cascade
  if [[ $keep_src == 1 && "$SRC" == "$ROOT"/* ]]; then  # исходники нужны для установки после удаления
    local tmp; tmp=$(mktemp -d /tmp/xvpn-src.XXXX)
    cp -r "$SRC"/. "$tmp/"; SRC=$tmp
  fi
  rm -rf "$ROOT"
  ok "xvpn удалён"
}

# Перед установкой: если xvpn уже стоит — обновить, переустановить начисто или отменить
check_existing() { # целевая роль: main | exit | bridge
  installed || return 0
  local want=$1 cur choice
  cur=$(current_role)
  echo
  warn "На сервере уже установлен xvpn: $(role_name "$cur")."
  case "${REINSTALL:-}" in
    update) choice=1 ;; clean|yes) choice=2 ;; no) die "Отменено (REINSTALL=no)" ;;
    *)
      if [[ "$cur" == "$want" ]]; then
        echo "  1) Обновить — ключи, настройки и подключения сохранятся"
        echo "  2) Переустановить начисто — всё удалить и поставить заново"
        echo "  3) Отмена"
        read -rp "Номер [1]: " choice </dev/tty; choice=${choice:-1}
      else
        echo "  Вы ставите: $(role_name "$want")."
        echo "  2) Удалить текущую установку и поставить заново"
        echo "  3) Отмена"
        read -rp "Номер [3]: " choice </dev/tty; choice=${choice:-3}
        [[ "$choice" == 1 ]] && choice=3
      fi ;;
  esac
  case "$choice" in
    1) [[ "$cur" == "$want" ]] || die "Обновление возможно только в той же роли"; do_update; exit 0 ;;
    2) do_uninstall 1
       if [[ "$cur" == exit || "$cur" == bridge ]]; then
         warn "Код подключения изменится. На главном сервере уберите старую запись (xvpn server del ИМЯ)"
         warn "и добавьте новый код, который покажет установщик."
       fi ;;
    *) die "Отменено" ;;
  esac
}

# ---------------------------------------------------------------- сценарии

do_server() {
  check_existing main
  dns64_on
  install_packages
  install_xray
  enable_bbr
  configure_exit
  configure_panel
  copy_code
  c "Python-окружения…"
  setup_venv "$ROOT/agent" "${AGENT_DEPS[@]}"
  setup_venv "$ROOT/panel" "${PANEL_DEPS[@]}"
  write_agent_service
  bootstrap_xray
  write_panel_service
  write_nginx
  install_backup
  open_firewall
  dns64_off
  systemctl daemon-reload
  systemctl enable --now xvpn-agent >/dev/null 2>&1; systemctl restart xvpn-agent
  systemctl enable --now xvpn-panel >/dev/null 2>&1; systemctl restart xvpn-panel
  # shellcheck disable=SC1091
  source "$ETC/panel.env"
  for _ in 1 2 3 4 5 6 7 8 9 10; do xvpn status >/dev/null 2>&1 && break; sleep 1; done
  echo
  ok "Главный сервер готов."
  echo
  echo "Ключи:        xvpn add ИМЯ [дней] [ГБ]   ·   xvpn list   ·   xvpn show ИМЯ"
  echo "Серверы:      xvpn server add КОД         ·   xvpn servers"
  echo "Справка:      xvpn help"
  [[ -n "$BOT_TOKEN" && -n "$DOMAIN" ]] && echo "Telegram:     откройте бота → /start → «Открыть панель»."
  [[ -n "$BOT_TOKEN" && -z "$DOMAIN" ]] && echo "Telegram:     откройте бота → /start (команды /newkey, /keys, /status)."
  [[ -n "$DOMAIN" ]] && echo "Cloudflare:   SSL/TLS = Full (strict), Network → gRPC = On."
  echo
  echo "Каскад через Россию: на российском VPS выполните «bash install.sh bridge»"
  echo "и добавьте его код сюда: xvpn server add КОД"
}

do_node() {
  check_existing exit
  dns64_on
  install_packages
  install_xray
  enable_bbr
  configure_exit
  copy_code
  c "Python-окружение агента…"
  setup_venv "$ROOT/agent" "${AGENT_DEPS[@]}"
  write_agent_service
  bootstrap_xray
  write_nginx
  open_firewall
  dns64_off
  systemctl daemon-reload
  systemctl enable --now xvpn-agent >/dev/null 2>&1; systemctl restart xvpn-agent
  ok "Сервер-выход готов."
  [[ -n "$DOMAIN" ]] && echo "Проверьте в Cloudflare: SSL/TLS = Full (strict), Network → gRPC = On."
  show_code
}

do_bridge() {
  check_existing bridge
  install_packages
  install_xray
  enable_bbr
  configure_bridge
  copy_code
  c "Python-окружение агента…"
  setup_venv "$ROOT/agent" "${AGENT_DEPS[@]}"
  write_agent_service
  bootstrap_xray
  open_firewall
  systemctl daemon-reload
  systemctl enable --now xvpn-agent >/dev/null 2>&1; systemctl restart xvpn-agent
  ok "Мост готов. Выходы и ключи он получит от главного сервера после подключения."
  show_code
}

do_update() {
  [[ -f "$ETC/node.env" ]] || die "xvpn не установлен"
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  # установки до появления ролей: нода с доменом и агентом за nginx
  if ! grep -q '^ROLE=' "$ETC/node.env"; then
    { echo "ROLE='exit'"; [[ -f "$ETC/panel.env" ]] && echo "AGENT_MODE='local'" || echo "AGENT_MODE='nginx'"; } >>"$ETC/node.env"
  fi
  if [[ -f "$ETC/panel.env" ]] && ! grep -q '^CLI_TOKEN=' "$ETC/panel.env"; then
    echo "CLI_TOKEN=\"$(rnd 48)\"" >>"$ETC/panel.env"
  fi
  dns64_on
  command -v qrencode >/dev/null || apt-get install -y -qq qrencode >/dev/null || true
  install_xray
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  if [[ "$ROLE" == bridge ]]; then configure_bridge; else configure_exit; fi
  copy_code
  setup_venv "$ROOT/agent" "${AGENT_DEPS[@]}"
  [[ -f "$ETC/panel.env" ]] && { setup_venv "$ROOT/panel" "${PANEL_DEPS[@]}"; write_panel_service; }
  write_agent_service
  [[ "$ROLE" == bridge ]] || write_nginx
  open_firewall
  dns64_off
  systemctl daemon-reload
  systemctl restart xray xvpn-agent
  [[ -f "$ETC/panel.env" ]] && systemctl restart xvpn-panel
  # дополнительные компоненты: бэкап ставим всем, у кого есть панель; балансировщик — если был
  install_backup
  [[ -f /etc/systemd/system/xvpn-balancer.timer ]] && install_balancer
  [[ -f /etc/systemd/system/xvpn-canary.timer ]] && install_canary
  if [[ -f /etc/xvpn/webadmin.env ]]; then
    cp "$SRC/panel/webapp/admin.html" /opt/xvpn-webadmin/index.html 2>/dev/null || true
    cp "$SRC/panel/webadmin-passwd.py" /opt/xvpn-webadmin/passwd.py 2>/dev/null || true
    cp "$SRC/panel/xvpn-webadmin-passwd.service" /etc/systemd/system/ 2>/dev/null || true
    systemctl daemon-reload; systemctl enable --now xvpn-webadmin-passwd >/dev/null 2>&1 || true
  fi
  ok "Обновлено"
}

# ---------------------------------------------------------------- балансировщик

install_balancer() {
  [[ -f "$ETC/panel.env" ]] || die "Балансировщик ставится на главный сервер (здесь нет панели)"
  c "Балансировщик трафика (ротация выходов каскада)…"
  mkdir -p "$ROOT/balancer"
  cp "$SRC/balancer/balancer.py" "$ROOT/balancer/"
  cp "$SRC/balancer/xvpn-balancer.service" "$SRC/balancer/xvpn-balancer.timer" /etc/systemd/system/
  if [[ ! -f "$ETC/balancer.env" ]]; then
    cp "$SRC/balancer/balancer.env.example" "$ETC/balancer.env"
    chmod 600 "$ETC/balancer.env"
  fi
  systemctl daemon-reload
  systemctl enable --now xvpn-balancer.timer >/dev/null 2>&1
  python3 "$ROOT/balancer/balancer.py" || true
  # shellcheck disable=SC1091
  source "$ETC/balancer.env"
  ok "Балансировщик включён: ротация каждые ${LIMIT_GB:-200} ГБ, активных выходов ${PAIR:-2}, сброс счётчиков ${RESET_DAY:-1}-го числа"
  echo "  Настройки: $ETC/balancer.env · лог: journalctl -u xvpn-balancer"
}

# ---------------------------------------------------------------- резервное копирование

install_backup() {
  [[ -f "$ETC/panel.env" ]] || return 0  # бэкап нужен на главном сервере (там база ключей)
  c "Резервное копирование базы ключей…"
  mkdir -p "$ROOT/backup"
  cp "$SRC/backup/backup.py" "$ROOT/backup/"
  cp "$SRC/backup/xvpn-backup.service" "$SRC/backup/xvpn-backup.timer" /etc/systemd/system/
  if [[ ! -f "$ETC/backup.env" ]]; then
    cp "$SRC/backup/backup.env.example" "$ETC/backup.env"
    chmod 600 "$ETC/backup.env"
  fi
  systemctl daemon-reload
  systemctl enable --now xvpn-backup.timer >/dev/null 2>&1
  ok "Бэкапы: ежедневно в /root/xvpn-backups (настройки: $ETC/backup.env)"
}

# ---------------------------------------------------------------- канарейка

install_canary() {
  [[ -f "$ETC/panel.env" ]] || die "Канарейка ставится на главный сервер (здесь нет панели)"
  c "Канарейка — мониторинг маскировки, DNS и нод с алертами в Telegram…"
  mkdir -p "$ROOT/canary"
  cp "$SRC/canary/canary.py" "$ROOT/canary/"
  cp "$SRC/canary/xvpn-canary.service" "$SRC/canary/xvpn-canary.timer" /etc/systemd/system/
  if [[ ! -f "$ETC/canary.env" ]]; then
    cp "$SRC/canary/canary.env.example" "$ETC/canary.env"
    chmod 600 "$ETC/canary.env"
  fi
  systemctl daemon-reload
  systemctl enable --now xvpn-canary.timer >/dev/null 2>&1
  python3 "$ROOT/canary/canary.py" || true
  ok "Канарейка включена: проверки каждые 5 минут, алерты в Telegram владельцу"
  echo "  Автоочистка DNS от мёртвых IP: впишите CF_TOKEN и CF_ZONE в $ETC/canary.env"
  echo "  Лог: journalctl -u xvpn-canary"
}

# ---------------------------------------------------------------- веб-панель

# HTTPS-страница управления (те же «коты», что в мини-аппе) на любом сервере:
# Basic Auth + секретный путь + нестандартный порт; API панели — напрямую (на главном)
# или через SSH-туннель с урезанным ключом (на любом другом сервере).
install_webadmin() {
  echo
  c "Веб-панель управления на этом сервере"
  local pp ct upstream main_ip=""
  if [[ -f "$ETC/panel.env" ]]; then
    # shellcheck disable=SC1091
    source "$ETC/panel.env"
    pp=$PANEL_PATH; ct=$CLI_TOKEN; upstream="http://127.0.0.1:8080"
  else
    command -v jq >/dev/null || apt-get install -y -qq jq >/dev/null
    ask WA_CODE "Код веб-панели (на главном сервере выполните: xvpn webadmin)"
    local decoded
    decoded=$(tr '_-' '/+' <<<"$WA_CODE" | base64 -d 2>/dev/null) || die "Код не читается"
    main_ip=$(jq -r .ip <<<"$decoded"); pp=$(jq -r .p <<<"$decoded"); ct=$(jq -r .t <<<"$decoded")
    [[ "$main_ip" =~ ^[0-9.]+$ && -n "$pp" && -n "$ct" ]] || die "Код повреждён"
    upstream="http://127.0.0.1:18080"
  fi
  SERVER_IP=$(public_ip)
  ask WA_PORT "Порт веб-панели" "9443"

  install_packages 2>/dev/null || { export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; }
  apt-get install -y -qq nginx autossh apache2-utils openssl >/dev/null

  mkdir -p /opt/xvpn-webadmin /etc/xvpn-webadmin
  cp "$SRC/panel/webapp/admin.html" /opt/xvpn-webadmin/index.html
  cp "$SRC/panel/webadmin-passwd.py" /opt/xvpn-webadmin/passwd.py
  cp "$SRC/panel/xvpn-webadmin-passwd.service" /etc/systemd/system/
  systemctl daemon-reload; systemctl enable --now xvpn-webadmin-passwd >/dev/null 2>&1
  [[ -s /etc/xvpn-webadmin/cert.pem ]] || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
    -nodes -days 3650 -keyout /etc/xvpn-webadmin/key.pem -out /etc/xvpn-webadmin/cert.pem \
    -subj "/CN=xvpn-panel" 2>/dev/null

  local wa_pass wa_path
  wa_pass=$(rnd 12); wa_path="k-$(rnd 6)"
  htpasswd -cb /etc/xvpn-webadmin/.htpasswd admin "$wa_pass" >/dev/null
  chgrp www-data /etc/xvpn-webadmin/.htpasswd 2>/dev/null || true
  chmod 640 /etc/xvpn-webadmin/.htpasswd; chmod 600 /etc/xvpn-webadmin/key.pem

  if [[ -n "$main_ip" ]]; then
    c "SSH-туннель к панели главного сервера ($main_ip)…"
    [[ -f /root/.ssh/id_ed25519 ]] || ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N '' -q
    local pub; pub=$(cat /root/.ssh/id_ed25519.pub)
    echo
    warn "Разрешите туннель на главном сервере, выполнив там:"
    echo "  xvpn webadmin-allow '$pub'"
    cat >/etc/systemd/system/xvpn-webtunnel.service <<EOF
[Unit]
Description=xvpn webadmin tunnel to panel API
After=network-online.target
Wants=network-online.target

[Service]
Environment=AUTOSSH_GATETIME=0
ExecStart=/usr/bin/autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -i /root/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new -L 127.0.0.1:18080:127.0.0.1:8080 root@$main_ip
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload; systemctl enable --now xvpn-webtunnel >/dev/null 2>&1
    local tries=0
    until curl -sf -m 8 -H "X-CLI-Token: $ct" "http://127.0.0.1:18080/$pp/api/me" >/dev/null 2>&1; do
      tries=$((tries+1)); [[ $tries -gt 30 ]] && die "Туннель так и не заработал — проверьте ключ на главном и запустите установку снова"
      read -rp "Выполнили команду на главном? Enter — проверить снова: " </dev/tty
      systemctl restart xvpn-webtunnel; sleep 3
    done
    ok "Туннель работает"
  fi

  cat >/etc/nginx/sites-available/xvpn-webadmin <<EOF
# xvpn webadmin: HTTPS :$WA_PORT, Basic Auth, секретный путь
limit_req_zone \$binary_remote_addr zone=wa:10m rate=6r/m;

server {
    listen $WA_PORT ssl;
    server_name _;

    ssl_certificate     /etc/xvpn-webadmin/cert.pem;
    ssl_certificate_key /etc/xvpn-webadmin/key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:wa_ssl:10m;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy no-referrer always;

    location / { return 404; }

    location /$wa_path/ {
        limit_req zone=wa burst=10 nodelay;
        auth_basic "restricted";
        auth_basic_user_file /etc/xvpn-webadmin/.htpasswd;
        alias /opt/xvpn-webadmin/;
        index index.html;
        try_files \$uri \$uri/ =404;
    }

    location /$wa_path/api/ {
        limit_req zone=wa burst=24 nodelay;
        auth_basic "restricted";
        auth_basic_user_file /etc/xvpn-webadmin/.htpasswd;
        proxy_pass $upstream/$pp/api/;
        proxy_http_version 1.1;
        proxy_set_header Host 127.0.0.1;
        proxy_set_header X-CLI-Token "$ct";
        proxy_set_header X-Init-Data "";
        proxy_read_timeout 30s;
    }

    # смена пароля панели (локальный сервис, тоже за Basic Auth)
    location = /$wa_path/passwd {
        limit_req zone=wa burst=4 nodelay;
        auth_basic "restricted";
        auth_basic_user_file /etc/xvpn-webadmin/.htpasswd;
        proxy_pass http://127.0.0.1:9444/;
        proxy_http_version 1.1;
        proxy_set_header Host 127.0.0.1;
        proxy_read_timeout 15s;
    }
}
EOF
  ln -sf /etc/nginx/sites-available/xvpn-webadmin /etc/nginx/sites-enabled/xvpn-webadmin
  nginx -t || die "Ошибка в конфиге nginx"
  systemctl enable --now nginx >/dev/null 2>&1; systemctl reload nginx
  if ufw status 2>/dev/null | grep -q "Status: active"; then ufw allow "$WA_PORT"/tcp >/dev/null; fi

  chmod 700 "$ETC" 2>/dev/null; cat >"$ETC/webadmin.env" <<EOF
WA_PORT='$WA_PORT'
WA_PATH='$wa_path'
WA_USER='admin'
WA_PASS='$wa_pass'
EOF
  chmod 600 "$ETC/webadmin.env"
  echo
  ok "Веб-панель готова:"
  echo "  адрес:  https://$(url_host "$SERVER_IP"):$WA_PORT/$wa_path/"
  echo "  логин:  admin"
  echo "  пароль: $wa_pass"
  echo "  (доступы также в $ETC/webadmin.env — команда «xvpn webadmin» покажет их снова)"
}

set_env() { # set_env КЛЮЧ ЗНАЧЕНИЕ — записать в node.env
  if grep -q "^$1=" "$ETC/node.env"; then sed -i "s|^$1=.*|$1='$2'|" "$ETC/node.env"
  else echo "$1='$2'" >>"$ETC/node.env"; fi
}

# Вход через российский CDN (Yandex Cloud CDN, VK Cloud, Selectel…):
# клиент → CDN_HOST (адрес CDN, обычно в белых списках) → ORIGIN_HOST (этот сервер, Let's Encrypt) → Xray XHTTP.
# Российские CDN режут POST, поэтому в ссылках packet-up с отправкой методом GET (данные в заголовках).
do_rucdn() {
  [[ -f "$ETC/node.env" ]] || die "Сначала установите xvpn (server или node)"
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  [[ "${ROLE:-exit}" == exit ]] || die "Российский CDN подключают к серверу-выходу, а не к мосту"
  if [[ "${2:-}" == off || "${ARG2:-}" == off ]]; then
    set_env CDN_HOST ""; set_env ORIGIN_HOST ""
    configure_exit >/dev/null; write_nginx
    ok "Вход через российский CDN выключен. На главном сервере: xvpn sync"
    return
  fi
  echo
  c "Нужны два имени в вашем DNS:"
  echo "  • источник — A-запись на этот сервер ($SERVER_IP), БЕЗ проксирования Cloudflare (серое облако),"
  echo "    например origin.$( [[ -n "$DOMAIN" ]] && echo "${DOMAIN#*.}" || echo ваш-домен )"
  echo "  • адрес CDN для клиентов — CNAME на CDN-ресурс, тоже без проксирования, например cdn.ваш-домен"
  ask ORIGIN_HOST "Имя источника" "${ORIGIN_HOST:-}"
  ask CDN_HOST "Адрес CDN для клиентов" "${CDN_HOST:-}"
  local got
  got=$(getent ahostsv4 "$ORIGIN_HOST" | awk 'NR==1{print $1}')
  [[ "$got" == "$SERVER_IP" ]] || die "$ORIGIN_HOST указывает на ${got:-ничто}, а должен на $SERVER_IP (A-запись, серое облако)"
  ok "$ORIGIN_HOST → $SERVER_IP"
  set_env ORIGIN_HOST "$ORIGIN_HOST"; set_env CDN_HOST "$CDN_HOST"
  command -v certbot >/dev/null || { c "certbot…"; apt-get install -y -qq certbot >/dev/null; }
  open_firewall
  write_nginx  # порт 80 с /.well-known для проверки Let's Encrypt
  if [[ ! -s "/etc/letsencrypt/live/$ORIGIN_HOST/fullchain.pem" ]]; then
    c "Сертификат Let's Encrypt для $ORIGIN_HOST…"
    certbot certonly --webroot -w /var/www/xvpn-acme -d "$ORIGIN_HOST" --non-interactive --agree-tos \
      --register-unsafely-without-email --deploy-hook "systemctl reload nginx" \
      || die "Let's Encrypt не выдал сертификат: проверьте, что порт 80 открыт и запись без проксирования"
  fi
  configure_exit >/dev/null
  write_nginx
  systemctl restart xvpn-agent
  ok "Источник готов: https://$ORIGIN_HOST$XHTTP_PATH/"
  echo
  c "Настройки CDN-ресурса:"
  echo "  источник: $ORIGIN_HOST, протокол HTTPS, заголовок Host: $ORIGIN_HOST"
  echo "  доменное имя: $CDN_HOST (+ TLS-сертификат для него), кеширование ВЫКЛЮЧЕНО"
  echo "  разрешённые методы: GET, HEAD, OPTIONS; сжатие и сегментация — выключены"
  echo
  echo "На главном сервере выполните «xvpn sync» — в подписках появятся ссылки «РФ-CDN»."
  echo "Проверка источника: curl -s -o /dev/null -w '%{http_code}\n' https://$ORIGIN_HOST$XHTTP_PATH/ (ждём 404 или 400)"
}

run_mode() {
  case "$MODE" in
    server) do_server ;;
    node) do_node ;;
    bridge) do_bridge ;;
    update) do_update ;;
    code) show_code ;;
    rucdn) do_rucdn "$@" ;;
    webadmin) install_webadmin ;;
    balancer) install_balancer ;;
    canary) install_canary ;;
    uninstall) do_uninstall 0 ;;
    reinstall)
      installed || die "xvpn не установлен — выберите установку в меню"
      local was; was=$(current_role)
      if [[ "${YES:-}" != 1 ]]; then
        local yn
        warn "Будут удалены ключи и настройки ($(role_name "$was")). Резервная копия сохранится в /root."
        read -rp "Переустановить начисто? [д/Н]: " yn </dev/tty
        [[ "$yn" =~ ^[YyДд] ]] || die "Отменено"
      fi
      do_uninstall 1
      case "$was" in main) MODE=server ;; exit) MODE=node ;; bridge) MODE=bridge ;; *) menu ;; esac
      run_mode ;;
  esac
}
ARG2="${2:-}"
run_mode "$@"
