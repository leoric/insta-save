#!/usr/bin/env python3
"""
Telegram-бот: присылаешь ссылку на пост/рилс Instagram -> медиа и описание сохраняются в ~/Downloads.

Установка:
    pip install python-telegram-bot instaloader python-dotenv

Настройки лежат в файле .env рядом со скриптом (см. .env).

Запуск:
    python3 bot.py

Вход в Instagram бот делает сам при запуске (без входа Instagram часто отдаёт 401/403):
  1. берёт сохранённую сессию Instaloader (~/.config/instaloader/session-<логин>);
  2. если её нет или она невалидна и задан IG_BROWSER — импортирует cookies из браузера;
  3. иначе спрашивает логин и пароль в терминале (getpass). Пароль нигде не хранится,
     сохраняется только сессия. Пустой пароль — пропустить и работать без входа.
"""

import asyncio
import getpass
import glob
import logging
import os
import re
import sys
from pathlib import Path

import instaloader
from dotenv import load_dotenv
from instaloader.instaloader import get_default_session_filename
from telegram import Update
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv(Path(__file__).resolve().parent / ".env")

TOKEN = os.environ["TG_BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["TG_ALLOWED_USER_ID"])
IG_USERNAME = os.environ.get("IG_USERNAME") or None
# Браузер, из которого брать cookies Instagram при старте (firefox, chrome...). Нужен browser_cookie3
IG_BROWSER = os.environ.get("IG_BROWSER") or None

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "~/Downloads")).expanduser()

def pretty_dir(path: Path) -> str:
    """Показывает путь с ~ вместо домашней папки."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)"
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # в URL запросов виден токен
log = logging.getLogger("bot")


def make_loader() -> instaloader.Instaloader:
    loader = instaloader.Instaloader(
        dirname_pattern=str(DOWNLOAD_DIR),
        filename_pattern="{profile}_{shortcode}",
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern="{caption}",  # описание поста -> <автор>_<shortcode>.txt (если оно есть)
        quiet=True,
        request_timeout=30,  # по умолчанию 300 с: зависший запрос блокировал бота на минуты
        max_connection_attempts=2,
    )
    return loader


LOADER = make_loader()
LOGGED_IN = False  # выставляется в main() после ensure_login


def _saved_session_usernames() -> list[str]:
    session_dir = Path(get_default_session_filename("x")).parent
    return sorted(p.name[len("session-"):] for p in session_dir.glob("session-*"))


def _interactive_login(loader: instaloader.Instaloader, username: str | None) -> bool:
    if not sys.stdin.isatty():
        log.warning("Нет интерактивного терминала: ввод пароля недоступен")
        return False

    print("\nВход в Instagram (пароль нигде не сохраняется, пустой пароль — пропустить)")
    if not username:
        username = input("Instagram логин (Enter — пропустить): ").strip()
        if not username:
            return False

    for _ in range(3):
        password = getpass.getpass(f"Пароль для {username}: ")
        if not password:
            return False
        try:
            loader.login(username, password)
        except instaloader.exceptions.BadCredentialsException:
            print("Неверный пароль, попробуй ещё раз.")
            continue
        except instaloader.exceptions.TwoFactorAuthRequiredException:
            try:
                loader.two_factor_login(input("Код 2FA: ").strip())
            except instaloader.exceptions.InstaloaderException as e:
                print(f"Не удалось войти: {e}")
                return False
        except instaloader.exceptions.InstaloaderException as e:
            print(f"Не удалось войти: {e}")
            return False
        finally:
            password = ""  # пароль нигде не сохраняется
        loader.save_session_to_file()
        log.info("Вход выполнен, сессия сохранена для %s", username)
        return True
    return False


def ensure_login(loader: instaloader.Instaloader) -> bool:
    """Сессия -> cookies браузера -> логин с паролем. True, если вход выполнен."""
    username = IG_USERNAME
    if not username:
        saved = _saved_session_usernames()
        if len(saved) == 1:
            username = saved[0]

    if username:
        try:
            loader.load_session_from_file(username)
            if loader.test_login():
                log.info("Сессия Instagram для %s валидна", username)
                return True
            log.warning("Сессия Instagram для %s невалидна (или нет сети)", username)
        except FileNotFoundError:
            log.info("Сохранённой сессии для %s нет", username)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось загрузить сессию: %s", e)

    if IG_BROWSER:
        try:
            from instaloader.__main__ import import_session

            import_session(IG_BROWSER, loader, None)
            log.info(
                "Сессия Instagram взята из браузера %s (пользователь %s)",
                IG_BROWSER,
                loader.context.username,
            )
            return True
        except Exception as e:  # noqa: BLE001
            log.warning(
                "Не удалось взять сессию из браузера %s: %s. "
                "Нужен pip install browser_cookie3 и вход в Instagram в этом браузере",
                IG_BROWSER,
                e,
            )

    return _interactive_login(loader, username)


# Instaloader не потокобезопасен, поэтому скачиваем по одной ссылке за раз,
# но сам бот при этом продолжает принимать новые сообщения.
DOWNLOAD_LOCK = asyncio.Lock()
DOWNLOAD_TIMEOUT = 180  # секунд на одну попытку
MAX_ATTEMPTS = 3  # попыток на ссылку, если Instagram отказал
RETRY_DELAYS = (5, 20)  # паузы перед 2-й и 3-й попыткой, секунд
PAUSE_BETWEEN_POSTS = 3  # пауза после каждой ссылки, секунд


MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".txt"}


def _find_files(shortcode: str) -> list[Path]:
    return sorted(
        p
        for p in DOWNLOAD_DIR.glob(f"*_{glob.escape(shortcode)}*")
        if p.suffix.lower() in MEDIA_SUFFIXES
    )


def _collect_files(shortcode: str) -> list[Path]:
    files = _find_files(shortcode)
    # Instaloader ставит файлам дату публикации поста; touch обновляет её на текущую,
    # чтобы свежескачанное было сверху при сортировке по дате в Finder
    for f in files:
        f.touch()
    return files


def _download_via_mobile_api(shortcode: str) -> None:
    """Запасной путь, если веб-метаданные Instaloader не получены: мобильный API Instagram.
    Сначала скачиваются файлы, описание сохраняется после них, и его ошибки не критичны."""
    mediaid = instaloader.Post.shortcode_to_mediaid(shortcode)
    resp = LOADER.context.get_iphone_json(path=f"api/v1/media/{mediaid}/info/", params={})
    items = resp.get("items") or []
    if not items:
        raise instaloader.exceptions.BadResponseException("Мобильный API не вернул пост")
    item = items[0]

    username = (item.get("user") or {}).get("username") or "unknown"
    base = f"{username}_{shortcode}"
    nodes = item.get("carousel_media") or [item]

    saved = 0
    for i, node in enumerate(nodes, start=1):
        video = (node.get("video_versions") or [None])[0]
        if video:
            url, ext = video["url"], "mp4"
        else:
            candidates = (node.get("image_versions2") or {}).get("candidates") or []
            if not candidates:
                continue
            url, ext = candidates[0]["url"], "jpg"
        name = f"{base}.{ext}" if len(nodes) == 1 else f"{base}_{i}.{ext}"
        LOADER.context.get_and_write_raw(url, str(DOWNLOAD_DIR / name))
        saved += 1
    if not saved:
        raise instaloader.exceptions.BadResponseException("В ответе мобильного API нет медиа")

    try:
        caption = ((item.get("caption") or {}).get("text") or "").strip()
        if caption:
            (DOWNLOAD_DIR / f"{base}.txt").write_text(caption, encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.warning("Описание не сохранилось, файлы скачаны", exc_info=True)


def download_post(shortcode: str) -> tuple[list[Path], bool]:
    """Блокирующая загрузка; вызывается через asyncio.to_thread.
    Возвращает (файлы, были_ли_ошибки_Instagram_при_загрузке)."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    auth_errors = (
        instaloader.exceptions.LoginRequiredException,
        instaloader.exceptions.BadResponseException,
    )

    try:
        post = instaloader.Post.from_shortcode(LOADER.context, shortcode)
    except auth_errors as first_error:
        log.warning(
            "Метаданные через веб-API не получены (%s), пробую мобильный API", first_error
        )
        try:
            _download_via_mobile_api(shortcode)
        except instaloader.exceptions.InstaloaderException as e:
            log.warning("Мобильный API тоже не сработал: %s", e, exc_info=True)
            raise first_error
        return _collect_files(shortcode), False

    had_error = False
    try:
        LOADER.download_post(post, target=shortcode)
    except auth_errors as e:
        # Медиа могло скачаться, а упал дополнительный запрос (например, за описанием).
        # Если файлы лежат на диске, считаем загрузку состоявшейся, но предупреждаем.
        log.warning("Instaloader вернул ошибку при загрузке %s: %s", shortcode, e, exc_info=True)
        if not _find_files(shortcode):
            raise
        had_error = True
    return _collect_files(shortcode), had_error


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    if user.id != ALLOWED_USER_ID:
        log.warning(
            "Сообщение от неразрешённого пользователя id=%s (в .env: %s)",
            user.id,
            ALLOWED_USER_ID,
        )
        return
    log.info("Получено сообщение (отправлено %s): %r", msg.date, msg.text)

    match = URL_RE.search(msg.text or "")
    if not match:
        await msg.reply_text("Пришли ссылку на пост, рилс или IGTV из Instagram.")
        return

    shortcode = match.group(1)
    status = await msg.reply_text("Скачиваю…")
    try:
        async with DOWNLOAD_LOCK:
            for attempt in range(MAX_ATTEMPTS):
                try:
                    files, had_error = await asyncio.wait_for(
                        asyncio.to_thread(download_post, shortcode), timeout=DOWNLOAD_TIMEOUT
                    )
                    break
                except (
                    instaloader.exceptions.LoginRequiredException,
                    instaloader.exceptions.BadResponseException,
                ) as e:
                    if attempt == MAX_ATTEMPTS - 1:
                        raise
                    delay = RETRY_DELAYS[attempt]
                    log.warning(
                        "Попытка %d/%d для %s не удалась (%s), повтор через %d с",
                        attempt + 1, MAX_ATTEMPTS, shortcode, e, delay,
                    )
                    await status.edit_text(
                        f"Instagram отказал, повторяю через {delay} с (попытка {attempt + 2}/{MAX_ATTEMPTS})…"
                    )
                    await asyncio.sleep(delay)
            # пауза перед следующей ссылкой в очереди, чтобы не упираться в лимиты Instagram
            await asyncio.sleep(PAUSE_BETWEEN_POSTS)
    except asyncio.TimeoutError:
        await status.edit_text("Instagram не отвечает (таймаут). Попробуй ещё раз позже.")
        return
    except (
        instaloader.exceptions.LoginRequiredException,
        instaloader.exceptions.BadResponseException,
    ) as e:
        log.warning("Instagram отказал в доступе: %s", e, exc_info=True)
        hint = (
            "Сессия, возможно, устарела: перезапусти бота, он предложит войти заново."
            if LOGGED_IN
            else "Бот работает без входа в Instagram: перезапусти его в терминале и введи логин и пароль."
        )
        await status.edit_text(f"Instagram не отдал пост (нужна авторизация или лимит запросов). {hint}")
        return
    except instaloader.exceptions.InstaloaderException as e:
        log.exception("Ошибка Instaloader")
        await status.edit_text(f"Не получилось скачать: {e}")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("Неожиданная ошибка")
        await status.edit_text(f"Ошибка: {e}")
        return

    if not files:
        await status.edit_text("Загрузка завершилась, но файлов не найдено.")
        return

    names = "\n".join(f"• {p.name}" for p in files)
    text = f"Сохранено в {pretty_dir(DOWNLOAD_DIR)}:\n{names}"
    if had_error:
        text += "\n⚠️ Instagram вернул ошибку при загрузке: описание или часть файлов могли не скачаться."
    await status.edit_text(text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сетевые сбои — одной строкой, остальное — с traceback."""
    err = context.error
    if isinstance(err, NetworkError):
        log.warning("Проблема с сетью, Telegram-клиент переподключится сам: %s", err)
    else:
        log.error("Необработанная ошибка", exc_info=err)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает Telegram ID, чтобы вписать его в TG_ALLOWED_USER_ID."""
    if update.effective_message and update.effective_user:
        await update.effective_message.reply_text(
            f"Твой Telegram ID: {update.effective_user.id}"
        )


def main() -> None:
    global LOGGED_IN
    LOGGED_IN = ensure_login(LOADER)
    if not LOGGED_IN:
        log.warning("Работаю без входа в Instagram: возможны ошибки 401/403")

    # concurrent_updates: долгая загрузка не блокирует приём остальных сообщений
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Бот запущен. Папка загрузки: %s", pretty_dir(DOWNLOAD_DIR))
    # Сообщения, отправленные пока бот был выключен, не отбрасываем
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
