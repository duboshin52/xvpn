#!/usr/bin/env python3
"""xvpn-balancer — ротация выходов каскада по трафику.

Лимит трафика на VPS (например, 2 ТБ у Vultr) распределяем по нодам:
каждые LIMIT_GB отданного трафика выход уходит на «отдых» (cascade=off),
вместо него включается самый отдохнувший из пула (cascade=on).
Одновременно активны PAIR выхода: рабочий + резерв (мост сам ведёт трафик
через самый быстрый живой путь). HARD_CAP_GB — страховка от перерасхода:
выход отключается принудительно. Счётчики сбрасываются RESET_DAY-го числа
каждого месяца (день начала биллинга VPS).

Запускается по systemd-таймеру на главном сервере. Состояние: /etc/xvpn/balancer.json.
Настройки: /etc/xvpn/balancer.env (LIMIT_GB, HARD_CAP_GB, PAIR, RESET_DAY).
"""
import json
import os
import time
import urllib.request

ETC = "/etc/xvpn"
STATE_FILE = f"{ETC}/balancer.json"


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
conf = load_env(f"{ETC}/balancer.env")
PANEL_PATH = panel["PANEL_PATH"]
CLI_TOKEN = panel["CLI_TOKEN"]
BOT_TOKEN = panel.get("BOT_TOKEN", "")
OWNER_ID = panel.get("OWNER_ID", "")

LIMIT = int(float(conf.get("LIMIT_GB", 200)) * 1024**3)
HARD_CAP = int(float(conf.get("HARD_CAP_GB", 1800)) * 1024**3)
PAIR = int(conf.get("PAIR", 2))
RESET_DAY = int(conf.get("RESET_DAY", 1))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

API = f"http://127.0.0.1:8080/{PANEL_PATH}/api"


def api(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-CLI-Token": CLI_TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def gb(n):
    return f"{n / 1024**3:.1f} ГБ"


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


def main():
    try:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        state = {}
    net = state.setdefault("net", {})       # sid -> последнее значение счётчика
    served = state.setdefault("served", {})  # sid -> байты с начала месяца

    # месячный сброс счётчиков (день начала биллинга VPS)
    month = time.strftime("%Y-%m")
    if state.get("month") != month and time.localtime().tm_mday >= RESET_DAY:
        served.clear()
        state["month"] = month
        notify("Балансировщик: начался новый биллинговый месяц, счётчики трафика нод обнулены.")
    state.setdefault("month", month)

    ov = api("GET", "/overview")
    pool = [s for s in ov["servers"] if s["role"] == "exit" and s.get("enabled", 1)]

    # дельты трафика с прошлого запуска
    for s in pool:
        sid = str(s["id"])
        st = s.get("status") or {}
        if not (s.get("online") and st.get("xray", {}).get("active") and st.get("net")):
            continue
        now = int(st["net"]["sent"]) + int(st["net"]["recv"])
        prev = net.get(sid)
        if prev is not None and now >= prev:
            served[sid] = served.get(sid, 0) + (now - prev)
        net[sid] = now  # при перезагрузке сервера счётчик обнуляется — дельту не считаем

    working = [s for s in pool if s.get("cascade")]
    changed = False

    def set_cascade(s, val):
        nonlocal changed
        print(f"{'[dry] ' if DRY_RUN else ''}cascade {'on' if val else 'off'}: {s['name']}")
        if not DRY_RUN:
            api("PATCH", f"/servers/{s['id']}", {"cascade": val})
        s["cascade"] = val
        changed = True

    # страховка: жёсткий лимит — выключить, пока есть чем заменить
    for s in list(working):
        sid = str(s["id"])
        if served.get(sid, 0) >= HARD_CAP and len(working) > 1:
            set_cascade(s, 0)
            working.remove(s)
            notify(f"⛔️ Балансировщик: {s['name']} достиг жёсткого лимита {gb(HARD_CAP)} — отключён от каскада до конца месяца.")

    # плановая ротация по LIMIT_GB
    for s in list(working):
        sid = str(s["id"])
        if served.get(sid, 0) >= LIMIT and len(working) > 1:
            set_cascade(s, 0)
            working.remove(s)
            notify(f"🔄 Балансировщик: {s['name']} отдал {gb(served[sid])} — уходит на отдых.")

    # добираем рабочую пару из самых отдохнувших
    resting = [s for s in pool if not s.get("cascade") and served.get(str(s["id"]), 0) < LIMIT]
    resting.sort(key=lambda s: served.get(str(s["id"]), 0))
    target = min(PAIR, len(pool))
    while len(working) < target and resting:
        s = resting.pop(0)
        set_cascade(s, 1)
        working.append(s)
        notify(f"▶️ Балансировщик: {s['name']} подключён к каскаду (накоплено {gb(served.get(str(s['id']), 0))}).")

    # сокращаем до PAIR активных: выключаем самые нагруженные сверх пары
    if len(working) > target:
        working.sort(key=lambda s: served.get(str(s["id"]), 0))
        for s in working[target:]:
            set_cascade(s, 0)
            notify(f"⏸ Балансировщик: {s['name']} выведен из каскада — активных держим {target}.")
        working = working[:target]

    if changed and not DRY_RUN:
        api("POST", "/sync")

    state["ts"] = int(time.time())
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)

    print("working:", ", ".join(f"{s['name']} {gb(served.get(str(s['id']), 0))}" for s in working) or "—")


if __name__ == "__main__":
    main()
