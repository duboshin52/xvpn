#!/usr/bin/env python3
"""xvpn backup: ежедневный архив базы ключей и настроек главного сервера.

Кладёт в /root/xvpn-backups/xvpn-backup-ДАТА.tar.gz:
  - xvpn.db      — база панели (ключи, серверы, админы) через sqlite backup API
  - etc/xvpn/    — настройки (panel.env, backup.env, balancer.env…)
  - agent node.json — параметры этой ноды

Ротация: хранятся последние KEEP архивов (по умолчанию 14).
SEND_TG=1 — архив дополнительно отправляется Telegram-админам ботом панели.

Запускается по расписанию unit-таймером xvpn-backup.timer; вручную:
  python3 /opt/xvpn/backup/backup.py
Только стандартная библиотека — отдельное окружение не нужно.
"""
import glob
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.request

DB_PATH = os.environ.get("DB_PATH", "/opt/xvpn/panel/xvpn.db")
PANEL_ENV = os.environ.get("PANEL_ENV", "/etc/xvpn/panel.env")
NODE_JSON = os.environ.get("NODE_JSON", "/opt/xvpn/agent/node.json")
ETC_DIR = os.environ.get("XVPN_ETC", "/etc/xvpn")
OUT_DIR = os.environ.get("BACKUP_DIR", "/root/xvpn-backups")
KEEP = int(os.environ.get("KEEP", "14"))
SEND_TG = os.environ.get("SEND_TG", "0") == "1"


def read_env(path: str) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def make_archive() -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(OUT_DIR, f"xvpn-backup-{stamp}.tar.gz")
    tmpdir = tempfile.mkdtemp(prefix="xvpn-backup-")
    try:
        # согласованная копия БД даже при работающей панели
        if os.path.exists(DB_PATH):
            db_copy = os.path.join(tmpdir, "xvpn.db")
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(db_copy)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        with tarfile.open(out, "w:gz") as tar:
            db_copy = os.path.join(tmpdir, "xvpn.db")
            if os.path.exists(db_copy):
                tar.add(db_copy, arcname="opt/xvpn/panel/xvpn.db")
            if os.path.isdir(ETC_DIR):
                tar.add(ETC_DIR, arcname="etc/xvpn")
            if os.path.exists(NODE_JSON):
                tar.add(NODE_JSON, arcname="opt/xvpn/agent/node.json")
        os.chmod(out, 0o600)
    finally:
        for f in glob.glob(os.path.join(tmpdir, "*")):
            os.unlink(f)
        os.rmdir(tmpdir)
    return out


def rotate():
    if KEEP <= 0:  # 0 — не удалять старые
        return
    archives = sorted(glob.glob(os.path.join(OUT_DIR, "xvpn-backup-*.tar.gz")))
    for old in archives[:-KEEP]:
        try:
            os.unlink(old)
            print(f"удалён старый архив: {old}")
        except OSError as e:
            print(f"не удалось удалить {old}: {e}", file=sys.stderr)


def send_telegram(path: str):
    env = read_env(PANEL_ENV)
    token = env.get("BOT_TOKEN", "")
    if not token or not os.path.exists(DB_PATH):
        return
    with sqlite3.connect(DB_PATH) as db:
        admins = [r[0] for r in db.execute("SELECT tg_id FROM admins")]
    if not admins:
        return
    size = os.path.getsize(path)
    caption = f"Резервная копия xvpn за {time.strftime('%d.%m.%Y %H:%M')} ({size // 1024} КБ)"
    boundary = "xvpnbackup" + str(int(time.time()))
    for tg_id in admins:
        try:
            with open(path, "rb") as f:
                data = f.read()
            body = b"\r\n".join([
                f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{tg_id}'.encode(),
                f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}'.encode(),
                f"--{boundary}".encode(),
                f'Content-Disposition: form-data; name="document"; filename="{os.path.basename(path)}"\r\n'
                f"Content-Type: application/gzip\r\n\r\n".encode() + data,
                f"--{boundary}--".encode(),
            ])
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendDocument", data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            urllib.request.urlopen(req, timeout=60).read()
        except Exception as e:  # не роняем бэкап из-за Telegram
            print(f"telegram {tg_id}: {e}", file=sys.stderr)


def main():
    out = make_archive()
    print(f"архив: {out} ({os.path.getsize(out) // 1024} КБ)")
    rotate()
    if SEND_TG:
        send_telegram(out)


if __name__ == "__main__":
    main()
