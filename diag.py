"""Діагностика 500 від Telegram на sendVideo.

Один і той самий код надсилання запускається і на Render (команда /diag у боті),
і на Mac (python diag.py ...), щоб різницю в результатах не пояснював інший HTTP-клієнт.

Mac:
    python diag.py fetch  --base https://reels-bot-xxxx.onrender.com --key WEBHOOK_SECRET
    python diag.py matrix --chat-id 123456 control.mp4 diag_files/last_failed.mp4 --variants
Токен береться з BOT_TOKEN (.env поруч або змінна середовища).
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

import httpx

API = "https://api.telegram.org/bot{token}/{method}"
# Однакові тайм-аути на обох сторонах — як у reply_video бота
TIMEOUT = httpx.Timeout(connect=15, read=120, write=120, pool=15)
RETRY_DELAY = 5


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def top_atoms(path: Path) -> list[str]:
    """Порядок атомів верхнього рівня MP4: moov перед mdat означає faststart."""
    atoms = []
    size_total = path.stat().st_size
    with path.open("rb") as f:
        pos = 0
        while pos + 8 <= size_total and len(atoms) < 50:
            f.seek(pos)
            header = f.read(8)
            size = int.from_bytes(header[:4], "big")
            name = header[4:8].decode("latin-1")
            if size == 1:
                size = int.from_bytes(f.read(8), "big")
            elif size == 0:
                size = size_total - pos
            atoms.append(name)
            if size < 8:
                break
            pos += size
    return atoms


def probe(path: Path) -> dict:
    """Технічний профіль файлу: розмір, хеш, атоми, потоки ffprobe."""
    info = {"file": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
    try:
        info["atoms"] = top_atoms(path)
    except Exception as e:  # noqa: BLE001
        info["atoms_error"] = repr(e)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        data = json.loads(out.stdout or "{}")
        info["format"] = {k: data.get("format", {}).get(k) for k in
                          ("format_name", "duration", "bit_rate", "start_time", "nb_streams")}
        keep = ("codec_type", "codec_name", "codec_tag_string", "profile", "level", "pix_fmt",
                "width", "height", "r_frame_rate", "avg_frame_rate", "start_time", "duration",
                "bit_rate", "color_transfer", "color_primaries", "sample_rate", "channels", "nb_frames")
        info["streams"] = []
        for s in data.get("streams", []):
            item = {k: s.get(k) for k in keep if s.get(k) is not None}
            side = [d.get("side_data_type") for d in s.get("side_data_list", [])]
            if side:
                item["side_data"] = side
            if s.get("tags", {}).get("rotate"):
                item["rotate"] = s["tags"]["rotate"]
            info["streams"].append(item)
        if out.stderr.strip():
            info["ffprobe_stderr"] = out.stderr.strip()[:500]
    except Exception as e:  # noqa: BLE001
        info["ffprobe_error"] = repr(e)
    return info


def versions() -> dict:
    v = {"python": sys.version.split()[0], "platform": platform.platform()}
    for pkg in ("python-telegram-bot", "httpx", "httpcore", "h11", "anyio", "yt-dlp"):
        try:
            v[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            v[pkg] = None
    try:
        v["ffmpeg"] = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                                     timeout=10).stdout.splitlines()[0]
    except Exception:  # noqa: BLE001
        v["ffmpeg"] = None
    return v


def make_variants(path: Path, outdir: Path) -> list[Path]:
    """Перепакована копія (без перекодування) і перекодована в H.264/AAC."""
    outdir.mkdir(parents=True, exist_ok=True)
    remux = outdir / f"{path.stem}_remux.mp4"
    h264 = outdir / f"{path.stem}_h264.mp4"
    cmds = [
        (remux, ["-c", "copy", "-movflags", "+faststart"]),
        (h264, ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-preset", "veryfast",
                "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]),
    ]
    made = []
    for out, args in cmds:
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), *args, str(out)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode == 0 and out.exists():
            made.append(out)
    return made


async def send_once(client: httpx.AsyncClient, token: str, chat_id: str, path: Path,
                    method: str, params: dict) -> dict:
    """Одна спроба. Файл щоразу відкривається заново, щоб повтор не пішов з порожнім тілом."""
    field = "video" if method == "sendVideo" else "document"
    data = {"chat_id": str(chat_id), "disable_notification": "true",
            **{k: str(v).lower() if isinstance(v, bool) else str(v)
               for k, v in params.items() if v is not None}}
    rec = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "method": method,
           "file": path.name, "bytes": path.stat().st_size, "params": params}
    t0 = time.monotonic()
    try:
        with path.open("rb") as fh:
            resp = await client.post(API.format(token=token, method=method), data=data,
                                     files={field: (path.name, fh, "video/mp4")})
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        rec["http_status"] = resp.status_code
        rec["headers"] = {k: resp.headers.get(k) for k in
                          ("date", "server", "content-type", "content-length", "retry-after")
                          if resp.headers.get(k)}
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:500]}
        if body.get("ok"):
            msg = body["result"]
            media = msg.get("video") or msg.get("document") or {}
            rec["ok"] = True
            rec["result"] = {"message_id": msg.get("message_id"), "kind": "video" if msg.get("video") else
                             "document" if msg.get("document") else "other",
                             "duration": media.get("duration"), "width": media.get("width"),
                             "height": media.get("height"), "file_size": media.get("file_size"),
                             "mime_type": media.get("mime_type")}
        else:
            rec["ok"] = False
            rec["error_code"] = body.get("error_code")
            rec["description"] = body.get("description") or body.get("raw")
            if body.get("parameters"):
                rec["parameters"] = body["parameters"]
    except Exception as e:  # noqa: BLE001
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        rec["ok"] = False
        rec["exception"] = f"{type(e).__name__}: {e}"[:500]
    return rec


async def run_matrix(token: str, chat_id: str, files: list[Path], origin: str,
                     log=print) -> dict:
    """Кожен файл: sendVideo з тими ж параметрами, що в боті, і sendDocument.
    Невдала спроба одразу повторюється через RETRY_DELAY с."""
    report = {"origin": origin, "versions": versions(), "files": [], "attempts": []}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for path in files:
            p = probe(path)
            report["files"].append(p)
            video = next((s for s in p.get("streams", []) if s.get("codec_type") == "video"), {})
            dur = p.get("format", {}).get("duration")
            video_params = {"supports_streaming": True, "width": video.get("width"),
                            "height": video.get("height"),
                            "duration": int(float(dur)) if dur else None}
            for method, params in (("sendVideo", video_params), ("sendDocument", {})):
                for attempt in (1, 2):
                    rec = await send_once(client, token, chat_id, path, method, params)
                    rec["attempt"] = attempt
                    rec["sha256"] = p["sha256"]
                    report["attempts"].append(rec)
                    log(json.dumps(rec, ensure_ascii=False))
                    if rec["ok"]:
                        break
                    await asyncio.sleep(RETRY_DELAY)
                # Контроль каналу: якщо надсилання впало, чи працює Bot API загалом
                if not report["attempts"][-1]["ok"]:
                    me = await client.get(API.format(token=token, method="getMe"))
                    report["attempts"][-1]["getMe_after"] = me.status_code
    return report


def summary(report: dict) -> str:
    lines = [f"Діагностика ({report['origin']}): "
             f"PTB {report['versions'].get('python-telegram-bot')}, "
             f"httpx {report['versions'].get('httpx')}"]
    for rec in report["attempts"]:
        res = "OK" if rec["ok"] else (f"{rec.get('error_code') or ''} {rec.get('description') or ''}"
                                      f"{rec.get('exception') or ''}").strip()
        lines.append(f"{rec['file']} · {rec['method']} #{rec['attempt']} · "
                     f"{rec['bytes'] / 1e6:.1f} МБ · {rec.get('elapsed_s')} с · {res}")
    return "\n".join(lines)


def _token() -> str:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).with_name(".env"))
    except ImportError:
        pass
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Задай BOT_TOKEN у .env поруч зі скриптом або в змінній середовища")
    return token


def cmd_fetch(args) -> None:
    """Забирає збережений файл із Render окремим каналом (HTTP, не Telegram) і звіряє SHA-256."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    headers = {"X-Diag-Key": args.key}
    base = args.base.rstrip("/")
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        meta = c.get(f"{base}/diag/last_failed.json", headers=headers)
        meta.raise_for_status()
        (out / "last_failed.json").write_bytes(meta.content)
        with c.stream("GET", f"{base}/diag/last_failed.mp4", headers=headers) as r:
            r.raise_for_status()
            with (out / "last_failed.mp4").open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        res = c.get(f"{base}/diag/results.json", headers=headers)
        if res.status_code == 200:
            (out / "render_results.json").write_bytes(res.content)
    expected = json.loads(meta.content)["probe"]["sha256"]
    actual = sha256(out / "last_failed.mp4")
    print(f"SHA-256 Render: {expected}\nSHA-256 Mac:    {actual}")
    print("✅ Файл збігається байт у байт" if expected == actual else "❌ Файл відрізняється — тест D недійсний")


def cmd_matrix(args) -> None:
    files = [Path(f) for f in args.files]
    if args.variants:
        for f in list(files):
            if f.name != "control.mp4":
                files += make_variants(f, Path(args.out) / "variants")
    report = asyncio.run(run_matrix(_token(), args.chat_id, files, origin=args.origin))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "mac_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n" + summary(report))
    print(f"\nПовний звіт: {out / 'mac_results.json'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="забрати last_failed.mp4 з Render")
    f.add_argument("--base", required=True, help="адреса сервісу, напр. https://reels-bot-xxxx.onrender.com")
    f.add_argument("--key", required=True, help="значення WEBHOOK_SECRET з Render → Environment")
    f.add_argument("--out", default="diag_files")
    m = sub.add_parser("matrix", help="надіслати файли тим самим кодом, що й /diag на Render")
    m.add_argument("--chat-id", required=True)
    m.add_argument("files", nargs="+")
    m.add_argument("--variants", action="store_true", help="додати перепаковану й H.264-копії")
    m.add_argument("--origin", default="mac")
    m.add_argument("--out", default="diag_files")
    args = ap.parse_args()
    {"fetch": cmd_fetch, "matrix": cmd_matrix}[args.cmd](args)


if __name__ == "__main__":
    main()
