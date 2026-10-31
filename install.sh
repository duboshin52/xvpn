#!/usr/bin/env bash
# xvpn — Xray (VLESS: XHTTP via Cloudflare CDN + Reality + XHTTP-Reality) с Telegram-ботом и мини-аппом.
#   bash install.sh panel   — главный сервер: Xray + агент + бот/панель
#   bash install.sh node    — дополнительный сервер: Xray + агент (подключается в панели)
#   bash install.sh update  — обновить код, Xray и перезапустить сервисы
#   bash install.sh info    — показать данные для подключения ноды
# Все вопросы можно заранее задать переменными окружения (DOMAIN, BOT_TOKEN, OWNER_ID, CERT_FILE, KEY_FILE, SNI_TCP, SNI_XHTTP, NODE_NAME).
set -Eeuo pipefail
trap 'printf "\033[1;31m✘ Ошибка в строке %s: %s\033[0m\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT=/opt/xvpn
ETC=/etc/xvpn
MODE="${1:-}"

c() { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
rnd() { local s; s=$(openssl rand -hex 32); echo "${s:0:${1:-12}}"; }
ask() { # ask VAR "вопрос" [default]
  local var=$1 q=$2 def=${3:-} val
  if [[ -n "${!var:-}" ]]; then return; fi
  read -rp "$q${def:+ [$def]}: " val </dev/tty
  printf -v "$var" '%s' "${val:-$def}"
  [[ -n "${!var}" ]] || die "$var обязателен"
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

[[ $EUID -eq 0 ]] || die "Запустите от root"
[[ "$MODE" =~ ^(panel|node|update|info)$ ]] || die "Использование: bash install.sh panel|node|update|info"

# Запуск одной командой через curl: скачиваем исходники из репозитория и перезапускаемся из них
REPO="${XVPN_REPO:-duboshin52/xvpn}"
BRANCH="${XVPN_BRANCH:-main}"
if [[ ! -f "$SRC/agent/agent.py" ]]; then
  command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }
  c "Скачиваю xvpn из github.com/$REPO…"
  rm -rf "$ROOT/src" && mkdir -p "$ROOT/src"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$BRANCH" | tar xz -C "$ROOT/src" --strip-components=1 \
    || die "Не удалось скачать репозиторий $REPO"
  exec bash "$ROOT/src/install.sh" "$@"
fi

show_info() {
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  echo
  c "Данные ноды для панели (вкладка «Серверы» → Добавить):"
  echo "  URL агента: https://$DOMAIN/$AGENT_PATH"
  echo "  Секрет:     $AGENT_SECRET"
}
if [[ "$MODE" == info ]]; then show_info; exit 0; fi

install_packages() {
  c "Пакеты…"
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
  apt-get update -qq
  apt-get install -y -qq curl openssl jq nginx libnginx-mod-stream python3 python3-venv python3-pip ca-certificates >/dev/null
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
  python3 -m venv "$dir/venv"
  "$dir/venv/bin/pip" install -q --upgrade pip
  "$dir/venv/bin/pip" install -q "$@"
}

copy_code() {
  mkdir -p "$ROOT/agent" "$ROOT/panel/webapp"
  cp "$SRC/agent/agent.py" "$ROOT/agent/"
  if [[ -f "$ETC/panel.env" ]]; then
    cp "$SRC/panel/app.py" "$ROOT/panel/"
    cp "$SRC/panel/webapp/index.html" "$ROOT/panel/webapp/"
  fi
}

cf_api() { # cf_api METHOD path [json]
  curl -sS -X "$1" "https://api.cloudflare.com/client/v4$2" \
    -H "Authorization: Bearer $CF_API_TOKEN" -H "Content-Type: application/json" ${3:+--data "$3"}
}

cf_setup() {
  c "Настраиваю Cloudflare…"
  local zone="" zid="" d="$DOMAIN" r rid csr
  # ищем зону: sub.example.com → example.com
  while [[ "$d" == *.* ]]; do
    zid=$(cf_api GET "/zones?name=$d" | jq -r '.result[0].id // empty')
    [[ -n "$zid" ]] && { zone=$d; break; }
    d=${d#*.}
  done
  [[ -n "$zid" ]] || die "Зона для $DOMAIN не найдена в Cloudflare (проверьте токен и что домен добавлен в аккаунт)"
  ok "Зона $zone"

  # A-запись с проксированием
  local body
  body=$(jq -nc --arg n "$DOMAIN" --arg ip "$SERVER_IP" '{type:"A",name:$n,content:$ip,proxied:true,ttl:1}')
  rid=$(cf_api GET "/zones/$zid/dns_records?type=A&name=$DOMAIN" | jq -r '.result[0].id // empty')
  if [[ -n "$rid" ]]; then r=$(cf_api PUT "/zones/$zid/dns_records/$rid" "$body")
  else r=$(cf_api POST "/zones/$zid/dns_records" "$body"); fi
  [[ $(jq -r .success <<<"$r") == true ]] || die "DNS: $(jq -c .errors <<<"$r")"
  ok "DNS: $DOMAIN → $SERVER_IP (через Cloudflare)"

  r=$(cf_api PATCH "/zones/$zid/settings/ssl" '{"value":"strict"}')
  [[ $(jq -r .success <<<"$r") == true ]] && ok "SSL/TLS: Full (strict)" \
    || echo "⚠ SSL Full (strict) не включился: $(jq -c .errors <<<"$r") — включите вручную"
  r=$(cf_api PATCH "/zones/$zid/settings/grpc" '{"value":"on"}')
  [[ $(jq -r .success <<<"$r") == true ]] && ok "gRPC: On" \
    || echo "⚠ gRPC не включился через API — включите вручную (Network → gRPC), это не критично"

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

configure_node() {
  mkdir -p "$ETC" && chmod 700 "$ETC"
  if [[ -f "$ETC/node.env" ]]; then
    c "Найдена существующая конфигурация ноды — используем её."
    # shellcheck disable=SC1091
    source "$ETC/node.env"
  else
    ask DOMAIN "Домен этого сервера (A-запись в Cloudflare, оранжевое облако)"
    ask SNI_TCP "SNI для Reality (чужой сайт с TLS1.3/H2, лучше рядом с VPS)" "www.samsung.com"
    ask SNI_XHTTP "SNI для XHTTP-Reality (другой сайт)" "www.nvidia.com"
    ask NODE_NAME "Название сервера" "$(hostname -s)"
    local keys
    keys=$(/usr/local/bin/xray x25519)
    # формат вывода менялся между версиями: берём значения первых двух строк
    REALITY_PRIV=$(echo "$keys" | sed -n 1p | awk -F': *' '{print $2}')
    REALITY_PUB=$(echo "$keys" | sed -n 2p | awk -F': *' '{print $2}')
    [[ -n "$REALITY_PRIV" && -n "$REALITY_PUB" ]] || die "Не удалось сгенерировать ключи Reality"
    SHORT_ID=$(openssl rand -hex 8)
    XHTTP_PATH="/$(rnd 10)"
    RXHTTP_PATH="/$(rnd 10)"
    AGENT_PATH="a-$(rnd 16)"
    AGENT_SECRET=$(rnd 40)
    SERVER_IP=$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me)
    cat >"$ETC/node.env" <<EOF
DOMAIN='$DOMAIN'
SNI_TCP='$SNI_TCP'
SNI_XHTTP='$SNI_XHTTP'
NODE_NAME='$NODE_NAME'
REALITY_PRIV='$REALITY_PRIV'
REALITY_PUB='$REALITY_PUB'
SHORT_ID='$SHORT_ID'
XHTTP_PATH='$XHTTP_PATH'
RXHTTP_PATH='$RXHTTP_PATH'
AGENT_PATH='$AGENT_PATH'
AGENT_SECRET='$AGENT_SECRET'
SERVER_IP='$SERVER_IP'
EOF
    chmod 600 "$ETC/node.env"
  fi

  # сертификат Cloudflare Origin
  if [[ ! -s "$ETC/cert.pem" || ! -s "$ETC/key.pem" ]]; then
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
  fi

  mkdir -p "$ROOT/agent"
  jq -n --arg d "$DOMAIN" --arg ip "$SERVER_IP" --arg xp "$XHTTP_PATH" --arg s "$AGENT_SECRET" \
        --arg pk "$REALITY_PRIV" --arg pub "$REALITY_PUB" --arg sid "$SHORT_ID" \
        --arg st "$SNI_TCP" --arg sx "$SNI_XHTTP" --arg rxp "$RXHTTP_PATH" \
    '{domain:$d, ip:$ip, xhttp_path:$xp, agent_secret:$s,
      reality:{private_key:$pk, public_key:$pub, short_id:$sid, sni_tcp:$st, sni_xhttp:$sx, xhttp_path:$rxp}}' \
    >"$ROOT/agent/node.json"
  chmod 600 "$ROOT/agent/node.json"
}

write_nginx() {
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  local panel_locs=""
  if [[ -f "$ETC/panel.env" ]]; then
    # shellcheck disable=SC1091
    source "$ETC/panel.env"
    panel_locs="
    location /$PANEL_PATH/ { proxy_pass http://127.0.0.1:8080; include /etc/nginx/xvpn_proxy.conf; }
    location /$SUB_PATH/   { proxy_pass http://127.0.0.1:8080; include /etc/nginx/xvpn_proxy.conf; }"
  fi
  cat >/etc/nginx/xvpn_proxy.conf <<'EOF'
proxy_http_version 1.1;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto https;
proxy_read_timeout 60s;
EOF
  mkdir -p /etc/nginx/stream.d /var/www/xvpn
  # 443 делится по SNI: домен → nginx (Cloudflare/XHTTP/панель), SNI Reality → Xray
  cat >/etc/nginx/stream.d/xvpn.conf <<EOF
map \$ssl_preread_server_name \$xvpn_up {
    $DOMAIN    127.0.0.1:8443;
    $SNI_TCP   127.0.0.1:4431;
    $SNI_XHTTP 127.0.0.1:4432;
    default    127.0.0.1:4431;
}
server {
    listen 443;
    listen [::]:443;
    ssl_preread on;
    proxy_pass \$xvpn_up;
    proxy_connect_timeout 5s;
}
EOF
  grep -q 'stream.d' /etc/nginx/nginx.conf || \
    printf '\nstream {\n    include /etc/nginx/stream.d/*.conf;\n}\n' >>/etc/nginx/nginx.conf

  rm -f /etc/nginx/sites-enabled/default
  cat >/etc/nginx/sites-available/xvpn.conf <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name _;
    return 301 https://\$host\$request_uri;
}
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
        grpc_pass unix:/dev/shm/xvpn-xhttp.sock;
        grpc_buffer_size 16k;
        grpc_socket_keepalive on;
        grpc_read_timeout 1h;
        grpc_send_timeout 1h;
        grpc_set_header Connection "";
        grpc_set_header X-Real-IP \$remote_addr;
    }
    location /$AGENT_PATH/ { proxy_pass http://127.0.0.1:8081/; include /etc/nginx/xvpn_proxy.conf; }$panel_locs
    location / { try_files \$uri /index.html; }
}
EOF
  ln -sf /etc/nginx/sites-available/xvpn.conf /etc/nginx/sites-enabled/xvpn.conf
  [[ -f /var/www/xvpn/index.html ]] || cat >/var/www/xvpn/index.html <<'EOF'
<!doctype html><html><head><meta charset="utf-8"><title>Welcome</title>
<style>body{font-family:system-ui;max-width:640px;margin:80px auto;color:#333}</style></head>
<body><h1>Coming soon</h1><p>This site is under construction.</p></body></html>
EOF
  nginx -t 2>&1 | tail -2
  systemctl enable --now nginx >/dev/null 2>&1
  systemctl reload nginx || systemctl restart nginx
}

write_agent_service() {
  cat >/etc/systemd/system/xvpn-agent.service <<EOF
[Unit]
Description=xvpn node agent
After=network.target xray.service

[Service]
WorkingDirectory=$ROOT/agent
ExecStart=$ROOT/agent/venv/bin/uvicorn agent:app --host 127.0.0.1 --port 8081 --log-level warning
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

write_panel_service() {
  cat >/etc/systemd/system/xvpn-panel.service <<EOF
[Unit]
Description=xvpn panel + telegram bot
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
  # стартовый конфиг без пользователей (дальше его пишет агент)
  cd "$ROOT/agent"
  XVPN_AGENT_DIR="$ROOT/agent" "$ROOT/agent/venv/bin/python" -c \
    'import agent; agent.apply_users([])' || die "Конфиг Xray не прошёл проверку"
  systemctl enable xray >/dev/null 2>&1
  systemctl restart xray
}

configure_panel() {
  if [[ -f "$ETC/panel.env" ]]; then return; fi
  # shellcheck disable=SC1091
  source "$ETC/node.env"
  echo
  c "Telegram-бот: создайте его у @BotFather (/newbot) и вставьте токен."
  ask BOT_TOKEN "Токен бота"
  c "Ваш Telegram ID можно узнать у @userinfobot."
  ask OWNER_ID "Telegram ID владельца"
  [[ "$OWNER_ID" =~ ^[0-9]+$ ]] || die "OWNER_ID должен быть числом"
  ask SUB_TITLE "Название VPN в приложениях" "xvpn"
  cat >"$ETC/panel.env" <<EOF
BOT_TOKEN="$BOT_TOKEN"
OWNER_ID="$OWNER_ID"
DOMAIN="$DOMAIN"
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

case "$MODE" in
  node|panel)
    install_packages
    install_xray
    enable_bbr
    configure_node
    [[ "$MODE" == panel ]] && configure_panel
    copy_code
    c "Python-окружение агента…"
    setup_venv "$ROOT/agent" fastapi "uvicorn[standard]" psutil
    write_agent_service
    bootstrap_xray
    if [[ "$MODE" == panel ]]; then
      c "Python-окружение панели…"
      setup_venv "$ROOT/panel" fastapi "uvicorn[standard]" httpx "aiogram>=3.7,<4"
      write_panel_service
    fi
    write_nginx
    systemctl daemon-reload
    systemctl enable --now xvpn-agent >/dev/null 2>&1; systemctl restart xvpn-agent
    if [[ "$MODE" == panel ]]; then
      systemctl enable --now xvpn-panel >/dev/null 2>&1; systemctl restart xvpn-panel
    fi
    echo
    ok "Готово."
    echo "Проверьте в Cloudflare: SSL/TLS = Full (strict); Network → gRPC = On (для режима stream-up XHTTP)."
    if [[ "$MODE" == panel ]]; then
      echo "Откройте своего бота в Telegram и нажмите /start → «Открыть панель»."
    else
      show_info
    fi
    ;;
  update)
    [[ -f "$ETC/node.env" ]] || die "xvpn не установлен"
    install_xray
    configure_node
    copy_code
    "$ROOT/agent/venv/bin/pip" install -q --upgrade fastapi "uvicorn[standard]" psutil
    [[ -f "$ETC/panel.env" ]] && "$ROOT/panel/venv/bin/pip" install -q --upgrade fastapi "uvicorn[standard]" httpx "aiogram>=3.7,<4"
    write_nginx
    systemctl daemon-reload
    systemctl restart xray xvpn-agent
    [[ -f "$ETC/panel.env" ]] && systemctl restart xvpn-panel
    ok "Обновлено"
    ;;
esac
