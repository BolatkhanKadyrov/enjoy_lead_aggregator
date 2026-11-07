"""WSGI endpoints for the Telegram Mini App."""

import hashlib
import hmac
import json
import logging
import time
from contextlib import nullcontext
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qsl

from store import Conflict, PERMISSIONS, employee_id


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
    def __init__(self, store, token, admin_ids, ready=lambda: True, *, owner_id=None):
        self.store = store
        self.token = token
        self.admin_ids = admin_ids
        self.owner_id = owner_id
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
        except PermissionError:
            status, body = 403, {"error": "Для этого действия недостаточно прав. Обратитесь к владельцу цеха."}
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

    def access(self, uid, required=()):
        if self.owner_id is not None:
            return self.store.authorized(uid, required, self.owner_id)
        if uid not in self.admin_ids or "employees" in required:
            raise PermissionError
        return nullcontext(set(PERMISSIONS))

    def visible_state(self, uid, grants):
        if not grants:
            raise PermissionError
        catalog = self.store.state()
        owner = uid == self.owner_id
        result = {"owner": owner, "permissions": [p for p in PERMISSIONS if p in grants]}
        point_fields = ["name"]
        if "point_addresses" in grants:
            point_fields.append("address")
        if "invoice_exclusions" in grants:
            point_fields.append("excluded")
        if len(point_fields) > 1:
            result["points"] = [{k: item[k] for k in point_fields} for item in catalog["points"]]
        if grants.intersection(("dishes", "sections")):
            result["sections"] = catalog["sections"]
        if "dishes" in grants:
            result["dishes"] = catalog["dishes"]
        if "hours" in grants:
            result["settings"] = catalog["settings"]
        if owner:
            result["employees"] = [p for p in self.store.staff_list() if p["id"] != str(uid)]
        return result

    def usage(self, query):
        if len(query) > 160:
            raise ValueError("Слишком длинный запрос статистики.")
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
        except ValueError:
            raise ValueError("Проверьте параметры статистики.") from None
        params = dict(pairs)
        if len(params) != len(pairs) or params.keys() - {"days", "user_id", "before"}:
            raise ValueError("Проверьте параметры статистики.")
        days = params.get("days", "7")
        if days not in ("1", "7", "30"):
            raise ValueError("Выберите период за сегодня, 7 или 30 дней.")
        if "user_id" not in params:
            if "before" in params:
                raise ValueError("Выберите отправителя для просмотра истории.")
            return self.store.usage_summary(int(days), self.owner_id)
        uid = employee_id(params["user_id"])
        before = params.get("before")
        if before is not None:
            if not before.isascii() or not before.isdecimal() or not 0 < int(before) < 2**53:
                raise ValueError("Проверьте номер страницы истории.")
            before = int(before)
        return self.store.usage_detail(int(days), uid, before)

    def api(self, env, path, method):
        user_id = signed_user(env.get("HTTP_AUTHORIZATION", ""), self.token)
        if user_id is None:
            return 401, {"error": "Сеанс истёк. Откройте настройки заново из бота."}
        if self.owner_id is None and user_id not in self.admin_ids:
            raise PermissionError
        if path == "/api/usage":
            if self.owner_id is None or user_id != self.owner_id:
                raise PermissionError
            if method != "GET":
                return 405, {"error": "Статистика доступна для чтения."}
            return 200, self.usage(env.get("QUERY_STRING", ""))
        if path == "/api/state" and method == "GET":
            with self.access(user_id) as grants:
                return 200, self.visible_state(user_id, grants)
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
            point_rights = {"address": "point_addresses", "excluded": "invoice_exclusions"}
            changes = {k: data[k] for k in point_rights if k in data}
            if not changes or set(data) - {"name", "previous", *point_rights}:
                return 400, {"error": "Проверьте изменённые поля точки."}
            with self.access(user_id, [point_rights[k] for k in changes]):
                self.store.edit_point_fields(data["name"], changes, previous)
        elif path in ("/api/employee", "/api/employee/remove"):
            with self.access(user_id, ("employees",)):
                target = employee_id(data["id"])
                if target == self.owner_id:
                    return 400, {"error": "Владелец задаётся в Railway через OWNER_ID."}
                if path == "/api/employee":
                    self.store.save_employee(target, data["name"], data["permissions"], previous)
                else:
                    self.store.remove_employee(target, data["previous"])
        else:
            required = {"/api/section": "sections", "/api/dish": "dishes", "/api/settings": "hours"}.get(path)
            if required is None:
                return 404, {"error": "Операция не найдена."}
            with self.access(user_id, (required,)):
                if path == "/api/section":
                    self.store.add_section(data["name"])
                elif path == "/api/dish":
                    self.store.move_dish(data["name"], data["section"], previous)
                else:
                    self.store.set_settings(data["start"], data["end"], previous)
        return 200, {"ok": True}
