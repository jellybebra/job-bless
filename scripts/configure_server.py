"""Prepare Docker settings and a separate SQLite copy without third-party packages."""

import argparse
import getpass
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")
sys.path.insert(0, str(ROOT))
from src.passwords import hash_password


def copy_database(source: Path, destination: Path):
    if destination.exists():
        raise ValueError(f"База уже существует: {destination}. Она не будет перезаписана.")
    if not source.is_file():
        raise ValueError(f"Исходная база не найдена: {source}")
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as original:
        tables = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "app_settings" not in tables or "vacancies" not in tables:
            raise ValueError("Это не база job-bless")
        with sqlite3.connect(destination) as copied:
            original.backup(copied)
            # Importing a local installation must not start sending applications.
            for key in ("schedule.enabled", "schedule.activity_enabled", "schedule.resume_touch_enabled", "llm.enabled", "browser.headless"):
                copied.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES (?, 'false')", (key,))
            copied.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES ('hh.login_required', 'true')")
            copied.commit()


def configure(args, password):
    env_path = args.output.resolve()
    if env_path.exists():
        raise ValueError(f"{env_path} уже существует. Настройки и ключи сохранены; для новой установки выберите --output.")
    if args.generate_password and env_path.with_name(env_path.name + ".password").exists():
        raise ValueError("Файл пароля уже существует. Выберите другой --output.")
    if not 1 <= args.port <= 65535:
        raise ValueError("Порт должен быть от 1 до 65535")
    if args.bind not in ("127.0.0.1", "0.0.0.0"):
        raise ValueError("--bind должен быть 127.0.0.1 или 0.0.0.0")
    if args.domain and not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", args.domain):
        raise ValueError("Укажите доменное имя без протокола и пути")
    encoded = hash_password(password)
    data = args.data.resolve()
    if any(character in str(data) for character in ("'", "\n", "\r")):
        raise ValueError("Путь содержит недопустимый символ")
    data.mkdir(parents=True, exist_ok=True)
    source = args.database
    if source is None and (ROOT / "data/career_agent.db").is_file():
        source = ROOT / "data/career_agent.db"
    if source:
        copy_database(source, data / "career_agent.db")
    # The containers run as uid 1000. Root-created bind mounts must be writable.
    if os.name != "nt" and os.geteuid() == 0:
        os.chown(data, 1000, 1000)
        if (data / "career_agent.db").exists():
            os.chown(data / "career_agent.db", 1000, 1000)
    values = {
        "JOB_BLESS_IMAGE_PREFIX": "ghcr.io/yn9m/job-bless",
        "JOB_BLESS_IMAGE_TAG": "latest",
        "WEB_PASSWORD_HASH": encoded,
        "AISTUDIO_SERVICE_KEY": secrets.token_urlsafe(32),
        "COMPOSE_PROFILES": "" if args.api_only else "google",
        "WEB_BIND": args.bind,
        "WEB_PORT": str(args.port),
        "JOB_BLESS_DATA_PATH": data.as_posix(),
        "SITE_DOMAIN": args.domain,
        "APP_UID": str(os.getuid() or 1000) if os.name != "nt" else "1000",
        "APP_GID": str(os.getgid() or 1000) if os.name != "nt" else "1000",
    }
    if args.api_only:
        values["AISTUDIO_SERVICE_URL"] = ""
    # Single quotes prevent Compose from interpolating any characters in paths.
    if any("'" in value or "\n" in value or "\r" in value for value in values.values()):
        raise ValueError("Путь или настройка содержит недопустимый символ")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    with env_path.open("x", encoding="utf-8") as output:
        output.write("\n".join(f"{key}='{value}'" for key, value in values.items()) + "\n")
    env_path.chmod(0o600)
    if args.generate_password:
        password_path = env_path.with_name(env_path.name + ".password")
        with password_path.open("x", encoding="utf-8") as output:
            output.write(password + "\n")
        password_path.chmod(0o600)
        print(f"Пароль панели сохранён в {password_path}")
    print(f"Настройки сохранены в {env_path}")
    print(f"Каталог данных: {data}")
    if source:
        print("Существующая база скопирована. Расписание выключено до проверки аккаунтов.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="", help="Домен для HTTPS, например jobs.example.com")
    parser.add_argument("--bind", default="127.0.0.1", help="0.0.0.0 для доступа по LAN")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--database", type=Path, help="Существующая SQLite-БД для отдельной копии")
    parser.add_argument("--data", type=Path, default=ROOT / "data/server")
    parser.add_argument("--output", type=Path, default=ROOT / ".env")
    parser.add_argument("--api-only", action="store_true", help="Без браузера Google; нейросеть подключается по API")
    parser.add_argument("--generate-password", action="store_true", help="Сохранить случайный пароль рядом с .env")
    args = parser.parse_args()
    try:
        password = secrets.token_urlsafe(18) if args.generate_password else getpass.getpass("Пароль панели (не менее 12 символов): ")
        if not args.generate_password and password != getpass.getpass("Повторите пароль: "):
            raise ValueError("Пароли не совпадают")
        configure(args, password)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, f"Ошибка: {error}\n")


if __name__ == "__main__":
    main()
