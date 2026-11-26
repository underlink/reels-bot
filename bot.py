"""Telegram-бот: надсилаєш посилання на рілз з Instagram або Facebook — отримуєш відео."""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import ReplyKeyboardRemove, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from yt_dlp import YoutubeDL

import diag

load_dotenv(Path(__file__).with_name(".env"))

BOT_TOKEN = os.environ.get("BOT_TOKEN")
# На Render задається автоматично — тоді бот працює через webhook, локально — через polling
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
# Сирий секрет з Render → Environment — ключ для завантаження діагностичних файлів
DIAG_KEY = WEBHOOK_SECRET
# Telegram accepts only A-Z, a-z, 0-9, underscores and hyphens.
if WEBHOOK_SECRET:
    WEBHOOK_SECRET = hashlib.sha256(WEBHOOK_SECRET.encode()).hexdigest()
PORT = int(os.environ.get("PORT", "10000"))
# Діагностика 500 на sendVideo. На безкоштовному Render /tmp очищується при перезапуску й засинанні
DIAG_DIR = Path(os.environ.get("DIAG_DIR", "/tmp/diag"))
DIAG_FILES = {"last_failed.mp4", "last_failed.json", "results.json"}
CONTROL_MP4 = Path(__file__).with_name("control.mp4")


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
    f"bv[vcodec^=avc1][height<={height}]+ba[acodec^=mp4a]/"
    f"b[vcodec^=avc1][acodec^=mp4a][height<={height}]/"
    f"bv[vcodec^=avc1][height<={height}]+ba/"
    f"b[vcodec^=avc1][height<={height}]/"
    f"b[ext=mp4][height<={height}]/"
    f"bv[ext=mp4][height<={height}]+ba[ext=m4a]/"
    f"bv[vcodec^=avc1][height<={height}]/"
    f"bv[ext=mp4][height<={height}]"
    for height in (1080, 720, 480)
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


def prepare_mp4(source: Path) -> Path:
    """Prepare H.264 MP4 for Premiere or keep the source MP4 as downloaded."""
    metadata = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    streams = json.loads(metadata.stdout)["streams"]
    video = next(stream for stream in streams if stream["codec_type"] == "video")
    audio = next((stream for stream in streams if stream["codec_type"] == "audio"), None)
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        if source.suffix.lower() == ".mp4":
            return source
        raise ValueError("Джерело не надало сумісний потік H.264 або файл MP4")
    audio_options = ["-c:a", "copy"]
    if audio and (audio.get("codec_name") != "aac" or audio.get("profile") != "LC"):
        audio_options = ["-c:a", "aac", "-profile:a", "aac_low", "-b:a", "128k"]

    output = source.with_name("reel.mp4")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source),
         "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", *audio_options,
         "-movflags", "+faststart", str(output)],
        capture_output=True, text=True, timeout=40, check=True,
    )
    return output


def download(url: str, workdir: Path) -> tuple[Path, dict]:
    """Повертає сумісний MP4 і його метадані."""
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
            if "Requested format is not available" in str(e):
                raise RuntimeError("Немає доступного відео у форматі MP4") from e
            last_error = e
            if "429" in str(e) or "Too Many Requests" in str(e):
                raise
            continue
        files = sorted(workdir.glob("video.*"), key=lambda p: p.stat().st_size, reverse=True)
        if files and files[0].stat().st_size <= MAX_BYTES:
            try:
                output = prepare_mp4(files[0])
            except (subprocess.SubprocessError, ValueError, KeyError, StopIteration) as error:
                log.warning("Не вдалося підготувати MP4: %s", error)
                last_error = RuntimeError("Не вдалося підготувати сумісний MP4")
                continue
            if 0 < output.stat().st_size <= MAX_BYTES:
                return output, probe(output)
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
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or update.message.caption or ""
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text("Надішли посилання на рілз з Instagram або Facebook.")
        return

    url = match.group(0)
    status = await update.message.reply_text("⏳ Завантажую…", reply_markup=ReplyKeyboardRemove())
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)

    workdir = Path(tempfile.mkdtemp(prefix="reel_"))
    path = None
    params = {}
    try:
        path, info = await asyncio.to_thread(download, url, workdir)
        params = {
            "supports_streaming": True,
            "width": info.get("width"),
            "height": info.get("height"),
            "duration": int(info["duration"]) if info.get("duration") else None,
        }
        with path.open("rb") as f:
            await update.message.reply_video(
                video=f,
                **params,
                read_timeout=120,
                write_timeout=120,
            )
    except Exception as e:  # noqa: BLE001
        log.exception("Помилка для %s", url)
        if path is not None and path.exists():
            # Файл уже завантажено — впало саме надсилання в Telegram. Зберігаємо точні байти.
            try:
                await asyncio.to_thread(save_failed, path, url, params, e, update.effective_chat.id)
            except Exception:
                log.exception("Не вдалося зберегти файл для діагностики")
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


def save_failed(path: Path, url: str, params: dict, error: Exception, chat_id: int) -> None:
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    target = DIAG_DIR / "last_failed.mp4"
    shutil.copyfile(path, target)
    meta = {
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url": url,
        "chat_id": chat_id,
        "params": params,
        "error": f"{type(error).__name__}: {error}",
        "traceback": "".join(traceback.format_exception(error))[-4000:],
        "probe": diag.probe(target),
        "versions": diag.versions(),
    }
    (DIAG_DIR / "last_failed.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    log.info("DIAG збережено last_failed.mp4: %s", json.dumps(meta["probe"], ensure_ascii=False))


async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Контрольний тест з Render: той самий код надсилання, що й `python diag.py matrix` на Mac."""
    chat_id = update.effective_chat.id
    files = [CONTROL_MP4]
    meta_path = DIAG_DIR / "last_failed.json"
    failed = DIAG_DIR / "last_failed.mp4"
    if failed.exists() and meta_path.exists():
        # Проблемний файл надсилаємо лише в чат, де він упав
        if json.loads(meta_path.read_text()).get("chat_id") == chat_id:
            files.append(failed)
    with_variants = "variants" in (context.args or [])
    await update.message.reply_text(
        f"🔬 Діагностика: {', '.join(f.name for f in files)}"
        + (" + перепаковані копії" if with_variants and len(files) > 1 else "")
        + ". Це займе 1–3 хв."
    )
    if with_variants and len(files) > 1:
        files += await asyncio.to_thread(diag.make_variants, failed, DIAG_DIR / "variants")
    report = await diag.run_matrix(BOT_TOKEN, str(chat_id), files, origin="render", log=log.info)
    DIAG_DIR.mkdir(parents=True, exist_ok=True)
    (DIAG_DIR / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    await update.message.reply_text(diag.summary(report)[:4000])


async def serve_webhook(app: Application) -> None:
    """Webhook на власному tornado-сервері: крім /webhook він віддає діагностичні файли
    окремим від Telegram каналом (GET /diag/<файл> з заголовком X-Diag-Key)."""
    import tornado.web

    class WebhookHandler(tornado.web.RequestHandler):
        async def post(self):
            token = self.request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if WEBHOOK_SECRET and not hmac.compare_digest(token, WEBHOOK_SECRET):
                self.set_status(403)
                return
            update = Update.de_json(json.loads(self.request.body), app.bot)
            await app.update_queue.put(update)

    class DiagHandler(tornado.web.RequestHandler):
        async def get(self, name):
            key = self.request.headers.get("X-Diag-Key", "")
            if not DIAG_KEY or not hmac.compare_digest(key, DIAG_KEY):
                self.set_status(403)
                return
            path = DIAG_DIR / name
            if name not in DIAG_FILES or not path.exists():
                self.set_status(404)
                return
            self.set_header("Content-Type", "video/mp4" if name.endswith(".mp4") else "application/json")
            self.set_header("Content-Length", str(path.stat().st_size))
            with path.open("rb") as f:
                while chunk := f.read(1 << 20):
                    self.write(chunk)
                    await self.flush()

    class HealthHandler(tornado.web.RequestHandler):
        def get(self):
            self.write("ok")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with app:
        await app.bot.set_webhook(
            url=f"{WEBHOOK_URL.rstrip('/')}/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
        )
        await app.start()
        server = tornado.web.Application([
            (r"/webhook", WebhookHandler),
            (r"/diag/([\w.]+)", DiagHandler),
            (r"/", HealthHandler),
        ]).listen(PORT, address="0.0.0.0")
        log.info("Бот запущено (webhook): %s", WEBHOOK_URL)
        await stop.wait()
        server.stop()
        await app.stop()


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задано BOT_TOKEN — створи файл .env (див. .env.example)")
    log.info("DIAG версії: %s", json.dumps(diag.versions(), ensure_ascii=False))
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("diag", diag_command, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    if WEBHOOK_URL:
        asyncio.run(serve_webhook(app))
    else:
        log.info("Бот запущено (polling)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
