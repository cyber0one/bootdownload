# bot.py — نسخة محسّنة مع إصلاح async + rate limiting + كشف ملفات أفضل
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

# ─────────────────────────────────────────────
#  مجلد البوت (لإيجاد ملفات الكوكيز)
# ─────────────────────────────────────────────
BASE_DIR = pathlib.Path(__file__).resolve().parent

# ─────────────────────────────────────────────
#  إعدادات أساسية
# ─────────────────────────────────────────────
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("❌ BOT_TOKEN غير موجود في متغيرات البيئة.")

TG_LIMIT       = 48 * 1024 * 1024   # حد الرفع العملي لتيليجرام
TARGET_MARGIN  = 46 * 1024 * 1024   # هدف الضغط مع هامش أمان
AUDIO_KBIT     = 96                  # بيتريت الصوت عند إعادة الترميز
LADDER         = [480, 360, 240, 144, None]  # سلّم جودات التنزيل
RATE_LIMIT_SEC = 30                  # ثواني الانتظار بين طلبات المستخدم الواحد
MAX_WORKERS    = 4                   # عدد خيوط التنزيل المتوازية

# ─────────────────────────────────────────────
#  Aiogram v3
# ─────────────────────────────────────────────
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp  = Dispatcher()

URL_RX   = re.compile(r"https?://\S+")
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# تتبع آخر طلب لكل مستخدم
user_last_request: dict[int, float] = defaultdict(float)


# ══════════════════════════════════════════════
#  دوال مساعدة
# ══════════════════════════════════════════════

def site_key(url: str) -> str:
    """تحديد المنصة من الرابط."""
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
    """إرجاع مسار ملف الكوكيز إن وُجد."""
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
        raise FileNotFoundError(
            "⚠️ tiktok_cookies.txt غير موجود. ضعه في نفس مجلد bot.py."
        )
    return str(p) if p.exists() else None


def select_format(sk: str, h: int | None) -> str:
    """اختيار صيغة التنزيل بناءً على المنصة والجودة المطلوبة."""
    if h is None:
        return "best[ext=mp4]/best"
    if sk == "yt":
        return f"bv*[height<={h}]+ba/b[height<={h}][ext=mp4]/b[height<={h}]/best"
    # إنستغرام / تويتر / تيك توك: عادةً ملف واحد بدون مسارات منفصلة
    return f"best[height<={h}][ext=mp4]/best[height<={h}]/best"


def build_opts(tmpdir: str, sk: str, height: int | None) -> dict:
    """بناء خيارات yt-dlp."""
    opts = {
        "outtmpl"           : str(pathlib.Path(tmpdir) / "%(title).80s.%(ext)s"),
        "format"            : select_format(sk, height),
        "merge_output_format": "mp4",
        "quiet"             : True,
        "noprogress"        : True,
        "nocheckcertificate": True,
        "http_headers"      : {"Cookie": "CONSENT=YES+1"},
        "retries"           : 3,
        "fragment_retries"  : 3,
        "file_access_retries": 3,
    }
    ck = pick_cookiefile(sk)
    if ck:
        opts["cookiefile"] = ck
    return opts


def find_largest_file(folder: pathlib.Path) -> pathlib.Path | None:
    """
    البحث عن أكبر ملف في المجلد — أفضل من الاعتماد على prepare_filename
    لأن yt-dlp قد يغيّر الامتداد أو اسم الملف أثناء المعالجة.
    """
    files = [f for f in folder.iterdir() if f.is_file()]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_size)


def _sync_download(url: str, tmpdir: str, sk: str, height: int | None) -> tuple[pathlib.Path | None, float]:
    """
    تنزيل متزامن داخل Thread منفصل (يُستدعى عبر run_in_executor).
    يُرجع (مسار الملف, المدة بالثواني).
    """
    opts = build_opts(tmpdir, sk, height)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        duration = float(info.get("duration") or 0)

    fpath = find_largest_file(pathlib.Path(tmpdir))
    return fpath, duration


async def download_with_ladder(url: str, tmpdir: str, sk: str) -> tuple[pathlib.Path, float]:
    """
    يجرّب جودات متنازلة حتى يجد ملفاً ضمن حد تيليجرام.
    كل محاولة تعمل في Thread منفصل (✅ لا يوقف البوت).
    """
    loop = asyncio.get_running_loop()
    last_path: pathlib.Path | None = None
    last_duration: float = 0.0

    for h in LADDER:
        # مجلد مؤقت منفصل لكل محاولة حتى لا تتداخل الملفات
        sub = pathlib.Path(tmpdir) / f"attempt_{h or 'best'}"
        sub.mkdir(exist_ok=True)

        fpath, duration = await loop.run_in_executor(
            executor,
            _sync_download,
            url, str(sub), sk, h,
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
    """
    إعادة ترميز الفيديو لتصغير حجمه — تعمل في Thread منفصل.
    """
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
    # ✅ نستخدم asyncio.create_subprocess_exec في الدالة الـ async
    # لكن هنا نحن داخل Thread عادي فنستخدم subprocess مباشرة
    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg فشل:\n{err}")
    return out


async def ffmpeg_compress(src: pathlib.Path, duration_sec: float, max_h: int = 360) -> pathlib.Path:
    """
    ✅ نسخة async من الضغط — تشغّل ffmpeg في Thread منفصل حتى لا تجمّد البوت.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, _sync_ffmpeg, src, duration_sec, max_h)


def get_video_duration_ffprobe(path: pathlib.Path) -> float:
    """
    استخراج المدة بدقة عبر ffprobe كبديل احتياطي عن yt-dlp.
    """
    import subprocess, json
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(result.stdout)
        return float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def cleanup_dir(folder: pathlib.Path) -> None:
    """حذف الملفات المؤقتة بأمان."""
    for p in folder.rglob("*"):
        try:
            if p.is_file():
                p.unlink(missing_ok=True)
        except Exception:
            pass


# ══════════════════════════════════════════════
#  Handlers
# ══════════════════════════════════════════════

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

    # ─── Rate Limiting ───────────────────────
    if wait > 0:
        await m.answer(
            f"⏳ الرجاء الانتظار <b>{int(wait)+1}</b> ثانية قبل الطلب التالي."
        )
        return
    user_last_request[uid] = now

    url = URL_RX.search(m.text).group(0)
    sk  = site_key(url)

    # رسالة الانتظار القابلة للتعديل
    status_msg = await m.answer("🔄 جاري التنزيل… قد يستغرق هذا دقيقة.")

    with tempfile.TemporaryDirectory() as td:
        td_path = pathlib.Path(td)

        # ─── تنزيل ───────────────────────────
        try:
            fpath, duration = await download_with_ladder(url, td, sk)
        except FileNotFoundError as e:
            await status_msg.edit_text(f"❌ {e}")
            return
        except Exception as e:
            await status_msg.edit_text(
                f"❌ <b>فشل التنزيل:</b>\n<code>{str(e)[:400]}</code>"
            )
            return

        # ─── ضغط إن لزم ──────────────────────
        out_path = fpath
        if out_path.stat().st_size > TG_LIMIT:
            await status_msg.edit_text("🗜️ الملف كبير… جاري الضغط تلقائياً.")

            # استخدم ffprobe للمدة إن كانت غير موثوقة
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

        # ─── إرسال ───────────────────────────
        final_size_mb = out_path.stat().st_size / (1024 * 1024)

        if out_path.stat().st_size > TG_LIMIT:
            await status_msg.edit_text(
                f"⚠️ حجم الفيديو ({final_size_mb:.1f} ميغابايت) لا يزال أكبر من حد تيليجرام.\n"
                "جرّب رابطاً لمقطع أقصر."
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
            # fallback: أرسله كملف عادي
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


# ══════════════════════════════════════════════
#  تشغيل البوت
# ══════════════════════════════════════════════

async def main() -> None:
    print("✅ البوت يعمل…")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
