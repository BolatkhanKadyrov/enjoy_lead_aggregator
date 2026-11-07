"""Telegram transport and the private-chat order workflow."""

from datetime import date, datetime, timedelta
from http.client import HTTPException
import json
import logging
from secrets import token_hex
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from documents import MAX_BYTES as MAX_XLSX_BYTES, parse_workbook, render_documents
from store import PERMISSIONS, IntakeLimits, key


LOG = logging.getLogger(__name__)
ZONE = ZoneInfo("Asia/Almaty")
CHOICES_PER_PAGE = 12
COMMANDS = {
    "start": "Как отправить Excel и когда открыт приём",
    "resume": "Продолжить последнюю выгрузку или повторно получить PDF",
    "admin": "Открыть настройки цеха при наличии доступа",
    "id": "Узнать свой Telegram ID",
    "help": "Показать подсказки по командам",
}


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=0):
        # urllib errors may contain the token as part of the request URL.
        super().__init__(f"Telegram API status {code}")
        self.code = code
        self.retry_after = retry_after

    @property
    def transient(self):
        return self.code in (0, 409, 429) or self.code >= 500


class FileTooLarge(ValueError):
    def __init__(self, size_bytes=None):
        super().__init__("Файл больше 10 МБ.")
        self.size_bytes = size_bytes


def multipart(fields, files):
    boundary = "enjoy-" + token_hex(16)
    chunks = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            .encode("ascii")
        )
        chunks.extend((str(value).encode("utf-8"), b"\r\n"))
    # All part names and filenames come from our code.
    for name, (filename, content) in files.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'
            .encode("utf-8")
        )
        chunks.extend((content, b"\r\n"))
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class Telegram:
    def __init__(self, token):
        self.api_url = f"https://api.telegram.org/bot{token}/"
        self.file_url = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method, fields=None, files=None):
        fields = fields or {}
        if files:
            body, content_type = multipart(fields, files)
        else:
            body = json.dumps(fields).encode("utf-8")
            content_type = "application/json"
        request = Request(self.api_url + method, body, {"Content-Type": content_type})
        timeout = 5 if method == "setMyCommands" else 75
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except HTTPError as error:
            try:
                payload = json.loads(error.read(65536))
            except (ValueError, OSError, HTTPException):
                payload = {}
            raise TelegramError(error.code, payload.get("parameters", {}).get("retry_after", 0)) from None
        except (OSError, URLError, HTTPException, ValueError):
            raise TelegramError() from None
        if not payload.get("ok"):
            raise TelegramError(payload.get("error_code", 0), payload.get("parameters", {}).get("retry_after", 0))
        return payload["result"]

    def download(self, file_id):
        info = self.call("getFile", {"file_id": file_id})
        if info.get("file_size", 0) > MAX_XLSX_BYTES:
            raise FileTooLarge(info["file_size"])
        path = info.get("file_path")
        if not path:
            raise ValueError("Telegram не вернул файл. Отправьте его ещё раз.")
        try:
            with urlopen(self.file_url + quote(path, safe="/"), timeout=45) as response:
                data = response.read(MAX_XLSX_BYTES + 1)
        except HTTPError as error:
            raise TelegramError(error.code) from None
        except (OSError, URLError, HTTPException):
            raise TelegramError() from None
        if len(data) > MAX_XLSX_BYTES:
            # We stopped reading at the limit and do not know the full file size.
            raise FileTooLarge()
        return data


def next_delivery(collected):
    day = collected + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def question_for(receipts, catalog):
    for receipt in receipts:
        for item in receipt["lines"]:
            name = item["name"]
            if key(name) not in catalog["dishes"]:
                return {"kind": "dish", "name": name, "options": catalog["sections"]}
    for receipt in receipts:
        name = receipt["point"]
        if key(name) not in catalog["points"]:
            return {"kind": "point", "name": name}
    return None


class Bot:
    def __init__(self, store, token, admin_ids, public_url, *, limits=None, operator_ids=(), owner_id=None):
        self.store = store
        self.api = Telegram(token)
        self.admin_ids = set(admin_ids)
        self.operator_ids = set(operator_ids)
        self.owner_id = owner_id
        self.limits = limits if limits is not None else IntakeLimits()
        self.connected = False
        public_url = public_url or ""
        parsed = urlsplit(public_url)
        self.public_url = public_url if parsed.scheme == "https" and parsed.hostname else None

    def permissions(self, uid):
        if self.owner_id is not None:
            return PERMISSIONS if uid == self.owner_id else self.store.staff_permissions(uid)
        if uid in self.admin_ids:
            return PERMISSIONS
        return () if uid in self.operator_ids else None

    def say(self, uid, text, markup=None):
        data = {"chat_id": uid, "text": text}
        if markup:
            data["reply_markup"] = markup
        return self.api.call("sendMessage", data)

    def run(self, stop_event):
        webhook_removed = False
        next_menu_attempt = 0
        delay = 1
        while not stop_event.is_set():
            try:
                if not webhook_removed:
                    self.api.call("deleteWebhook", {"drop_pending_updates": False})
                    webhook_removed = True
                if next_menu_attempt is not None and monotonic() >= next_menu_attempt:
                    try:
                        self.api.call("setMyCommands", {
                            "scope": {"type": "all_private_chats"},
                            "commands": [{"command": name, "description": hint} for name, hint in COMMANDS.items()],
                        })
                    except TelegramError as error:
                        LOG.warning("Could not update the command menu (Telegram status %d)", error.code)
                        next_menu_attempt = monotonic() + max(300, error.retry_after)
                    else:
                        next_menu_attempt = None
                updates = self.api.call("getUpdates", {
                    "offset": self.store.get_offset(), "limit": 10, "timeout": 25 if self.connected else 0,
                    "allowed_updates": ["message", "callback_query"],
                })
                if not updates:
                    self.connected = True
                for update in updates:
                    if stop_event.is_set():
                        self.connected = False
                        return
                    try:
                        self.handle(update)
                    except TelegramError as error:
                        if error.transient or error.code == 401:
                            raise
                        LOG.warning("Telegram rejected update %d (HTTP %d)", update["update_id"], error.code)
                    # Advance only after persisting the effect of this update.
                    self.store.set_offset(update["update_id"] + 1)
                    self.connected = True
                delay = 1
            except TelegramError as error:
                self.connected = False
                if error.code in (401, 404, 409):
                    webhook_removed = False
                LOG.warning("Polling paused (Telegram status %d)", error.code)
                stop_event.wait(max(delay, error.retry_after))
                delay = min(delay * 2, 60)
            except Exception as error:
                # Keep the cursor for database errors and unexpected failures.
                self.connected = False
                LOG.error("Polling paused (%s)", type(error).__name__)
                stop_event.wait(delay)
                delay = min(delay * 2, 60)
        self.connected = False

    def handle(self, update):
        callback = update.get("callback_query")
        message = (callback.get("message") if callback else update.get("message")) or {}
        sender = callback.get("from", {}) if callback else message.get("from", {})
        chat = message.get("chat", {})
        uid = sender.get("id")
        if not uid or sender.get("is_bot") or chat.get("type") != "private" or chat.get("id") != uid:
            return
        words = message.get("text", "").split(maxsplit=1)
        command = words[0].partition("@")[0].lower() if words else ""
        if self.operator_ids and self.permissions(uid) is None:
            if not callback:
                reason = str(uid) if command == "/id" else (
                    f"Доступ к боту ограничен. Передайте владельцу бота ваш Telegram ID: {uid}\n"
                    "/id — узнать свой ID."
                )
                self.refuse(uid, message, reason)
            return

        if callback:
            self.on_callback(uid, callback, message)
        elif command == "/id":
            self.say(uid, str(uid))
        elif message.get("media_group_id"):
            if "document" in message:
                self.record_request(uid, message, "rejected")
            self.refuse(uid, message, "Файлы отправлены альбомом. Отправьте одну полную выгрузку XLSX отдельным сообщением.")
        elif "document" in message:
            self.upload(uid, message)
        elif "text" in message:
            self.on_text(uid, message, command)
        else:
            self.refuse(uid, message, "Отправьте выгрузку Excel в формате .xlsx как файл.")

    def welcome(self, uid, *, settings_requested=False):
        grants = self.permissions(uid)
        if settings_requested and self.owner_id is not None and not grants:
            self.say(uid, "Чтобы открыть настройки, попросите владельца выдать вам права на редактирование. "
                          f"Ваш Telegram ID: {uid}")
            return
        hours = self.store.settings()
        markup = None
        if self.public_url and grants:
            markup = {"inline_keyboard": [[{
                "text": "Настройки цеха", "web_app": {"url": self.public_url}
            }]]}
        self.say(uid, f"Отправьте полную выгрузку за день одним файлом .xlsx. "
                 f"Принимаю файлы каждый день с {hours['start']} до {hours['end']} по времени Астаны. "
                 "В ответ вы получите план производства и накладные.\n\n/help — подсказки по командам.", markup)

    def command_help(self, uid):
        can_open_settings = self.public_url and self.permissions(uid)
        hints = [f"/{name} — {hint}" for name, hint in COMMANDS.items()
                 if name != "admin" or can_open_settings]
        if uid == self.owner_id and can_open_settings:
            hints.append("\nСтатистика сотрудников доступна через /admin → Настройки цеха → Статистика.")
        self.say(uid, "\n".join(hints))

    def refuse(self, uid, message, reason):
        sent_at = message["date"]
        album = message.get("media_group_id")
        if not self.store.should_notify(uid, sent_at, album=album):
            return
        try:
            self.say(uid, reason)
        except TelegramError as error:
            if error.code in (401, 429):
                # Telegram explicitly declined delivery. Retry the notice with this update.
                self.store.release_notice(uid, sent_at, album=album)
            raise

    def record_request(self, uid, message, status):
        document = message.get("document")
        kind = "file" if document is not None else "resume"
        size = document.get("file_size") if document is not None else None
        self.store.record_usage(uid, message["message_id"], message["date"], kind, status, size_bytes=size)

    def reject_request(self, uid, message, reason):
        self.record_request(uid, message, "rejected")
        self.refuse(uid, message, reason)

    def admit(self, uid, message, *, resume=False):
        message_id = message["message_id"]
        wait, replay = self.store.reserve_work(uid, message_id, message["date"], self.limits, resume=resume)
        if wait is not None:
            action = "Команду /resume" if resume else "Файл Excel"
            when = f"через {wait} сек." if wait else "сейчас."
            self.reject_request(uid, message, f"Достигнут лимит обработки. {action} можно отправить ещё раз {when}")
            return False
        if replay:
            error = self.store.work_error(uid, message_id)
            if error:
                self.refuse(uid, message, error)
                return False
        self.record_request(uid, message, "accepted" if resume else "pending")
        return True

    def upload(self, uid, message):
        version = str(message["message_id"])
        current = self.store.job_version(uid)
        if current is not None and message["message_id"] <= int(current):
            if version == current:
                self.advance(uid, self.store.get_job(uid))
            return
        sent = datetime.fromtimestamp(message["date"], ZONE)
        hours = self.store.settings()
        clock = sent.strftime("%H:%M")
        if not hours["start"] <= clock <= hours["end"]:
            opening = sent.date()
            if clock > hours["end"]:
                opening += timedelta(days=1)
            self.reject_request(uid, message, f"Вы отправили файл {sent:%d.%m.%Y в %H:%M} по времени Астаны. "
                                f"Приём открыт ежедневно с {hours['start']} до {hours['end']}.\n"
                                f"Ближайший приём откроется {opening:%d.%m.%Y} в {hours['start']}. "
                                "Отправьте Excel ещё раз в часы приёма.")
            return
        document = message["document"]
        if not document.get("file_name", "").casefold().endswith(".xlsx"):
            self.reject_request(uid, message, "Нужен файл выгрузки с расширением .xlsx.")
            return
        if document.get("file_size", 0) > MAX_XLSX_BYTES:
            self.reject_request(uid, message, "Выгрузка не принята. Файл больше 10 МБ.")
            return
        if not self.admit(uid, message):
            return
        size = document.get("file_size")
        discard_size_estimate = False
        try:
            raw = self.api.download(document["file_id"])
            size = len(raw)
            receipts = parse_workbook(raw)
        except FileTooLarge as error:
            size = error.size_bytes
            # A bounded read crossed the limit despite Telegram's smaller estimate.
            discard_size_estimate = size is None
            failure = f"Выгрузка не принята. {error}"
        except ValueError as error:
            failure = f"Выгрузка не принята. {str(error)[:600]}"
        except TelegramError as error:
            if error.transient or error.code == 401:
                raise
            failure = "Telegram не передал файл. Отправьте Excel ещё раз."
        else:
            self.store.replace_job(uid, version, receipts, sent.date().isoformat(),
                                   usage_sent_at=message["date"], usage_size_bytes=size)
            del raw
            self.advance(uid, self.store.get_job(uid))
            return
        self.store.record_work_error(uid, message["message_id"], failure,
                                     usage_sent_at=message["date"], usage_size_bytes=size,
                                     discard_size_estimate=discard_size_estimate)
        self.refuse(uid, message, failure)

    def on_text(self, uid, message, command):
        text = message["text"].strip()
        if command in ("/help", "/"):
            self.command_help(uid)
            return
        if command in ("/start", "/admin"):
            self.welcome(uid, settings_requested=command == "/admin")
            return
        if command == "/resume":
            version = self.store.job_version(uid)
            if version is None:
                self.record_request(uid, message, "rejected")
                self.welcome(uid)
            elif message["message_id"] > int(version) and self.admit(uid, message, resume=True):
                self.advance(uid, self.store.get_job(uid))
            return
        if command.startswith("/"):
            self.say(uid, "Такой команды нет. Введите /help, чтобы посмотреть список команд.")
            return
        job = self.store.get_job(uid)
        if not job:
            self.welcome(uid)
            return
        prompt = job.get("pending")
        if job["status"] != "question" or not prompt or not prompt.get("message_id"):
            if job["status"] in ("done", "retry", "delivering"):
                self.say(uid, "Отправьте новый Excel или введите /resume для последней выгрузки.")
            else:
                self.advance(uid, job)
            return
        replied_to = message.get("reply_to_message", {}).get("message_id")
        if message["message_id"] <= prompt["message_id"] or (replied_to and replied_to != prompt["message_id"]):
            # An update may be replayed after the next prompt was persisted.
            if replied_to and replied_to != prompt["message_id"]:
                self.say(uid, "Этот вопрос уже закрыт. Ответьте на последний вопрос бота.")
            return
        self.answer(uid, job, text)

    def acknowledge(self, callback_id):
        try:
            self.api.call("answerCallbackQuery", {"callback_query_id": callback_id})
        except TelegramError as error:
            # An expired UI notification need not block processing the answer.
            if error.code != 400:
                raise

    def on_callback(self, uid, callback, message):
        self.acknowledge(callback["id"])
        job = self.store.get_job(uid)
        if not job:
            return
        pending = job.get("pending")
        if not pending or not pending.get("message_id"):
            self.advance(uid, job)
            return
        parts = callback.get("data", "").split(":")
        if (job["status"] != "question" or len(parts) != 2 or parts[0] != pending["token"]
                or message["message_id"] != pending["message_id"]):
            return
        action = parts[1]
        if pending["kind"] == "point" and action == "skip":
            self.answer(uid, job, "", skip=True)
        elif pending["kind"] == "dish" and action.startswith("s") and action[1:].isdecimal():
            idx = int(action[1:])
            if idx < len(pending["options"]):
                self.answer(uid, job, pending["options"][idx])
        elif pending["kind"] == "dish" and action.startswith("p") and action[1:].isdecimal():
            page = int(action[1:])
            if page * CHOICES_PER_PAGE < len(pending["options"]):
                self.prompt(uid, job, {**pending, "page": page})

    def answer(self, uid, job, text, skip=False):
        prompt = job["pending"]
        text = " ".join(text.split())
        is_dish = prompt["kind"] == "dish"
        limit = 100 if is_dish else 500
        if not skip and (not text or len(text) > limit or any(ord(c) < 32 for c in text)):
            self.say(uid, f"Введите ответ длиной от 1 до {limit} символов.")
            return
        if is_dish:
            section_key = key(text)
            text = next((option for option in prompt["options"] if key(option) == section_key), text)
        try:
            self.save_answer(uid, job["version"], prompt["kind"], prompt["name"], text)
        except ValueError:
            self.say(uid, "Не удалось сохранить значение. Проверьте текст и ответьте ещё раз.")
            return
        self.store.update_job(uid, job["version"], "processing")
        self.advance(uid, self.store.get_job(uid))

    def save_answer(self, uid, version, kind, name, value):
        is_dish = kind == "dish"
        save_shared = self.store.save_new_dish if is_dish else self.store.save_new_point
        if self.owner_id is None:
            save_shared(name, value)
            return
        required = "dishes" if is_dish else "point_addresses"
        try:
            with self.store.authorized(uid, required, self.owner_id) as grants:
                if version != self.store.job_version(uid):
                    return
                if is_dish and "sections" not in grants:
                    sections = self.store.catalog()["sections"]
                    if not any(key(section) == key(value) for section in sections):
                        self.store.save_local_answer(uid, version, kind, name, value)
                        return
                save_shared(name, value)
        except PermissionError:
            self.store.save_local_answer(uid, version, kind, name, value)

    def prompt(self, uid, job, pending):
        pending = {**pending, "token": token_hex(8), "message_id": None}
        self.store.update_job(uid, job["version"], "question", pending)
        prefix = pending["token"] + ":"
        if pending["kind"] == "point":
            text = f"Какой адрес указать для точки «{pending['name']}»? Напишите адрес или пропустите."
            buttons = [[{"text": "Пропустить", "callback_data": prefix + "skip"}]]
        else:
            text = f"В какой раздел плана добавить блюдо «{pending['name']}»? Выберите раздел или напишите название."
            options = pending["options"]
            start = pending.get("page", 0) * CHOICES_PER_PAGE
            buttons = [[{"text": label, "callback_data": prefix + f"s{idx}"}]
                       for idx, label in enumerate(options[start:start + CHOICES_PER_PAGE], start)]
            nav = []
            if start:
                nav.append({"text": "‹ Назад", "callback_data": prefix + f"p{pending['page'] - 1}"})
            if start + CHOICES_PER_PAGE < len(options):
                nav.append({"text": "Дальше ›", "callback_data": prefix + f"p{pending.get('page', 0) + 1}"})
            if nav:
                buttons.append(nav)
        if self.owner_id is not None:
            grants = self.permissions(uid) or ()
            required = "dishes" if pending["kind"] == "dish" else "point_addresses"
            if required not in grants:
                text += "\nОтвет сохранится только для этой выгрузки."
            elif pending["kind"] == "dish" and "sections" not in grants:
                text += "\nНовый раздел можно указать только для этой выгрузки."
        sent = self.say(uid, text, {"inline_keyboard": buttons} if buttons else None)
        pending["message_id"] = sent["message_id"]
        self.store.update_job(uid, job["version"], "question", pending)

    def advance(self, uid, job):
        if job["status"] == "done":
            return
        if job["status"] in ("delivering", "retry"):
            self.say(uid, "Если оба PDF не пришли, введите /resume для повторной отправки.")
            return
        catalog = self.store.catalog()
        overrides = self.store.job_overrides(uid, job["version"])
        sections = {key(section): section for section in catalog["sections"]}
        for dish, section in overrides["dishes"].items():
            if dish not in catalog["dishes"]:
                catalog["dishes"][dish] = sections.setdefault(key(section), section)
        catalog["sections"] = list(sections.values())
        for point, record in overrides["points"].items():
            destination = catalog["points"].setdefault(point, record)
            if not destination["address"]:
                destination["address"] = record["address"]
        question = question_for(job["receipts"], catalog)
        if question:
            old = job.get("pending")
            if old and old.get("kind") == question["kind"] and old.get("name") == question["name"] and old.get("message_id"):
                return
            self.prompt(uid, job, question)
            return
        collected = date.fromisoformat(job["collected"])
        delivery = next_delivery(collected)
        try:
            plan, invoices = render_documents(job["receipts"], catalog, collected, delivery)
            if not plan or not invoices:
                raise ValueError("Missing document")
        except Exception as error:
            LOG.error("Document generation failed (%s)", type(error).__name__)
            self.store.update_job(uid, job["version"], "retry")
            self.say(uid, "Не удалось сформировать PDF. Данные сохранены. Для повторной попытки введите /resume.")
            return
        media = [{"type": "document", "media": f"attach://{key}"} for key in ("plan", "invoices")]
        fields = {"chat_id": uid, "media": json.dumps(media),
                  "reply_parameters": json.dumps({"message_id": int(job["version"]), "allow_sending_without_reply": True})}
        files = {"plan": (f"1_План_производства_{delivery}.pdf", plan),
                 "invoices": (f"2_Накладные_{delivery}.pdf", invoices)}
        self.store.update_job(uid, job["version"], "delivering")
        try:
            self.api.call("sendMediaGroup", fields, files)
        except TelegramError as error:
            if error.code == 429:
                self.store.update_job(uid, job["version"], "processing")
                raise
            # A timeout can occur after Telegram accepted the album. Let the
            # recipient explicitly retry instead of blindly sending it twice.
            self.store.update_job(uid, job["version"], "retry")
            self.say(uid, "Не получилось подтвердить отправку. Если PDF не пришли, введите /resume.")
            return
        self.store.update_job(uid, job["version"], "done")
