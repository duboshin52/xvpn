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
import contextlib
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
SUB_PATH = "/" + panel.get("SUB_PATH", "").lstrip("/")
API = f"http://127.0.0.1:8080/{panel.get('PANEL_PATH', '')}/api"

CF_TOKEN = conf.get("CF_TOKEN", "")
CF_ZONE = conf.get("CF_ZONE", "")
HEARTBEAT_URL = conf.get("HEARTBEAT_URL", "")  # внешний сторож: молчание = главный умер
DEAD_AFTER = int(conf.get("DEAD_AFTER", 3))    # столько прогонов подряд IP мёртв — можно удалять
MIN_DNS = int(conf.get("MIN_DNS", 2))          # меньше стольких A-записей автоматика не оставит
PATH_AFTER = int(conf.get("PATH_AFTER", 3))    # столько прогонов подряд путь мёртв — алерт
STALLED_LIMIT = int(conf.get("STALLED_LIMIT", 10))  # свежих рукопожатий без ответа на мосту — алерт
RESTART_GAP = int(conf.get("RESTART_GAP", 1800))    # автоперезапуск Xray на мосту не чаще, секунд
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
    """Удаляет не больше одной мёртвой A-записи за прогон, оставляя минимум MIN_DNS.

    Проверка идёт с одной точки: если все мосты разом «мертвы», вероятнее сбой
    у главного или на маршруте, а клиенты в это время могут работать."""
    recs = cf_api("GET", f"/zones/{CF_ZONE}/dns_records?type=A&name={host}") or []
    if len(recs) - 1 < MIN_DNS:
        return ""
    for r in recs:
        if r["content"] in dead:
            cf_api("DELETE", f"/zones/{CF_ZONE}/dns_records/{r['id']}")
            return r["content"]
    return ""


def http_status(url: str, timeout=10) -> int:
    try:
        # браузерный UA: Cloudflare Bot Fight Mode отдаёт 403 на небраузерные,
        # клиентам подписки это не мешает, а мониторингу — да
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"})
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

    def save():
        state["ts"] = int(time.time())
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)

    try:
        ov = api("GET", "/overview")
    except Exception as e:
        # панель лежит: остальные проверки невозможны, но молчать нельзя
        report("panel", f"панель главного сервера не отвечает: {type(e).__name__}: {e}")
        save()
        return
    report("panel", None)
    servers = ov.get("servers") or []
    cl = ov.get("cluster")
    now = int(time.time())
    mask = state.setdefault("mask", {})  # ip -> {"ok": bool, "ts": ts} — маскировка по TLS
    paths = state.setdefault("paths", {})  # мост:путь -> сколько прогонов подряд мёртв
    stalls = state.setdefault("stalls", {})  # id моста -> сколько прогонов подряд выше порога
    mask_fails = state.setdefault("mask_fails", {})  # id моста -> сколько прогонов подряд TLS не прошёл
    restarts = state.setdefault("restarts", {})  # id моста -> когда перезапускали Xray
    watched = state.setdefault("watchdog", {})  # id моста -> последний перезапуск сторожем, о котором сообщили

    def heal(s, why):
        """Xray со временем зависает на рукопожатиях с отдельных адресов, перезапуск это снимает."""
        sid = str(s["id"])
        if DRY_RUN or now - restarts.get(sid, 0) < RESTART_GAP:
            return
        restarts[sid] = now
        try:
            api("POST", f"/servers/{s['id']}/restart")
            notify(f"🔁 Канарейка: перезапустила Xray на мосту «{s['name']}»: {why}")
        except Exception as e:
            notify(f"🔴 Канарейка: не смогла перезапустить Xray на мосту «{s['name']}»: {e}")

    # 1. живость нод
    for s in servers:
        if not s.get("enabled", 1):
            continue
        up = bool(s.get("online") and (s.get("status") or {}).get("xray", {}).get("active"))
        report(f"node:{s['id']}", None if up else
               f"нода «{s['name']}» ({s['role']}) не на связи или Xray лёг")
        # рукопожатия без ответа: клиент «подключён», но интернета нет (молчит сайт маскировки)
        stalled = (s.get("status") or {}).get("stalled")
        if up and stalled is not None:
            sid = str(s["id"])
            stalls[sid] = stalls.get(sid, 0) + 1 if stalled >= STALLED_LIMIT else 0
            report(f"stall:{sid}", f"на мосту «{s['name']}» {stalled} рукопожатий без ответа: "
                                   f"клиенты подключены, но интернета нет. Похоже, молчит сайт маскировки"
                   if stalls[sid] >= 2 else None)
            if stalls[sid] >= 2:
                heal(s, f"{stalled} рукопожатий без ответа")
        # сторож на самом мосту перезапускает Xray сам — сообщаем о каждом новом перезапуске
        wd = (s.get("status") or {}).get("watchdog") or {}
        if wd.get("t") and wd["t"] != watched.get(str(s["id"])):
            watched[str(s["id"])] = wd["t"]
            notify(f"🔁 Сторож моста «{s['name']}» перезапустил Xray: {wd.get('stalled')} рукопожатий "
                   "без ответа" + ("" if wd.get("ok") else ", но перезапуск НЕ удался"))
        # путь мост → выход: сервер может быть жив, а маршрут из РФ к нему закрыт.
        # Единичные сбои одного из двух путей обычны: алерт — если путь мёртв PATH_AFTER
        # прогонов подряд или мертвы оба пути к выходу два прогона (после рестарта замеров нет)
        exits = (s.get("status") or {}).get("exits") or []
        for e in exits if up else []:
            key = f"{s['id']}:{e['tag']}"
            dead = paths[key] = 0 if e.get("alive") else paths.get(key, 0) + 1
            both = not any(x.get("alive") for x in exits if x.get("server_id") == e.get("server_id"))
            report(f"path:{key}", f"мост «{s['name']}» не видит выход {e.get('name', e['tag'])} "
                                  f"({e.get('proto', '?')})" if dead >= PATH_AFTER or (both and dead >= 2) else None)

    # 2. маскировка мостов: TLS с SNI кластера (или собственным) должен вернуть
    #    валидный сертификат маскировочного сайта
    bridges = [s for s in servers if s["role"] == "bridge" and s.get("enabled", 1)]
    cl_sni = None
    for s in bridges:
        info = s.get("info") or {}
        ip = info.get("ip")
        if not ip:
            continue
        own = (info.get("reality") or {}).get("sni_tcp", "")
        sni = own if info.get("standalone") else cl_sni or own
        if cl and not cl_sni and not info.get("standalone"):
            cl_sni = sni  # у всех мостов в кластере один SNI
        if not sni:
            continue
        ok, det = tls_cert_ok(ip, sni)
        mask[ip] = {"ok": ok, "ts": now}
        report(f"mask:{ip}", None if ok else
               f"маскировка моста «{s['name']}» ({ip}) сломана: {det}. "
               f"Проверь, что {sni} отвечает по TLS, или смени SNI")
        sid = str(s["id"])
        mask_fails[sid] = 0 if ok else mask_fails.get(sid, 0) + 1
        if mask_fails[sid] >= 2:
            heal(s, f"TLS не отвечает два прогона: {det}"[:120])

    # 3. DNS кластера против живых мостов
    if cl:
        host = cl.get("host", "")
        got = set(dns_addrs(host))
        # мост профиля «Мобильный» намеренно вне DNS кластера: клиенты идут на его IP
        live = {s["info"]["ip"] for s in bridges
                if s.get("online") and (s.get("info") or {}).get("ip") and s["name"] != ov.get("mobile_bridge")
                and not (s.get("info") or {}).get("standalone")}
        missing = live - got
        if missing:
            report("dns:missing",
                   f"в DNS {host} нет живых мостов: {', '.join(sorted(missing))}. "
                   f"Добавь A-записи, иначе клиенты их не увидят")
        else:
            report("dns:missing", None)
        # мёртвые IP в DNS: не принадлежат живым мостам и не отвечают по маскировке
        suspicious = []
        strikes = state.setdefault("dead", {})  # ip -> сколько прогонов подряд мёртв
        for ip in got - live:
            ok, _ = tls_cert_ok(ip, cl_sni or "ya.ru")
            mask[ip] = {"ok": ok, "ts": now}
            if not ok:
                suspicious.append(ip)
        state["dead"] = strikes = {ip: strikes.get(ip, 0) + 1 for ip in suspicious}
        if suspicious:
            fixed = ""
            ripe = [ip for ip in suspicious if strikes[ip] >= DEAD_AFTER]
            # мертвы все адреса — это не мосты упали, а связь главного: DNS не трогаем
            if ripe and len(suspicious) < len(got) and CF_TOKEN and CF_ZONE and not DRY_RUN:
                try:
                    gone = cf_fix_dns(host, ripe)
                    fixed = f" Автоматически убрал из DNS: {gone}" if gone else ""
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
        report("sub:main", None if st in (400, 401, 403, 404) else
               f"подписка https://{DOMAIN} не отвечает (код {st})")
        # публичный адрес подписки (прокси перед главным): через него же открывается мини-апп
        with contextlib.suppress(Exception):
            sub_host = api("GET", "/settings").get("sub_host", "")
            if sub_host:
                st = http_status(f"https://{sub_host}{SUB_PATH}/canary")
                report("sub:public", None if st in (400, 401, 403, 404) else
                       f"подписка https://{sub_host} не отвечает (код {st})")
        rc_host = ""
        for s in servers:
            rc = (s.get("info") or {}).get("ru_cdn") or {}
            if rc.get("host"):
                rc_host = rc["host"]
                break
        if rc_host:
            st = http_status(f"https://{rc_host}{SUB_PATH}/canary")
            report("sub:rucdn", None if st in (400, 401, 403, 404) else
                   f"подписка через РФ-CDN ({rc_host}) не отвечает (код {st})")

    save()
    if HEARTBEAT_URL and not DRY_RUN:
        try:
            urllib.request.urlopen(HEARTBEAT_URL, timeout=10).read()
        except Exception as e:
            print("heartbeat failed:", e)
    print("fails:", len(fails), "·", "; ".join(fails) if fails else "всё живо")


if __name__ == "__main__":
    main()
