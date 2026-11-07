"""Shared catalog and per-Telegram-user uploads on a persistent SQLite volume."""

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, time as wall_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class Conflict(ValueError):
    """Another administrator changed the entry since it was opened."""


@dataclass(frozen=True)
class IntakeLimits:
    """Seconds between requests, requests per sender per 10 min, requests per service per min."""
    cooldown: int = 30
    per_user: int = 5
    global_rate: int = 10

    def __post_init__(self):
        for name, high in (("cooldown", 86400), ("per_user", 10000), ("global_rate", 10000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= high:
                raise ValueError(f"{name}: допустимы целые значения от 1 до {high}.")


INTAKE_RETENTION = 48 * 3600
PERMISSIONS = ("point_addresses", "invoice_exclusions", "dishes", "sections", "hours")
MAX_TELEGRAM_ID = 2**52 - 1
USAGE_PERIODS = (1, 7, 30)
USAGE_PAGE_SIZE = 25
ASTANA = ZoneInfo("Asia/Almaty")


def usage_window(days):
    if type(days) is not int or days not in USAGE_PERIODS:
        raise ValueError("Выберите период 1, 7 или 30 дней.")
    today = datetime.fromtimestamp(time.time(), ASTANA).date()
    first = today - timedelta(days=days - 1)
    start = datetime.combine(first, wall_time.min, ASTANA)
    stop = datetime.combine(today + timedelta(days=1), wall_time.min, ASTANA)
    return first.isoformat(), int(start.timestamp()), int(stop.timestamp())


def clean(value, label, limit, *, empty=False):
    if not isinstance(value, str):
        raise ValueError(f"Проверьте поле «{label}».")
    value = " ".join(value.split())
    if len(value) > limit or (not empty and not value):
        raise ValueError(f"Поле «{label}» должно содержать от {0 if empty else 1} до {limit} символов.")
    return value


def key(value):
    return " ".join(value.split()).casefold()


def check_previous(previous, actual):
    if previous is not None and previous != actual:
        raise Conflict("Запись уже изменена. Обновите данные и повторите сохранение.")


def employee_id(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= MAX_TELEGRAM_ID:
        raise ValueError("Введите числовой Telegram ID личного чата.")
    return value


def employee_fields(name, permissions):
    name = clean(name, "Имя сотрудника", 100)
    if (not isinstance(permissions, list) or len(permissions) > len(PERMISSIONS)
            or any(not isinstance(p, str) or p not in PERMISSIONS for p in permissions)
            or len(set(permissions)) != len(permissions)):
        raise ValueError("Выберите права из списка.")
    return {"name": name, "permissions": [p for p in PERMISSIONS if p in permissions]}


def employee_snapshot(previous):
    if not isinstance(previous, dict) or set(previous) != {"name", "permissions"}:
        raise ValueError("Откройте актуальную карточку сотрудника.")
    return employee_fields(previous["name"], previous["permissions"])


SCHEMA = """
CREATE TABLE IF NOT EXISTS sections (
    key TEXT PRIMARY KEY, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dishes (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, section_key TEXT NOT NULL REFERENCES sections(key)
);
CREATE TABLE IF NOT EXISTS points (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL, excluded INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS employees (
    user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, permissions TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    user_id INTEGER PRIMARY KEY, version TEXT NOT NULL, receipts TEXT NOT NULL,
    collected TEXT NOT NULL, status TEXT NOT NULL, pending TEXT
);
CREATE TABLE IF NOT EXISTS job_answers (
    user_id INTEGER NOT NULL REFERENCES jobs(user_id) ON DELETE CASCADE, version TEXT NOT NULL,
    kind TEXT NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL, answer TEXT NOT NULL,
    PRIMARY KEY(user_id,kind,key)
);
CREATE TABLE IF NOT EXISTS intake (
    user_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sent_at INTEGER NOT NULL,
    reserved_at INTEGER NOT NULL, error TEXT, PRIMARY KEY(user_id,message_id)
);
CREATE INDEX IF NOT EXISTS intake_sender_sent ON intake(user_id,sent_at);
CREATE INDEX IF NOT EXISTS intake_sender_reserved ON intake(user_id,reserved_at);
CREATE INDEX IF NOT EXISTS intake_sent ON intake(sent_at);
CREATE INDEX IF NOT EXISTS intake_reserved ON intake(reserved_at);
CREATE TABLE IF NOT EXISTS intake_denials (
    user_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, eligible_at INTEGER NOT NULL, decided_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS denials_age ON intake_denials(decided_at);
CREATE TABLE IF NOT EXISTS intake_notices (
    user_id INTEGER PRIMARY KEY, sent_at INTEGER NOT NULL, notified_at INTEGER NOT NULL, album TEXT
);
CREATE INDEX IF NOT EXISTS notices_age ON intake_notices(notified_at);
CREATE TABLE IF NOT EXISTS intake_album_notices (
    user_id INTEGER NOT NULL, album TEXT NOT NULL, notified_at INTEGER NOT NULL, sent_at INTEGER NOT NULL,
    PRIMARY KEY(user_id,album)
);
CREATE INDEX IF NOT EXISTS album_notices_age ON intake_album_notices(notified_at);
CREATE TABLE IF NOT EXISTS usage_events (
    user_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sent_at INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('file','resume')),
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')),
    size_bytes INTEGER, receipts INTEGER, lines INTEGER, PRIMARY KEY(user_id,message_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS usage_time ON usage_events(sent_at);
CREATE INDEX IF NOT EXISTS usage_pending ON usage_events(status) WHERE status='pending';
"""


class Store:
    def __init__(self, database, *, initial_catalog=None, require_catalog=False):
        self.database = Path(database)
        self._bound_connection = ContextVar("staff_transaction", default=None)
        self._owner_id = None
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        defaults = json.loads(Path(__file__).with_name("seed.json").read_text(encoding="utf-8"))
        with self.connection(write=True) as conn:
            conn.execute("INSERT OR IGNORE INTO config VALUES ('usage_started_at',?)", (str(int(time.time())),))
            conn.execute("DELETE FROM usage_events WHERE sent_at<?", (usage_window(max(USAGE_PERIODS))[1],))
            if not conn.execute("SELECT 1 FROM config WHERE key='initialized'").fetchone():
                for name in defaults["sections"]:
                    self._section(conn, name)
                conn.executemany("INSERT INTO config VALUES (?,?)", [
                    ("initialized", "1"), ("start", "18:00"), ("end", "23:30"), ("offset", "0")
                ])
            imported = conn.execute("SELECT 1 FROM config WHERE key='initial_catalog'").fetchone()
            if not imported and require_catalog and initial_catalog is None:
                raise ValueError("Для первого запуска добавьте INITIAL_CATALOG_JSON или файл enjoy-catalog.json на Volume.")
            if not imported and initial_catalog is not None:
                if not isinstance(initial_catalog, dict) or not all(
                    isinstance(initial_catalog.get(k), t)
                    for k, t in (("sections", list), ("dishes", dict), ("points", list))
                ):
                    raise ValueError("Проверьте формат стартового справочника.")
                for section in initial_catalog["sections"]:
                    self._section(conn, section)
                for dish, section in initial_catalog["dishes"].items():
                    name = clean(dish, "Блюдо", 500)
                    section_key = self._section(conn, section)
                    conn.execute("INSERT OR IGNORE INTO dishes VALUES (?,?,?)", (key(name), name, section_key))
                for point in initial_catalog["points"]:
                    self._insert_point(conn, point)
                conn.execute("INSERT INTO config VALUES ('initial_catalog','1')")

    @contextmanager
    def connection(self, *, write=False):
        bound = self._bound_connection.get()
        if bound is not None:
            yield bound
            return
        conn = sqlite3.connect(self.database, timeout=12)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            with conn:
                yield conn
        finally:
            conn.close()

    def seed_staff(self, owner_id, admin_ids, operator_ids):
        """Import legacy environment grants once. Subsequent edits belong to the owner."""
        with self.connection(write=True) as conn:
            seeded = conn.execute("SELECT 1 FROM config WHERE key='staff_seeded'").fetchone()
            if owner_id is None:
                if seeded:
                    raise ValueError("Для управления сотрудниками укажите OWNER_ID в переменных окружения.")
                return
            owner = employee_id(owner_id)
            if not seeded:
                admins = {employee_id(uid) for uid in admin_ids}
                operators = {employee_id(uid) for uid in operator_ids}
                for uid in sorted((admins | operators) - {owner}):
                    grants = PERMISSIONS if uid in admins else ()
                    conn.execute("INSERT OR IGNORE INTO employees VALUES (?,?,?)",
                                 (uid, f"Telegram {uid}", json.dumps(grants)))
                conn.execute("INSERT INTO config VALUES ('staff_seeded','1')")
            # If ownership was transferred to an existing employee, only OWNER_ID
            # now grants this account access. It cannot be edited as an employee.
            conn.execute("DELETE FROM employees WHERE user_id=?", (owner,))
        self._owner_id = owner

    def staff_permissions(self, uid):
        with self.connection() as conn:
            row = conn.execute("SELECT permissions FROM employees WHERE user_id=?", (uid,)).fetchone()
            return None if row is None else set(json.loads(row[0]))

    def staff_list(self):
        with self.connection() as conn:
            return [{"id": str(row["user_id"]), "name": row["name"], "permissions": json.loads(row["permissions"])}
                    for row in conn.execute("SELECT user_id,name,permissions FROM employees ORDER BY name,user_id")]

    @contextmanager
    def authorized(self, uid, required, owner_id):
        """Serialize grant checks and the protected operation with concurrent revocations."""
        required = {required} if isinstance(required, str) else set(required)
        with self.connection(write=True) as conn:
            token = self._bound_connection.set(conn)
            try:
                if owner_id is not None and uid == owner_id:
                    available = set(PERMISSIONS) | {"employees"}
                else:
                    available = self.staff_permissions(uid)
                if available is None or not required <= available:
                    raise PermissionError("Для этого действия у вас нет прав.")
                yield available
            finally:
                self._bound_connection.reset(token)

    def _editable_employee(self, empid):
        uid = employee_id(empid)
        if uid == self._owner_id:
            raise ValueError("Владелец задаётся через OWNER_ID.")
        return uid

    def save_employee(self, empid, name, permissions, previous=None):
        uid = self._editable_employee(empid)
        fields = employee_fields(name, permissions)
        encoded = json.dumps(fields["permissions"])
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT name,permissions FROM employees WHERE user_id=?", (uid,)).fetchone()
            if previous is None:
                if row is not None:
                    raise Conflict("Сотрудник с этим Telegram ID уже существует.")
                conn.execute("INSERT INTO employees VALUES (?,?,?)", (uid, fields["name"], encoded))
                return
            if row is None:
                raise Conflict("Этот сотрудник уже удалён.")
            check_previous(employee_snapshot(previous), {"name": row["name"], "permissions": json.loads(row["permissions"])})
            conn.execute("UPDATE employees SET name=?,permissions=? WHERE user_id=?", (fields["name"], encoded, uid))

    def remove_employee(self, empid, previous):
        uid = self._editable_employee(empid)
        expected = employee_snapshot(previous)
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT name,permissions FROM employees WHERE user_id=?", (uid,)).fetchone()
            if row is None:
                raise Conflict("Этот сотрудник уже удалён.")
            check_previous(expected, {"name": row["name"], "permissions": json.loads(row["permissions"])})
            conn.execute("DELETE FROM employees WHERE user_id=?", (uid,))

    @staticmethod
    def _insert_point(conn, point):
        name = clean(point["name"], "Точка", 500)
        address = clean(point["address"], "Адрес", 500, empty=True)
        excluded = point.get("excluded", False)
        if type(excluded) is not bool:
            raise ValueError("Проверьте исключение точки.")
        conn.execute("INSERT OR IGNORE INTO points VALUES (?,?,?,?)", (key(name), name, address, excluded))

    @staticmethod
    def _section(conn, name):
        name = clean(name, "Раздел", 100)
        conn.execute("INSERT OR IGNORE INTO sections VALUES (?,?)", (key(name), name))
        return key(name)

    @staticmethod
    def _settings(conn):
        return dict(conn.execute("SELECT key,value FROM config WHERE key IN ('start','end')"))

    def settings(self):
        with self.connection() as conn:
            return self._settings(conn)

    def set_settings(self, start, end, previous=None):
        pattern = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
        if any(not isinstance(t, str) or not re.fullmatch(pattern, t) for t in (start, end)):
            raise ValueError("Введите время в формате ЧЧ:ММ.")
        if start >= end:
            raise ValueError("Начало приёма должно быть раньше окончания.")
        with self.connection(write=True) as conn:
            check_previous(previous, self._settings(conn))
            conn.executemany("UPDATE config SET value=? WHERE key=?", [(start, "start"), (end, "end")])

    def state(self):
        with self.connection() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN")
            sections = [row[0] for row in conn.execute("SELECT name FROM sections ORDER BY rowid")]
            dishes = [dict(row) for row in conn.execute(
                "SELECT dishes.name AS name, sections.name AS section FROM dishes "
                "JOIN sections ON sections.key=dishes.section_key ORDER BY dishes.key"
            )]
            points = [dict(row, excluded=bool(row["excluded"])) for row in conn.execute(
                "SELECT name,address,excluded FROM points ORDER BY key"
            )]
            return {"sections": sections, "dishes": dishes, "points": points, "settings": self._settings(conn)}

    def catalog(self):
        state = self.state()
        return {"sections": state["sections"],
                "dishes": {key(d["name"]): d["section"] for d in state["dishes"]},
                "points": {key(p["name"]): p for p in state["points"]}}

    def add_section(self, name):
        with self.connection(write=True) as conn:
            self._section(conn, name)

    def save_new_dish(self, name, section):
        name = clean(name, "Блюдо", 500)
        with self.connection(write=True) as conn:
            if not conn.execute("SELECT 1 FROM dishes WHERE key=?", (key(name),)).fetchone():
                section_key = self._section(conn, section)
                conn.execute("INSERT INTO dishes VALUES (?,?,?)", (key(name), name, section_key))

    def save_new_point(self, name, address):
        name = clean(name, "Точка", 500)
        address = clean(address, "Адрес", 500, empty=True)
        with self.connection(write=True) as conn:
            # Two chats can receive the address question before either one replies.
            conn.execute(
                "INSERT INTO points (key,name,address,excluded) VALUES (?,?,?,0) "
                "ON CONFLICT(key) DO UPDATE SET address=excluded.address "
                "WHERE points.address='' AND excluded.address<>''",
                (key(name), name, address),
            )

    def edit_point(self, name, address, excluded, previous=None):
        self.edit_point_fields(name, {"address": address, "excluded": excluded}, previous)

    def edit_point_fields(self, name, changes, previous=None):
        point = key(clean(name, "Точка", 500))
        if not isinstance(changes, dict) or not changes or not changes.keys() <= {"address", "excluded"}:
            raise ValueError("Проверьте поля точки.")
        changes = changes.copy()
        if "address" in changes:
            changes["address"] = clean(changes["address"], "Адрес", 500, empty=True)
        if "excluded" in changes and type(changes["excluded"]) is not bool:
            raise ValueError("Укажите, исключена ли точка из накладных.")
        if previous is not None and (not isinstance(previous, dict) or previous.keys() != changes.keys()
                                    or ("excluded" in previous and type(previous["excluded"]) is not bool)
                                    or ("address" in previous and not isinstance(previous["address"], str))):
            raise ValueError("Откройте актуальную карточку точки.")
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT address,excluded FROM points WHERE key=?", (point,)).fetchone()
            if row is None:
                raise ValueError("Точка отсутствует в справочнике.")
            actual = {"address": row["address"], "excluded": bool(row["excluded"])}
            check_previous(previous, {field: actual[field] for field in changes})
            actual.update(changes)
            conn.execute("UPDATE points SET address=?,excluded=? WHERE key=?",
                         (actual["address"], actual["excluded"], point))

    def move_dish(self, name, section, previous=None):
        dish = key(clean(name, "Блюдо", 500))
        section_key = key(clean(section, "Раздел", 100))
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT s.name FROM dishes d JOIN sections s ON s.key=d.section_key WHERE d.key=?",
                               (dish,)).fetchone()
            if row is None or not conn.execute("SELECT 1 FROM sections WHERE key=?", (section_key,)).fetchone():
                raise ValueError("Указанное блюдо или раздел отсутствует в справочнике.")
            check_previous(previous, {"section": row[0]})
            conn.execute("UPDATE dishes SET section_key=? WHERE key=?", (section_key, dish))

    def replace_job(self, user_id, version, receipts, collected, *, usage_sent_at=None, usage_size_bytes=None):
        with self.connection(write=True) as conn:
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,'processing',NULL) ON CONFLICT(user_id) DO UPDATE SET "
                "version=excluded.version,receipts=excluded.receipts,collected=excluded.collected,"
                "status='processing',pending=NULL",
                (user_id, version, json.dumps(receipts, ensure_ascii=False), collected)
            )
            conn.execute("DELETE FROM job_answers WHERE user_id=?", (user_id,))
            if usage_sent_at is not None:
                self._record_usage(conn, user_id, int(version), usage_sent_at, "file", "accepted",
                                   usage_size_bytes, len(receipts), sum(len(r["lines"]) for r in receipts))

    def save_local_answer(self, uid, version, kind, name, value):
        """Keep an answer with its private upload when shared editing is unavailable."""
        if kind not in ("dish", "point"):
            raise ValueError("Неизвестный тип ответа.")
        name = clean(name, "Название", 500)
        answer = (clean(value, "Раздел", 100) if kind == "dish"
                  else clean(value, "Адрес", 500, empty=True))
        with self.connection(write=True) as conn:
            if not conn.execute("SELECT 1 FROM jobs WHERE user_id=? AND version=?", (uid, version)).fetchone():
                return False
            conn.execute("INSERT OR REPLACE INTO job_answers VALUES (?,?,?,?,?,?)",
                         (uid, version, kind, key(name), name, answer))
            return True

    def job_overrides(self, uid, version):
        result = {"dishes": {}, "points": {}}
        with self.connection() as conn:
            for row in conn.execute("SELECT kind,key,name,answer FROM job_answers WHERE user_id=? AND version=?",
                                    (uid, version)):
                if row["kind"] == "dish":
                    result["dishes"][row["key"]] = row["answer"]
                else:
                    result["points"][row["key"]] = {
                        "name": row["name"], "address": row["answer"], "excluded": False,
                    }
        return result

    def get_job(self, user_id):
        with self.connection() as conn:
            row = conn.execute("SELECT version,receipts,collected,status,pending FROM jobs WHERE user_id=?",
                               (user_id,)).fetchone()
            if not row:
                return None
            return dict(row, receipts=json.loads(row["receipts"]), pending=json.loads(row["pending"] or "null"))

    def job_version(self, user_id):
        with self.connection() as conn:
            row = conn.execute("SELECT version FROM jobs WHERE user_id=?", (user_id,)).fetchone()
            return row[0] if row else None

    @staticmethod
    def _quota_deadline(conn, column, clock, window, quota, user_id=None):
        # The quota-th newest reservation is the first one that must age out.
        # column is always a literal column name supplied by reserve_work.
        where = f"{column}>?"
        args = [clock - window]
        if user_id is not None:
            where += " AND user_id=?"
            args.append(user_id)
        row = conn.execute(f"SELECT {column} FROM intake WHERE {where} "
                           f"ORDER BY {column} DESC LIMIT 1 OFFSET ?", (*args, quota - 1)).fetchone()
        return row[0] + window if row else 0

    def reserve_work(self, uid, message_id, sent_at, limits, *, resume=False):
        """Return (None for admission or seconds until a fresh send, replay of a saved decision)."""
        with self.connection(write=True) as conn:
            now = int(time.time())
            cutoff = now - INTAKE_RETENTION
            conn.execute("DELETE FROM intake WHERE reserved_at<=?", (cutoff,))
            conn.execute("DELETE FROM intake_denials WHERE decided_at<=?", (cutoff,))
            identity = (uid, message_id)
            if conn.execute("SELECT 1 FROM intake WHERE user_id=? AND message_id=?", identity).fetchone():
                return None, True
            prior = conn.execute("SELECT eligible_at FROM intake_denials WHERE user_id=? AND message_id=?",
                                 identity).fetchone()
            if prior:
                return max(0, prior[0] - now), True

            deadline = 0
            for column, clock in (("sent_at", sent_at), ("reserved_at", now)):
                deadline = max(deadline,
                               self._quota_deadline(conn, column, clock, limits.cooldown, 1, uid),
                               self._quota_deadline(conn, column, clock, 600, limits.per_user, uid),
                               self._quota_deadline(conn, column, clock, 60, limits.global_rate))
            if deadline:
                # Polling acknowledges one update before it moves to the next.
                # Retain the last denial without allocating a row per burst message.
                conn.execute("INSERT OR REPLACE INTO intake_denials VALUES (?,?,?,?)",
                             (uid, message_id, deadline, now))
                return max(0, deadline - now), False
            conn.execute("INSERT INTO intake(user_id,message_id,sent_at,reserved_at) VALUES (?,?,?,?)",
                         (uid, message_id, sent_at, now))
            if resume:
                conn.execute("UPDATE jobs SET status='processing',pending=NULL WHERE user_id=?", (uid,))
            return None, False

    def work_error(self, uid, message_id):
        with self.connection() as conn:
            row = conn.execute("SELECT error FROM intake WHERE user_id=? AND message_id=?",
                               (uid, message_id)).fetchone()
            return row[0] if row else None

    def record_work_error(self, uid, message_id, detail, *, usage_sent_at=None, usage_size_bytes=None,
                          discard_size_estimate=False):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE intake SET error=? WHERE user_id=? AND message_id=?",
                         (detail, uid, message_id))
            if usage_sent_at is not None:
                self._record_usage(conn, uid, message_id, usage_sent_at, "file", "rejected", usage_size_bytes,
                                   None, None, discard_size_estimate=discard_size_estimate)

    @staticmethod
    def _record_usage(conn, uid, message_id, sent_at, kind, status, size_bytes, receipts, lines,
                      *, discard_size_estimate=False):
        # Store request metadata only. Retries share the original message's identity.
        cutoff = usage_window(max(USAGE_PERIODS))[1]
        conn.execute("DELETE FROM usage_events WHERE sent_at<?", (cutoff,))
        if sent_at < cutoff:
            return
        conn.execute("""
            INSERT INTO usage_events(user_id,message_id,sent_at,kind,status,size_bytes,receipts,lines)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,message_id) DO UPDATE SET
                status=excluded.status,
                size_bytes=CASE WHEN ? THEN NULL ELSE COALESCE(excluded.size_bytes,usage_events.size_bytes) END,
                receipts=COALESCE(excluded.receipts,usage_events.receipts),
                lines=COALESCE(excluded.lines,usage_events.lines)
            WHERE usage_events.kind=excluded.kind AND (usage_events.status='pending'
                OR (usage_events.status='rejected' AND excluded.status IN ('pending','accepted')))
        """, (uid, message_id, sent_at, kind, status, size_bytes, receipts, lines, discard_size_estimate))

    def record_usage(self, uid, message_id, sent_at, kind, status, *, size_bytes=None, receipts=None, lines=None):
        with self.connection(write=True) as conn:
            self._record_usage(conn, uid, message_id, sent_at, kind, status, size_bytes, receipts, lines)

    def usage_summary(self, days, owner_id):
        since, start, stop = usage_window(days)
        with self.connection() as conn:
            started = conn.execute("SELECT value FROM config WHERE key='usage_started_at'").fetchone()[0]
            rows = conn.execute("""
                WITH totals AS (
                    SELECT user_id,
                        SUM(kind='file') AS files,
                        SUM(kind='file' AND status='accepted') AS accepted,
                        SUM(kind='file' AND status='rejected') AS rejected,
                        SUM(kind='file' AND status='pending') AS pending,
                        SUM(kind='resume') AS resumes,
                        SUM(CASE WHEN kind='file' THEN size_bytes END) AS total_bytes,
                        MAX(CASE WHEN kind='file' THEN size_bytes END) AS max_bytes,
                        SUM(kind='file' AND size_bytes IS NULL) AS unknown_sizes,
                        SUM(receipts) AS receipts, SUM(lines) AS lines
                    FROM usage_events WHERE sent_at>=? AND sent_at<? GROUP BY user_id
                ), senders AS (
                    SELECT user_id FROM employees UNION SELECT user_id FROM totals UNION SELECT ?
                )
                SELECT CAST(senders.user_id AS TEXT) AS id,
                    CASE WHEN senders.user_id=? THEN 'Владелец'
                         ELSE COALESCE(employees.name,'Telegram '||senders.user_id) END AS name,
                    COALESCE(files,0) AS files, COALESCE(accepted,0) AS accepted,
                    COALESCE(rejected,0) AS rejected, COALESCE(pending,0) AS pending,
                    COALESCE(resumes,0) AS resumes, COALESCE(total_bytes,0) AS total_bytes,
                    max_bytes, COALESCE(unknown_sizes,0) AS unknown_sizes,
                    COALESCE(receipts,0) AS receipts, COALESCE(lines,0) AS lines
                FROM senders LEFT JOIN employees USING(user_id) LEFT JOIN totals USING(user_id)
                ORDER BY files DESC, total_bytes DESC, resumes DESC, name, senders.user_id
            """, (start, stop, owner_id, owner_id))
            return {"since": since, "started_at": int(started), "users": [dict(row) for row in rows]}

    def usage_detail(self, days, uid, before=None):
        _, start, stop = usage_window(days)
        uid = employee_id(uid)
        if before is not None and (type(before) is not int or not 0 < before <= 2**63 - 1):
            raise ValueError("Некорректная страница запросов.")
        cursor_clause = "" if before is None else " AND message_id<?"
        params = (uid, start, stop) if before is None else (uid, start, stop, before)
        with self.connection() as conn:
            records = conn.execute(f"""
                SELECT message_id,sent_at,kind,status,size_bytes,receipts,lines
                FROM usage_events WHERE user_id=? AND sent_at>=? AND sent_at<? {cursor_clause}
                ORDER BY message_id DESC LIMIT ?
            """, (*params, USAGE_PAGE_SIZE + 1)).fetchall()
        page = [dict(row) for row in records[:USAGE_PAGE_SIZE]]
        return {"requests": page, "next_before": page[-1]["message_id"] if len(records) > USAGE_PAGE_SIZE else None}

    def should_notify(self, uid, sent_at, *, album=None):
        """Coalesce refusal messages across polling retries, Telegram albums and restarts."""
        with self.connection(write=True) as conn:
            now = int(time.time())
            cutoff = now - INTAKE_RETENTION
            conn.execute("DELETE FROM intake_notices WHERE notified_at<=?", (cutoff,))
            conn.execute("DELETE FROM intake_album_notices WHERE notified_at<=?", (cutoff,))
            last = conn.execute("SELECT sent_at,notified_at FROM intake_notices WHERE user_id=?", (uid,)).fetchone()
            if last and (sent_at - last["sent_at"] < 30 or now - last["notified_at"] < 30):
                return False
            if album is not None:
                inserted = conn.execute("INSERT OR IGNORE INTO intake_album_notices VALUES (?,?,?,?)",
                                        (uid, album, now, sent_at)).rowcount
                if not inserted:
                    return False
            conn.execute("INSERT OR REPLACE INTO intake_notices VALUES (?,?,?,?)", (uid, sent_at, now, album))
            return True

    def release_notice(self, uid, sent_at, *, album=None):
        """Release only this notification when Telegram definitively declined delivery."""
        with self.connection(write=True) as conn:
            conn.execute("DELETE FROM intake_notices WHERE user_id=? AND sent_at=? AND album IS ?",
                         (uid, sent_at, album))
            if album is not None:
                conn.execute("DELETE FROM intake_album_notices WHERE user_id=? AND sent_at=? AND album=?",
                             (uid, sent_at, album))

    def update_job(self, user_id, version, status, pending=None):
        with self.connection(write=True) as conn:
            return bool(conn.execute("UPDATE jobs SET status=?,pending=? WHERE user_id=? AND version=?",
                                     (status, json.dumps(pending, ensure_ascii=False), user_id, version)).rowcount)

    def get_offset(self):
        with self.connection() as conn:
            cursor = dict(conn.execute("SELECT key,value FROM config WHERE key IN ('offset','offset_saved_at')"))
        # Telegram retains pending updates for 24h and can pick a lower ID after a week
        # of inactivity. After 48h (or on upgrade from an undated cursor), ask for
        # the earliest unconfirmed update before acknowledging any newer IDs.
        saved_at = cursor.get("offset_saved_at")
        if saved_at is None or time.time() - int(saved_at) >= 48 * 3600:
            return 0
        return int(cursor["offset"])

    def set_offset(self, n):
        with self.connection(write=True) as conn:
            # One polling worker handles one update at a time. Reaching this
            # point means no download is still running or waiting to be retried.
            # Reconcile attempts whose update expired or whose sender lost access.
            conn.execute("UPDATE usage_events SET status='rejected' WHERE status='pending'")
            conn.executemany("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)",
                             [("offset", str(n)), ("offset_saved_at", str(int(time.time())))])
