"""WSGI endpoints for the Telegram Mini App."""

import hashlib
import hmac
import json
import logging
import time
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qsl

from store import Conflict


STATIC = Path(__file__).with_name("static")
ASSETS = {
    "/": ("index.html", "text/html"),
    "/static/app.js": ("app.js", "text/javascript"),
    "/static/style.css": ("style.css", "text/css"),
}
CSP = ("default-src 'none'; script-src 'self' https://telegram.org; "
       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; "
       "frame-src https://web.telegram.org https://oauth.telegram.org; form-action 'none'; base-uri 'none'")


def signed_user(header, token, now=None):
    if not token or not header.startswith("tma ") or len(header) > 16384:
        return None
    try:
        pairs = parse_qsl(header[4:], keep_blank_values=True, strict_parsing=True, max_num_fields=50)
        data = dict(pairs)
        if len(data) != len(pairs):
            return None
        supplied_hash = bytes.fromhex(data.pop("hash"))
        signed = "\n".join(f"{k}={data[k]}" for k in sorted(data)).encode()
        secret = hmac.digest(b"WebAppData", token.encode(), hashlib.sha256)
        if not hmac.compare_digest(hmac.digest(secret, signed, "sha256"), supplied_hash):
            return None
        age = (time.time() if now is None else now) - int(data["auth_date"])
        if age < -60 or age > 3600:
            return None
        user_id = json.loads(data["user"])["id"]
        return user_id if type(user_id) is int and user_id > 0 else None
    except (ValueError, KeyError, TypeError, RecursionError, UnicodeError):
        return None


class WebApp:
    def __init__(self, store, token, admin_ids, ready=lambda: True):
        self.store = store
        self.token = token
        self.admin_ids = admin_ids
        self.ready = ready

    def __call__(self, env, start_response):
        content_type = "application/json"
        path = env.get("PATH_INFO", "")
        method = env.get("REQUEST_METHOD", "GET")
        try:
            if path in ASSETS and method in ("GET", "HEAD"):
                filename, content_type = ASSETS[path]
                body = (STATIC / filename).read_bytes()
                status = 200
            elif path == "/health" and method == "GET":
                status = 200 if self.ready() else 503
                body = {"ok": status == 200}
            elif path.startswith("/api/"):
                status, body = self.api(env, path, method)
            else:
                status, body = 404, {"error": "Страница не найдена."}
        except Conflict as exc:
            status, body = 409, {"error": str(exc)}
        except (KeyError, TypeError, RecursionError):
            status, body = 400, {"error": "Проверьте поля формы."}
        except ValueError as exc:
            status, body = 400, {"error": str(exc)}
        except Exception:
            logging.exception("Mini App request failed")
            status, body = 500, {"error": "Не удалось обработать запрос. Повторите позже."}
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        start_response(f"{status} {HTTPStatus(status).phrase}", [
            ("Content-Type", content_type + "; charset=utf-8"), ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("Content-Security-Policy", CSP),
        ])
        return [b"" if method == "HEAD" else body]

    def api(self, env, path, method):
        user_id = signed_user(env.get("HTTP_AUTHORIZATION", ""), self.token)
        if user_id is None:
            return 401, {"error": "Сеанс истёк. Откройте настройки заново из бота."}
        if user_id not in self.admin_ids:
            return 403, {"error": "Настройки доступны только администраторам."}
        if path == "/api/state" and method == "GET":
            return 200, self.store.state()
        if method != "POST":
            return 405, {"error": "Метод не поддерживается."}
        if env.get("CONTENT_TYPE", "").split(";")[0].strip() != "application/json":
            return 415, {"error": "Ожидается форма в формате JSON."}
        try:
            size = int(env.get("CONTENT_LENGTH") or 0)
            if not 0 < size <= 8192:
                return 413, {"error": "Проверьте размер отправленных данных."}
            data = json.loads(env["wsgi.input"].read(size))
        except (ValueError, UnicodeError):
            return 400, {"error": "Не удалось прочитать форму."}
        if not isinstance(data, dict):
            return 400, {"error": "Проверьте поля формы."}
        previous = data.get("previous")
        if path == "/api/point":
            self.store.edit_point(data["name"], data["address"], data["excluded"], previous)
        elif path == "/api/section":
            self.store.add_section(data["name"])
        elif path == "/api/dish":
            self.store.move_dish(data["name"], data["section"], previous)
        elif path == "/api/settings":
            self.store.set_settings(data["start"], data["end"], previous)
        else:
            return 404, {"error": "Операция не найдена."}
        return 200, {"ok": True}
