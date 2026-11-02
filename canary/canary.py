#!/usr/bin/env python3
"""xvpn-canary — канареечный мониторинг кластера и маскировки.

Каждые 5 минут (systemd-таймер) проверяет то, что видит клиент и РКН:
  1. Живость нод по данным панели (online / xray active).
  2. Маскировку мостов: TLS-рукопожатие с SNI кластера должно вернуть
     валидный публичный сертификат маскировочного сайта (Reality-dest жив).
     Если dest сломался или IP заблокирован — прилёт алерт.
  3. DNS кластера: все живые мосты должны быть в A-записях; мёртвые — не должны.
     Если задан CF_TOKEN/CF_ZONE в canary.env — мёртвые записи удаляются
     автоматически, иначе приходит алерт с инструкцией.
  4. Подписка: основной URL и РФ-CDN отвечают (не 5xx/таймаут).

Алерты — в Telegram владельцу, только при смене состояния (упал/поднялся).
Состояние: /etc/xvpn/canary.json. Настройки: /etc/xvpn/canary.env.
"""
import json
import os
import socket
import ssl
import time
import urllib.request

ETC = "/etc/xvpn"
STATE_FILE = f"{ETC}/canary.json"


def load_env(path):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'").strip('"')
    except FileNotFoundError:
        pass
    return out


panel = load_env(f"{ETC}/panel.env")
conf = load_env(f"{ETC}/canary.env")
CLI_TOKEN = panel.get("CLI_TOKEN", "")
BOT_TOKEN = panel.get("BOT_TOKEN", "")
OWNER_ID = panel.get("OWNER_ID", "")
DOMAIN = panel.get("DOMAIN", "")
SUB_PATH = panel.get("SUB_PATH", "")
API = f"http://127.0.0.1:8080/{panel.get('PANEL_PATH', '')}/api"

CF_TOKEN = conf.get("CF_TOKEN", "")
CF_ZONE = conf.get("CF_ZONE", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def api(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-CLI-Token": CLI_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def notify(text):
    print("notify:", text)
    if DRY_RUN or not BOT_TOKEN or not OWNER_ID:
        return
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=json.dumps({"chat_id": OWNER_ID, "text": text}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print("telegram notify failed:", e)


def tls_cert_ok(ip: str, sni: str, timeout=8) -> tuple[bool, str]:
    """Рукопожатие с SNI маскировки: Reality должен прозрачно отдать
    настоящий сертификат dest-сайта (проверяем цепочку и имя)."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ip, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=sni) as s:
                cert = s.getpeercert()
                san = [v for t, v in cert_sans(cert)]
                base = sni.split(".")[-2:]
                return True, ",".join(san)[:60] or str(base)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def cert_sans(cert):
    for t, v in cert.get("subjectAltName", ()):
        yield t, v


def dns_addrs(host: str) -> list[str]:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, 443, socket.AF_INET)})
    except Exception:
        return []


def cf_api(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {CF_TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    if not d.get("success"):
        raise RuntimeError(str(d.get("errors"))[:200])
    return d.get("result")


def cf_fix_dns(host: str, dead: list[str]) -> str:
    """Удаляет A-записи кластера, указывающие на мёртвые IP. Возвращает отчёт."""
    recs = cf_api("GET", f"/zones/{CF_ZONE}/dns_records?type=A&name={host}") or []
    done = []
    for r in recs:
        if r["content"] in dead:
            cf_api("DELETE", f"/zones/{CF_ZONE}/dns_records/{r['id']}")
            done.append(r["content"])
    return ", ".join(done)


def http_status(url: str, timeout=10) -> int:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "xvpn-canary"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def main():
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {}
    fails = state.setdefault("fails", {})  # имя проверки -> текст проблемы

    def report(name, problem):
        """problem=None — всё хорошо; строка — описание сбоя. Шлём только изменения."""
        old = fails.get(name)
        if problem and problem != "SKIP":
            if not old:
                notify(f"🔴 Канарейка: {problem}")
                fails[name] = problem
        elif old:
            notify(f"🟢 Канарейка: восстановилось — {name}")
            fails.pop(name, None)

    ov = api("GET", "/overview")
    servers = ov.get("servers") or []
    cl = ov.get("cluster")
    now = int(time.time())

    # 1. живость нод
    for s in servers:
        if not s.get("enabled", 1):
            continue
        up = bool(s.get("online") and (s.get("status") or {}).get("xray", {}).get("active"))
        report(f"node:{s['id']}", None if up else
               f"нода «{s['name']}» ({s['role']}) не на связи или Xray лёг")

    # 2. маскировка мостов: TLS с SNI кластера (или собственным) должен вернуть
    #    валидный сертификат маскировочного сайта
    bridges = [s for s in servers if s["role"] == "bridge" and s.get("enabled", 1)]
    cl_sni = None
    for s in bridges:
        info = s.get("info") or {}
        ip = info.get("ip")
        if not ip:
            continue
        sni = cl_sni or (info.get("reality") or {}).get("sni_tcp", "")
        if cl and not cl_sni:
            cl_sni = sni  # у всех мостов в кластере один SNI
        if not sni:
            continue
        ok, det = tls_cert_ok(ip, sni)
        report(f"mask:{ip}", None if ok else
               f"маскировка моста «{s['name']}» ({ip}) сломана: {det}. "
               f"Проверь, что {sni} отвечает по TLS, или смени SNI")

    # 3. DNS кластера против живых мостов
    if cl:
        host = cl.get("host", "")
        got = set(dns_addrs(host))
        live = {s["info"]["ip"] for s in bridges
                if s.get("online") and (s.get("info") or {}).get("ip")}
        dead_known = set(state.get("dns_extra", []))  # дополнительные IP в DNS (не ноды панели)
        missing = live - got
        if missing:
            report("dns:missing",
                   f"в DNS {host} нет живых мостов: {', '.join(sorted(missing))}. "
                   f"Добавь A-записи, иначе клиенты их не увидят")
        else:
            report("dns:missing", None)
        # мёртвые IP в DNS: не принадлежат живым мостам и не отвечают по маскировке
        suspicious = []
        for ip in got - live:
            if ip in dead_known:  # уже проверяли — известный доп. адрес, просто следим
                ok, _ = tls_cert_ok(ip, cl_sni or "ya.ru")
                if not ok:
                    suspicious.append(ip)
            else:
                ok, _ = tls_cert_ok(ip, cl_sni or "ya.ru")
                if not ok:
                    suspicious.append(ip)
        if suspicious:
            fixed = ""
            if CF_TOKEN and CF_ZONE and not DRY_RUN:
                try:
                    fixed = " Автоматически убрал из DNS: " + cf_fix_dns(host, suspicious)
                except Exception as e:
                    fixed = f" Автоудаление не сработало: {e}"
            report("dns:dead",
                   f"в DNS {host} висят мёртвые адреса: {', '.join(sorted(suspicious))} — "
                   f"клиенты будут долбиться в них впустую.{fixed or ' Удали A-записи.'}")
        else:
            report("dns:dead", None)

    # 4. подписка отвечает (400/404 — норм: сервис жив, просто нет токена)
    if DOMAIN and SUB_PATH:
        st = http_status(f"https://{DOMAIN}{SUB_PATH}/canary")
        report("sub:main", None if st in (400, 401, 404) else
               f"подписка https://{DOMAIN} не отвечает (код {st})")
        ru = ((ov.get("servers") or [{}])[0].get("info") or {})  # ищем РФ-CDN у локальной ноды
        rc_host = ""
        for s in servers:
            rc = (s.get("info") or {}).get("ru_cdn") or {}
            if rc.get("host"):
                rc_host = rc["host"]
                break
        if rc_host:
            st = http_status(f"https://{rc_host}{SUB_PATH}/canary")
            report("sub:rucdn", None if st in (400, 401, 404) else
                   f"подписка через РФ-CDN ({rc_host}) не отвечает (код {st})")

    state["ts"] = now
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)
    print("fails:", len(fails), "·", "; ".join(fails) if fails else "всё живо")


if __name__ == "__main__":
    main()
