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
from telegram import Update
from telegram.constants import ChatAction
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
            continue
        files = sorted(workdir.glob("video.*"), key=lambda p: p.stat().st_size, reverse=True)
        if files and files[0].stat().st_size <= MAX_BYTES:
            return files[0], probe(files[0])
        last_error = RuntimeError("Відео більше за 50 МБ навіть у низькій якості")
    raise last_error or RuntimeError("Не вдалося завантажити відео")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привіт! Надішли посилання на рілз з Instagram або Facebook — і я поверну відео."
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
            await update.message.reply_video(
                video=f,
                supports_streaming=True,
                width=info.get("width"),
                height=info.get("height"),
                duration=int(info["duration"]) if info.get("duration") else None,
                read_timeout=120,
                write_timeout=120,
            )
        await status.delete()
    except Exception as e:  # noqa: BLE001
        log.exception("Помилка для %s", url)
        await status.edit_text(
            "❌ Не вдалося завантажити відео.\n"
            "Можливо, допис приватний або потрібен вхід в акаунт (див. README про cookies.txt).\n\n"
            f"<code>{str(e)[:300]}</code>",
            parse_mode="HTML",
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        (workdir.parent / f"{workdir.name}_cookies.txt").unlink(missing_ok=True)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задано BOT_TOKEN — створи файл .env (див. .env.example)")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
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
