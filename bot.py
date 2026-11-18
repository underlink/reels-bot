"""Telegram-бот: надсилаєш посилання на рілз з Instagram або Facebook — отримуєш відео."""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Message, ReplyKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL

load_dotenv(Path(__file__).with_name(".env"))

BOT_TOKEN = os.environ.get("BOT_TOKEN")
# На Render задається автоматично — тоді бот працює через webhook, локально — через polling
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
# Telegram accepts only A-Z, a-z, 0-9, underscores and hyphens.
if WEBHOOK_SECRET:
    WEBHOOK_SECRET = hashlib.sha256(WEBHOOK_SECRET.encode()).hexdigest()
PORT = int(os.environ.get("PORT", "10000"))


def find_cookies() -> Path | None:
    """cookies.txt поруч із ботом або Secret File на Render (/etc/secrets/cookies.txt)."""
    for p in (Path(__file__).with_name("cookies.txt"), Path("/etc/secrets/cookies.txt")):
        if p.exists():
            return p
    return None


COOKIES_FILE = find_cookies()
# Ліміт Telegram Bot API на відправку файлу — 50 МБ
MAX_BYTES = 50 * 1024 * 1024

URL_RE = re.compile(
    r"https?://(?:www\.|m\.|web\.)?"
    r"(?:instagram\.com|facebook\.com|fb\.watch|fb\.com)/\S+",
    re.IGNORECASE,
)

# Від найкращої якості до найлегшої — беремо першу, що влазить у 50 МБ
FORMATS = [
    "bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4][height<=1080]/bv*[height<=1080]+ba/b",
    "bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4][height<=720]/bv*[height<=720]+ba/b",
    "bv*[height<=480]+ba/b[height<=480]/worst",
]

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("reels-bot")
# httpx пише в лог кожен запит разом з URL, а в ньому токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)


def probe(path: Path) -> dict:
    """Ширина, висота й тривалість готового файлу — щоб Telegram показав рілз вертикально."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout
        data = json.loads(out)
        stream = data["streams"][0]
        return {
            "width": stream.get("width"),
            "height": stream.get("height"),
            "duration": float(data["format"]["duration"]),
        }
    except Exception:  # noqa: BLE001 — без метаданих відео все одно відправиться
        return {}


def download(url: str, workdir: Path) -> tuple[Path, dict]:
    """Завантажує відео й повертає шлях до файлу та метадані."""
    last_error = None
    for fmt in FORMATS:
        for f in workdir.iterdir():
            f.unlink()
        opts = {
            "format": fmt,
            "outtmpl": str(workdir / "video.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
        }
        if COOKIES_FILE:
            # yt-dlp перезаписує cookie-файл, а /etc/secrets лише для читання — працюємо з копією
            cookie_copy = workdir.parent / f"{workdir.name}_cookies.txt"
            shutil.copy(COOKIES_FILE, cookie_copy)
            opts["cookiefile"] = str(cookie_copy)
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:  # noqa: BLE001 — yt-dlp кидає різні типи
            last_error = e
            if "429" in str(e) or "Too Many Requests" in str(e):
                raise
            continue
        files = sorted(workdir.glob("video.*"), key=lambda p: p.stat().st_size, reverse=True)
        if files and files[0].stat().st_size <= MAX_BYTES:
            return files[0], probe(files[0])
        last_error = RuntimeError("Відео більше за 50 МБ навіть у низькій якості")
    raise last_error or RuntimeError("Не вдалося завантажити відео")


def download_error_text(error: Exception) -> str:
    detail = str(error)
    if "429" in detail or "Too Many Requests" in detail:
        return (
            "❌ Instagram тимчасово обмежив запити із сервера (429). "
            "Завантаження зупинено. Спробуйте пізніше; "
            "повторне надсилання зараз не допоможе."
        )
    return (
        "❌ Не вдалося завантажити відео. "
        "Допис може бути недоступний або сервіс обмежив завантаження.\n\n"
        + detail[:300]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привіт! Надішли посилання на рілз з Instagram або Facebook — і я поверну відео.",
        reply_markup=SESSION_KEYBOARD,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or update.message.caption or ""
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text("Надішли посилання на рілз з Instagram або Facebook.")
        return

    url = match.group(0)
    status = await update.message.reply_text("⏳ Завантажую…")
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)

    workdir = Path(tempfile.mkdtemp(prefix="reel_"))
    try:
        path, info = await asyncio.to_thread(download, url, workdir)
        with path.open("rb") as f:
            sent_video = await update.message.reply_video(
                video=f,
                supports_streaming=True,
                width=info.get("width"),
                height=info.get("height"),
                duration=int(info["duration"]) if info.get("duration") else None,
                read_timeout=120,
                write_timeout=120,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("Помилка для %s", url)
        error_text = download_error_text(e)
        try:
            await status.edit_text(error_text)
        except Exception:
            log.exception("Не вдалося оновити статус")
            await update.message.reply_text(error_text)
        return
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        (workdir.parent / f"{workdir.name}_cookies.txt").unlink(missing_ok=True)

    try:
        await status.delete()
    except Exception:
        log.warning("Не вдалося прибрати статус завантаження", exc_info=True)

    if update.effective_chat.type == "private" and sent_video.video:
        items = context.chat_data.setdefault("session_videos", {})
        add_session_video(items, sent_video)
        await publish_session_summary(sent_video, context, items)


SESSION_KEYBOARD = ReplyKeyboardMarkup(
    [["Підрахувати", "Вивантажити сесію"], ["Нова сесія"]],
    resize_keyboard=True,
)


def clock_text(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02}:{seconds:02}"


async def session_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    items = context.chat_data.setdefault("session_videos", {})
    action = message.text
    if action == "Нова сесія":
        items.clear()
        await message.reply_text("Нову сесію розпочато. Надішли відео.", reply_markup=SESSION_KEYBOARD)
        return

    if message.video:
        add_session_video(items, message)
    elif message.document:
        await message.reply_text(
            "Надішли це відео як відеоповідомлення, щоб Telegram передав тривалість.",
            reply_markup=SESSION_KEYBOARD,
        )
        return

    if not items:
        await message.reply_text("Сесія порожня.", reply_markup=SESSION_KEYBOARD)
        return

    total = sum(items.values())
    if action == "Вивантажити сесію":
        manifest = {"messages": sorted(items), "seconds": total}
        await message.reply_text(
            "Запит на вивантаження на Mac.\nREELS_EXPORT_V1 " + json.dumps(manifest),
            reply_markup=SESSION_KEYBOARD,
        )
        return

    await publish_session_summary(message, context, items)


async def publish_session_summary(message: Message, context: ContextTypes.DEFAULT_TYPE, items: dict[int, int]) -> None:
    previous_id = context.chat_data.get("session_summary_id")
    summary = await message.reply_text(session_summary(items), reply_markup=SESSION_KEYBOARD)
    context.chat_data["session_summary_id"] = summary.message_id
    if previous_id is not None:
        try:
            await context.bot.delete_message(chat_id=message.chat_id, message_id=previous_id)
        except TelegramError:
            log.warning("Не вдалося видалити попередній підсумок сесії", exc_info=True)


def add_session_video(items: dict[int, int], message: Message) -> None:
    duration = message.video.duration
    items[message.message_id] = int(duration.total_seconds()) if hasattr(duration, "total_seconds") else duration


def session_summary(items: dict[int, int]) -> str:
    total = sum(items.values())
    if total < 480:
        target = f"до 8 хв ще {clock_text(480 - total)}"
    elif total <= 600:
        target = "у межах 8–10 хв"
    else:
        target = f"понад 10 хв на {clock_text(total - 600)}"
    return f"Відео: {len(items)} · хрон {clock_text(total)} · {target}"


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задано BOT_TOKEN — створи файл .env (див. .env.example)")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (
            filters.VIDEO | filters.Document.VIDEO
            | filters.Regex(r"^(Підрахувати|Вивантажити сесію|Нова сесія)$")
        ),
        session_message,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    if WEBHOOK_URL:
        log.info("Бот запущено (webhook): %s", WEBHOOK_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Бот запущено (polling)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
