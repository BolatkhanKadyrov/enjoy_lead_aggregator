import json
import logging
import os
import signal
import threading
from pathlib import Path
from urllib.parse import urlsplit

from waitress import create_server

from bot import Bot
from store import IntakeLimits, Store
from web import WebApp


def admin_ids(value):
    parts = [v.strip() for v in value.replace("\n", ",").split(",") if v.strip()]
    if any(not v.isascii() or not v.isdecimal() or int(v) <= 0 for v in parts):
        raise ValueError("ADMIN_IDS должен содержать числовые ID личных чатов через запятую.")
    return {int(v) for v in parts}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Добавьте BOT_TOKEN в переменные окружения.")
    ids = admin_ids(os.getenv("ADMIN_IDS", ""))
    domain = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    public_url = os.getenv("PUBLIC_URL", "").strip() or ("https://" + domain if domain else "")
    if public_url:
        url = urlsplit(public_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise SystemExit("Для PUBLIC_URL нужен публичный HTTPS-адрес Mini App.")
    if not public_url:
        logging.warning("Для кнопки Mini App настройте домен Railway или PUBLIC_URL.")
    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if os.getenv("RAILWAY_SERVICE_ID") and not mount:
        raise SystemExit("Подключите постоянный Volume к сервису Railway, например в /data.")
    data_dir = Path(mount or os.getenv("DATA_DIR") or "data")
    seed_value = os.getenv("INITIAL_CATALOG_JSON", "").strip()
    seed_file = data_dir / "enjoy-catalog.json"
    if not seed_value and seed_file.exists():
        seed_value = seed_file.read_text(encoding="utf-8-sig")
    store = Store(data_dir / "enjoy.sqlite3", initial_catalog=json.loads(seed_value) if seed_value else None,
                  require_catalog=bool(os.getenv("RAILWAY_SERVICE_ID")))
    defaults = IntakeLimits()
    limits = IntakeLimits(
        cooldown=int(os.getenv("UPLOAD_COOLDOWN_SECONDS", defaults.cooldown)),
        per_user=int(os.getenv("UPLOADS_PER_USER_PER_10_MINUTES", defaults.per_user)),
        global_rate=int(os.getenv("UPLOADS_PER_MINUTE_TOTAL", defaults.global_rate)),
    )
    bot = Bot(store, token, ids, public_url, limits=limits)
    stop = threading.Event()
    worker = threading.Thread(target=bot.run, args=(stop,), name="telegram-poller", daemon=True)
    app = WebApp(store, token, ids, ready=lambda: worker.is_alive() and bot.connected)
    server = create_server(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")),
                           threads=4, max_request_body_size=16384, clear_untrusted_proxy_headers=True)
    def shutdown(*_):
        stop.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    worker.start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.close()
        worker.join(timeout=2)


if __name__ == "__main__":
    main()
