# bot.py — نسخة محسّنة مع curl_cffi لتيك توك
import os
import asyncio
import tempfile
import pathlib
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from yt_dlp import YoutubeDL

BASE_DIR = pathlib.Path(__file__).resolve().parent

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("❌ BOT_TOKEN غير موجود في متغيرات البيئة.")

TG_LIMIT       = 48 * 1024 * 1024
TARGET_MARGIN  = 46 * 1024 * 1024
AUDIO_KBIT     = 96
LADDER         = [480, 360, 240, 144, None]
RATE_LIMIT_SEC = 30
MAX_WORKERS    = 4

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp  = Dispatcher()

URL_RX   = re.compile(r"https?://\S+")
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

user_last_request: dict[int, float] = defaultdict(float)


def site_key(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "yt"
    if "instagram.com" in u:
        return "ig"
    if "twitter.com" in u or "x.com" in u:
        return "tw"
    if "tiktok.com" in u:
        return "tt"
    return "other"


def pick_cookiefile(sk: str) -> str | None:
    mapping = {
        "yt": "youtube_cookies.txt",
        "ig": "instagram_cookies.txt",
        "tw": "twitter_cookies.txt",
        "tt": "tiktok_cookies.txt",
    }
    fn = mapping.get(sk)
    if not fn:
        return None
    p = BASE_DIR / fn
    if sk == "tt" and not p.exists():
        raise FileNotFoundError("⚠️ tiktok_cookies.txt غير موجود.")
    return str(p) if p.exists() else None


def select_format(sk: str, h: int | None) -> str:
    if h is None:
        return "best[ext=mp4]/best"
    if sk == "yt":
        return f"bv*[height<={h}]+ba/b[height<={h}][ext=mp4]/b[height<={h}]/best"
    return f"best[height<={h}][ext=mp4]/best[height<={h}]/best"


def build_opts(tmpdir: str, sk: str, height: int | None) -> dict:
    opts = {
        "outtmpl"            : str(pathlib.Path(tmpdir) / "%(title).80s.%(ext)s"),
        "format"             : select_format(sk, height),
        "merge_output_format": "mp4",
        "quiet"              : True,
        "noprogress"         : True,
        "nocheckcertificate" : True,
        "http_headers"       : {"Cookie": "CONSENT=YES+1"},
        "retries"            : 3,
        "fragment_retries"   : 3,
        "file_access_retries": 3,
    }
    # ✅ تفعيل impersonation لتيك توك لتجاوز حماية الـ bot detection
    if sk == "tt":
        opts["impersonate"] = "chrome"

    ck = pick_cookiefile(sk)
    if ck:
        opts["cookiefile"] = ck
    return opts


def find_largest_file(folder: pathlib.Path) -> pathlib.Path | None:
    files = [f for f in folder.iterdir() if f.is_file()]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_size)


def _sync_download(url: str, tmpdir: str, sk: str, height: int | None) -> tuple[pathlib.Path | None, float]:
    opts = build_opts(tmpdir, sk, height)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        duration = float(info.get("duration") or 0)
    fpath = find_largest_file(pathlib.Path(tmpdir))
    return fpath, duration


async def download_with_ladder(url: str, tmpdir: str, sk: str) -> tuple[pathlib.Path, float]:
    loop = asyncio.get_running_loop()
    last_path: pathlib.Path | None = None
    last_duration: float = 0.0

    for h in LADDER:
        sub = pathlib.Path(tmpdir) / f"attempt_{h or 'best'}"
        sub.mkdir(exist_ok=True)

        fpath, duration = await loop.run_in_executor(
            executor, _sync_download, url, str(sub), sk, h,
        )

        if fpath is None:
            continue

        last_path, last_duration = fpath, duration

        if fpath.stat().st_size <= TG_LIMIT:
            return fpath, duration

    if last_path is None:
        raise RuntimeError("فشل التنزيل في جميع الجودات.")

    return last_path, last_duration


def _sync_ffmpeg(src: pathlib.Path, duration_sec: float, max_h: int = 360) -> pathlib.Path:
    import subprocess
    duration_sec = max(1.0, float(duration_sec or 1.0))
    audio_bps    = AUDIO_KBIT * 1000
    target_bps   = (TARGET_MARGIN * 8) / duration_sec
    video_bps    = max(180_000, int(target_bps - audio_bps))
    out = src.with_suffix(".compressed.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf",
        f"scale='if(gt(iw,ih),-2,min({max_h},ih))':'if(gt(iw,ih),min({max_h},iw),-2)',"
        "setsar=1:1",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", str(video_bps),
        "-maxrate", str(int(video_bps * 1.2)),
        "-bufsize", str(int(video_bps * 2)),
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", f"{AUDIO_KBIT}k", "-ac", "2",
        str(out),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg فشل:\n{err}")
    return out


async def ffmpeg_compress(src: pathlib.Path, duration_sec: float, max_h: int = 360) -> pathlib.Path:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _sync_ffmpeg, src, duration_sec, max_h)


def get_video_duration_ffprobe(path: pathlib.Path) -> float:
    import subprocess, json
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def cleanup_dir(folder: pathlib.Path) -> None:
    for p in folder.rglob("*"):
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
        except Exception:
            pass


@dp.message(Command("test"))
async def test_cmd(m: Message) -> None:
    import subprocess
    await m.answer("🔍 جاري الاختبار… انتظر.")
    cookie_path = str(BASE_DIR / "tiktok_cookies.txt")
    test_url    = "https://www.tiktok.com/@tiktok/video/7106594312292453675"
    result = subprocess.run(
        ["yt-dlp", "--cookies", cookie_path, "--impersonate", "chrome",
         "--get-title", "--no-warnings", test_url],
        capture_output=True, text=True, timeout=30,
    )
    stdout = result.stdout.strip()[:300] or "— لا يوجد —"
    stderr = result.stderr.strip()[:400] or "— لا يوجد —"
    status = "✅ نجح!" if result.returncode == 0 else "❌ فشل"
    await m.answer(
        f"<b>نتيجة الاختبار: {status}</b>\n\n"
        f"<b>stdout:</b>\n<code>{stdout}</code>\n\n"
        f"<b>stderr:</b>\n<code>{stderr}</code>"
    )


@dp.message(Command("start"))
async def start_cmd(m: Message) -> None:
    await m.answer(
        "👋 <b>أهلاً!</b>\n\n"
        "أرسل رابط فيديو من:\n"
        "• يوتيوب\n"
        "• إنستغرام\n"
        "• تويتر / X\n"
        "• تيك توك\n\n"
        "سأحاول تنزيله بأنسب جودة تلائم حد تيليجرام (50 ميغابايت)، "
        "وإن احتجت سأضغطه تلقائياً.\n\n"
        "⏳ <i>الطلبات الكبيرة تأخذ دقيقة أو أكثر، يُرجى الصبر.</i>"
    )


@dp.message(Command("help"))
async def help_cmd(m: Message) -> None:
    await m.answer(
        "📖 <b>طريقة الاستخدام:</b>\n\n"
        "فقط أرسل الرابط مباشرةً وسأتكفل بالباقي.\n\n"
        "<b>ملاحظات:</b>\n"
        "• حد التيليجرام 50 ميغابايت؛ سيُضغط الفيديو إن تجاوزه.\n"
        "• إنستغرام وتيك توك قد يحتاجان ملفات كوكيز.\n"
        "• انتظر 30 ثانية بين كل طلب وآخر."
    )


@dp.message(F.text.regexp(URL_RX))
async def handle_url(m: Message) -> None:
    uid  = m.from_user.id
    now  = time.monotonic()
    wait = RATE_LIMIT_SEC - (now - user_last_request[uid])

    if wait > 0:
        await m.answer(f"⏳ الرجاء الانتظار <b>{int(wait)+1}</b> ثانية قبل الطلب التالي.")
        return
    user_last_request[uid] = now

    url = URL_RX.search(m.text).group(0)
    sk  = site_key(url)

    status_msg = await m.answer("🔄 جاري التنزيل… قد يستغرق هذا دقيقة.")

    with tempfile.TemporaryDirectory() as td:
        td_path = pathlib.Path(td)

        try:
            fpath, duration = await download_with_ladder(url, td, sk)
        except FileNotFoundError as e:
            await status_msg.edit_text(f"❌ {e}")
            return
        except Exception as e:
            err_str = str(e)
            if "TikTok" in err_str and "Unable to extract" in err_str:
                await status_msg.edit_text(
                    "❌ <b>فشل تيك توك</b>\n\n"
                    "جرّب:\n"
                    "• أرسل /test للتشخيص\n"
                    "• جدّد tiktok_cookies.txt"
                )
            else:
                await status_msg.edit_text(
                    f"❌ <b>فشل التنزيل:</b>\n<code>{err_str[:400]}</code>"
                )
            return

        out_path = fpath
        if out_path.stat().st_size > TG_LIMIT:
            await status_msg.edit_text("🗜️ الملف كبير… جاري الضغط تلقائياً.")
            if duration < 1:
                duration = get_video_duration_ffprobe(out_path)
            try:
                out_path = await ffmpeg_compress(out_path, duration, max_h=360)
            except Exception as e:
                await status_msg.edit_text(
                    f"❌ <b>فشل الضغط:</b>\n<code>{str(e)[:400]}</code>"
                )
                cleanup_dir(td_path)
                return

        final_size_mb = out_path.stat().st_size / (1024 * 1024)

        if out_path.stat().st_size > TG_LIMIT:
            await status_msg.edit_text(
                f"⚠️ حجم الفيديو ({final_size_mb:.1f} ميغابايت) أكبر من حد تيليجرام.\n"
                "جرّب مقطعاً أقصر."
            )
            cleanup_dir(td_path)
            return

        await status_msg.edit_text("📤 جاري الرفع…")
        try:
            await m.answer_video(
                FSInputFile(str(out_path)),
                caption=f"✅ تم التنزيل ({final_size_mb:.1f} MB)",
                supports_streaming=True,
            )
            await status_msg.delete()
        except Exception:
            try:
                await m.answer_document(
                    FSInputFile(str(out_path)),
                    caption=f"✅ تم التنزيل ({final_size_mb:.1f} MB)",
                )
                await status_msg.delete()
            except Exception as e:
                await status_msg.edit_text(
                    f"❌ <b>فشل الإرسال:</b>\n<code>{str(e)[:400]}</code>"
                )
        finally:
            cleanup_dir(td_path)


async def main() -> None:
    print("✅ البوت يعمل…")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
