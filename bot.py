# bot.py — robust for YouTube / Instagram / Twitter(X) on Render
import os, asyncio, tempfile, pathlib, re, math, subprocess
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from yt_dlp import YoutubeDL

# ====== Config ======
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN env var is missing.")

TG_LIMIT = 48 * 1024 * 1024            # حد عملي للرفع من البوت
TARGET_MARGIN = 46 * 1024 * 1024       # هدف ضغط دون الحد بهامش
AUDIO_KBIT = 96                        # بيتريت صوت عند الضغط
LADDER = [360, 240, 144, None]         # سلّم الجودات

# ====== Aiogram v3 ======
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
URL_RX = re.compile(r"https?://\S+")

def site_key(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "yt"
    if "instagram.com" in u:
        return "ig"
    if "twitter.com" in u or "x.com" in u:
        return "tw"
    return "other"

def pick_cookiefile(sk: str) -> str | None:
    # ملفات كوكيز اختيارية؛ إن وُجدت تُضاف
    mapping = {
        "yt": "youtube_cookies.txt",
        "ig": "instagram_cookies.txt",
        "tw": "twitter_cookies.txt",
    }
    fn = mapping.get(sk)
    if not fn:
        return None
    p = pathlib.Path(fn)
    return str(p) if p.exists() else None

def select_format(sk: str, h: int | None) -> str:
    """
    صيغة مرنة:
      - لو h موجود: نجرّب streams <= h ثم fallback إلى best
      - مواقع DASH (يوتيوب غالبًا): bestvideo+bestaudio أولًا
      - مواقع single-file (IG/Twitter): best[height<=h]/best
    """
    if h is None:
        # fallback النهائي
        # نعطي أولوية mp4 ثم best
        return "best[ext=mp4]/best"

    if sk == "yt":
        return f"bv*[height<={h}]+ba/b[height<={h}][ext=mp4]/b[height<={h}]/best"
    elif sk in ("ig", "tw"):
        # في تويتر/إنستغرام أحياناً لا توجد مسارات منفصلة للصوت
        return f"best[height<={h}][ext=mp4]/best[height<={h}]/best"
    else:
        # عام
        return f"best[height<={h}][ext=mp4]/best[height<={h}]/best"

def build_opts(tmpdir: str, sk: str, height: int | None):
    out_tmpl = str(pathlib.Path(tmpdir) / "%(title).80s.%(ext)s")

    fmt = select_format(sk, height)

    opts = {
        "outtmpl": out_tmpl,
        "format": fmt,
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "http_headers": {"Cookie": "CONSENT=YES+1"},
        "retries": 3,
    }

    ck = pick_cookiefile(sk)
    if ck:
        opts["cookiefile"] = ck

    # تحسينات صغيرة لبعض المواقع
    # مثال: تعطيل hls_live_start_index في البث المباشر قد يسبب مشاكل، لذا نتركه افتراضياً

    return opts

def ffmpeg_transcode_fit(src: pathlib.Path, duration_sec: float, max_h: int = 360) -> pathlib.Path:
    """
    ضغط بالفيديو H.264 والصوت AAC للوصول تحت TARGET_MARGIN تقريبياً.
    """
    duration_sec = max(1.0, float(duration_sec or 1.0))
    audio_bps = AUDIO_KBIT * 1000
    target_total_bps = (TARGET_MARGIN * 8) / duration_sec
    video_bps = max(180_000, int(target_total_bps - audio_bps))  # حد أدنى للفيديو

    out = src.with_suffix(".tgfit.mp4")

    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"scale='if(gt(iw,ih),-2,{max_h})':'if(gt(iw,ih),{max_h},-2)',"
               "setsar=1:1",  # الحفاظ على الأسبكت
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", str(video_bps),
        "-maxrate", str(int(video_bps * 1.2)),
        "-bufsize", str(int(video_bps * 2)),
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", f"{AUDIO_KBIT}k", "-ac", "2",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return out

async def download_with_ladder(url: str, tmpdir: str, sk: str):
    """
    نحاول بجودات منخفضة تدريجياً. نُرجع (Path, duration).
    لو ظلت كبيرة: نُرجع آخر ملف ونضغطه لاحقاً.
    """
    last_path = None
    last_duration = None
    for h in LADDER:
        with YoutubeDL(build_opts(tmpdir, sk, h)) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = float(info.get("duration") or 0)
            fpath = pathlib.Path(ydl.prepare_filename(info))
            mp4 = fpath.with_suffix(".mp4")
            if mp4.exists():
                fpath = mp4
        last_path, last_duration = fpath, duration
        if fpath.stat().st_size <= TG_LIMIT:
            return fpath, duration
    return last_path, last_duration

@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer(
        "✅ أرسل رابط فيديو (يوتيوب/إنستغرام/تويتر/X…). "
        "أحاول أولاً تنزيل جودة منخفضة تلائم حد تيليجرام، "
        "وإن لزم أضغطه تلقائيًا.\n"
        "ℹ️ لتحسين الاستخراج من IG/X ضع ملفات كوكيز: instagram_cookies.txt / twitter_cookies.txt في نفس المجلد."
    )

@dp.message(F.text.regexp(URL_RX))
async def handle_url(m: Message):
    url = URL_RX.search(m.text).group(0)
    await m.answer("🔄 جاري المعالجة…")

    sk = site_key(url)

    with tempfile.TemporaryDirectory() as td:
        td_path = pathlib.Path(td)
        try:
            fpath, duration = await download_with_ladder(url, td, sk)
        except Exception as e:
            await m.answer(f"❌ فشل التنزيل:\n<code>{e}</code>")
            return

        out_path = fpath
        if out_path.stat().st_size > TG_LIMIT:
            # محاولة ضغط أخيرة
            try:
                out_path = ffmpeg_transcode_fit(out_path, duration, max_h=360)
            except Exception as e:
                await m.answer(f"❌ لم أستطع ضغط الفيديو تلقائيًا:\n<code>{e}</code>")
                return

        try:
            if out_path.stat().st_size <= TG_LIMIT:
                await m.answer_document(FSInputFile(str(out_path)))
            else:
                await m.answer("⚠️ حتى بعد الضغط الحجم ما يزال أعلى من حد تيليجرام. جرّب مقطعًا أقصر أو جودة أقل.")
        finally:
            # تنظيف المؤقت
            for p in td_path.glob("*"):
                try: p.unlink(missing_ok=True)
                except: pass

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
