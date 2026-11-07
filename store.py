"""Shared catalog and per-Telegram-user uploads on a persistent SQLite volume."""

import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError):
    """Another administrator changed the entry since it was opened."""


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
CREATE TABLE IF NOT EXISTS jobs (
    user_id INTEGER PRIMARY KEY, version TEXT NOT NULL, receipts TEXT NOT NULL,
    collected TEXT NOT NULL, status TEXT NOT NULL, pending TEXT
);
"""


class Store:
    def __init__(self, database, *, initial_catalog=None, require_catalog=False):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
        defaults = json.loads(Path(__file__).with_name("seed.json").read_text(encoding="utf-8"))
        with self.connection(write=True) as conn:
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
        with self.connection(write=True) as conn:
            self._insert_point(conn, {"name": name, "address": address})

    def edit_point(self, name, address, excluded, previous=None):
        point = key(clean(name, "Точка", 500))
        address = clean(address, "Адрес", 500, empty=True)
        if type(excluded) is not bool:
            raise ValueError("Укажите, исключена ли точка из накладных.")
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT address,excluded FROM points WHERE key=?", (point,)).fetchone()
            if row is None:
                raise ValueError("Точка отсутствует в справочнике.")
            check_previous(previous, {"address": row["address"], "excluded": bool(row["excluded"])})
            conn.execute("UPDATE points SET address=?,excluded=? WHERE key=?", (address, excluded, point))

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

    def replace_job(self, user_id, version, receipts, collected):
        with self.connection(write=True) as conn:
            conn.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,'processing',NULL) ON CONFLICT(user_id) DO UPDATE SET "
                "version=excluded.version,receipts=excluded.receipts,collected=excluded.collected,"
                "status='processing',pending=NULL",
                (user_id, version, json.dumps(receipts, ensure_ascii=False), collected)
            )

    def get_job(self, user_id):
        with self.connection() as conn:
            row = conn.execute("SELECT version,receipts,collected,status,pending FROM jobs WHERE user_id=?",
                               (user_id,)).fetchone()
            if not row:
                return None
            return dict(row, receipts=json.loads(row["receipts"]), pending=json.loads(row["pending"] or "null"))

    def update_job(self, user_id, version, status, pending=None):
        with self.connection(write=True) as conn:
            return bool(conn.execute("UPDATE jobs SET status=?,pending=? WHERE user_id=? AND version=?",
                                     (status, json.dumps(pending, ensure_ascii=False), user_id, version)).rowcount)

    def get_offset(self):
        with self.connection() as conn:
            return int(conn.execute("SELECT value FROM config WHERE key='offset'").fetchone()[0])

    def set_offset(self, n):
        with self.connection(write=True) as conn:
            conn.execute("UPDATE config SET value=? WHERE key='offset'", (str(n),))
