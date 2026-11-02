#!/usr/bin/env python3
"""xvpn webadmin: смена пароля Basic Auth.

Слушает только 127.0.0.1:9444. Перед сервисом стоит nginx с Basic Auth
и секретным путём, так что сюда попадают только авторизованные запросы.
POST / {"user": "...", "old": "...", "new": "..."} -> {"ok": true} | {"ok": false, "err": "..."}
"""
import json
import os
import re
import shlex
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HTPASSWD = "/etc/xvpn-webadmin/.htpasswd"
ENV_FILE = "/etc/xvpn/webadmin.env"


def verify(user: str, password: str) -> bool:
    return subprocess.run(
        ["htpasswd", "-vb", HTPASSWD, user, password],
        capture_output=True,
    ).returncode == 0


def update_env(user: str, password: str) -> None:
    """Обновить WA_USER/WA_PASS в webadmin.env, если файл есть. Файл читают через source."""
    try:
        if not os.path.exists(ENV_FILE):
            return
        with open(ENV_FILE, encoding="utf-8") as f:
            s = f.read()
        s = re.sub(r"^WA_USER=.*$", lambda _: "WA_USER=%s" % shlex.quote(user), s, flags=re.M)
        s = re.sub(r"^WA_PASS=.*$", lambda _: "WA_PASS=%s" % shlex.quote(password), s, flags=re.M)
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(s)
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # тихий
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 4096:
                return self._json(400, {"ok": False, "err": "Некорректный запрос"})
            data = json.loads(self.rfile.read(length) or b"{}")
            user = str(data.get("user", ""))
            old = str(data.get("old", ""))
            new = str(data.get("new", ""))
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", user):
                return self._json(400, {"ok": False, "err": "Недопустимый логин"})
            if not 10 <= len(new) <= 128:
                return self._json(400, {"ok": False, "err": "Новый пароль: от 10 до 128 символов"})
            if new == old:
                return self._json(400, {"ok": False, "err": "Новый пароль совпадает со старым"})
            if not verify(user, old):
                time.sleep(2)  # против перебора
                return self._json(403, {"ok": False, "err": "Старый пароль неверный"})
            r = subprocess.run(
                ["htpasswd", "-bi", HTPASSWD, user],
                input=new.encode(),
                capture_output=True,
            )
            if r.returncode != 0:
                return self._json(500, {"ok": False, "err": "Не удалось записать пароль"})
            update_env(user, new)
            return self._json(200, {"ok": True})
        except Exception:
            return self._json(400, {"ok": False, "err": "Некорректный запрос"})


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 9444), Handler).serve_forever()
