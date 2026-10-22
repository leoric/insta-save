# Instagram → Downloads Telegram bot

Telegram-бот: отправляешь ему ссылку на пост, рилс или IGTV из Instagram, а он скачивает медиа (фото, видео, карусели) в `~/Downloads`.

Бот работает на твоём компьютере, пока запущен скрипт, и сохраняет файлы на этой же машине. Скачивание делает [Instaloader](https://github.com/instaloader/instaloader).

## Файлы

| Файл | Назначение |
|---|---|
| `bot.py` | сам бот |
| `.env` | настройки (токен, ID, логин) |
| `requirements.txt` | зависимости |

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Настройка

Заполни `.env` рядом со скриптом:

```env
TG_BOT_TOKEN=токен_от_BotFather
TG_ALLOWED_USER_ID=твой_telegram_id
IG_USERNAME=
DOWNLOAD_DIR=~/Downloads
```

| Переменная | Обязательна | Описание |
|---|---|---|
| `TG_BOT_TOKEN` | да | токен бота, выдаёт [@BotFather](https://t.me/BotFather) |
| `TG_ALLOWED_USER_ID` | да | твой Telegram user id (узнать у [@userinfobot](https://t.me/userinfobot)). Бот отвечает только этому пользователю |
| `IG_USERNAME` | нет | логин Instagram для сохранённой сессии; если сессия одна, определяется сам |
| `IG_BROWSER` | нет | браузер, из которого бот сам берёт cookies Instagram при каждом старте (`firefox`, `chrome` …). Нужен `browser_cookie3`; используется, если сохранённой сессии нет или она невалидна |
| `DOWNLOAD_DIR` | нет | папка для загрузок, по умолчанию `~/Downloads` |

Если используешь git, добавь `.env` в `.gitignore`, чтобы токен не попал в репозиторий.

## Запуск

```bash
python3 bot.py
```

Отправь боту в Telegram ссылку вида `https://www.instagram.com/p/XXXXXXXXXXX/`. Файлы сохраняются как `<username>_<shortcode>.jpg` или `<username>_<shortcode>.mp4`, где `<username>` — автор поста, а для каруселей добавляются суффиксы `_1`, `_2` и так далее. Описание (подпись) поста сохраняется рядом в `<username>_<shortcode>.txt`; если у поста нет описания, файла не будет.

## Логин в Instagram

Без входа Instagram часто отвечает 401/403, поэтому бот сам входит при запуске в таком порядке:

1. **Сохранённая сессия** Instaloader (`~/.config/instaloader/session-<логин>`). Если сессия одна, имя логина подхватывается автоматически, иначе задай `IG_USERNAME` в `.env`.
2. **Cookies браузера**, если задан `IG_BROWSER` (например `firefox`). Нужен пакет `browser_cookie3`, которого нет в `requirements.txt`: `pip install browser_cookie3`. В этом браузере должен быть выполнен вход в Instagram.
3. **Логин и пароль в терминале.** Бот спросит их через `getpass`. Пароль нигде не сохраняется, на диск попадает только сессия. Поддерживается 2FA. Пустой пароль — пропустить и работать без входа.

Поэтому в первый раз запусти бота в обычном терминале (`python3 bot.py`) и введи данные, дальше он будет использовать сохранённую сессию. Лучше использовать отдельный аккаунт, а не основной: Instagram иногда блокирует автоматизированный доступ.

Вручную сессию тоже можно создать: `instaloader --login твой_логин` или `instaloader --load-cookies firefox` (для второго нужен `browser_cookie3`).

Если сессия протухла, просто перезапусти бота, он предложит войти заново.

## Если перестало работать

Instaloader неофициальный, а Instagram периодически меняет API. Сначала обнови библиотеку:

```bash
pip install -U instaloader
```

Если сессия протухла, повтори логин.
