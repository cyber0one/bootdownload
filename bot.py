# bot.py — Aiogram v3 + yt-dlp مرن، يعمل Polling واحد فقط
import os, asyncio, tempfile, pathlib, re, math, subprocess
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from yt_dlp import YoutubeDL

# ====== إعدادات ======
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN env var is missing.")

TG_LIMIT = 48 * 1024 * 1024            # حد رفع عملي ~48MB
TARGET_MARGIN = 46 * 1024 * 1024       # هدف الضغط أدنى من حد الرفع بهامش
AUDIO_KBIT = 96                        # AAC 96kbps
LADDER = [720, 480, 360, 240, 144, None]  # نجرب من أعلى لأسفل

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
URL_RX = re.compile(r"https?://\S+")

def site_key(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u: return "yt"
    if "instagram.com" in u: return "ig"
    if "twitter.com" in u or "x.com" in u: return "tw"
    return "other"

def pick_cookiefile(sk: str) -> str | None:
    mapping = {"yt": "youtube_cookies.txt", "ig": "instagram_cookies.txt", "tw": "twitter_cookies.txt"}
    fn = mapping.get(sk)
    if not fn: return None
    p = pathlib.Path(fn)
    return str(p) if p.exists() else None

def select_format(sk: str, h: int | None) -> str:
    # سلاسل مرنة تفضّل mp4 وتهبط تلقائياً إذا صيغة غير متاحة
    if h is None:
        return "best[ext=mp4]/best"
    if sk == "yt":
        return f"bv*[height<={h}]+ba/b[height<={h}][ext=mp4]/b[height<={h}]/best"
    elif sk in ("ig", "tw"):
        return f"best[height<={h}][ext=mp4]/best[height<={h}]/best"
    else:
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
        "http_headers": {"User-Agent": "Mozilla/5.0"},
        "retries": 3,
        "concurrent_fragment_downloads": 1,
        "nopart": True,
    }
    ck = pick_cookiefile(sk)
    if ck: opts["cookiefile"] = ck  # اختيارية
    return opts

def ffmpeg_transcode_fit(src: pathlib.Path, duration_sec: float, max_h: int = 360) -> pathlib.Path:
    duration_sec = max(1.0, float(duration_sec or 1.0))
    audio_bps = AUDIO_KBIT * 1000
    target_total_bps = (TARGET_MARGIN * 8) / duration_sec
    video_bps = max(180_000, int(target_total_bps - audio_bps))
    out = src.with_suffix(".tgfit.mp4")
    cmd = [
        "ffmpeg","-y","-i",str(src),
        "-vf",f"scale='if(gt(iw,ih),-2,{max_h})':'if(gt(iw,ih),{max_h},-2)',setsar=1:1",
        "-c:v","libx264","-preset","veryfast",
        "-b:v",str(video_bps),"-maxrate",str(int(video_bps*1.2)),"-bufsize",str(int(video_bps*2)),
        "-movflags","+faststart","-c:a","aac","-b:a",f"{AUDIO_KBIT}k","-ac","2", str(out)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return out

async def download_with_ladder(url: str, tmpdir: str, sk: str):
    last_path = None; last_duration = None
    for h in LADDER:
        with YoutubeDL(build_opts(tmpdir, sk, h)) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = float(info.get("duration") or 0)
            fpath = pathlib.Path(ydl.prepare_filename(info))
            mp4 = fpath.with_suffix(".mp4")
            if mp4.exists(): fpath = mp4
        last_path, last_duration = fpath, duration
        if fpath.stat().st_size <= TG_LIMIT: return fpath, duration
    return last_path, last_duration

@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer("أرسل رابط فيديو (YouTube/Instagram/Twitter). أحاول جودة مناسبة للحد، وإذا لزم أضغطه.\n"
                   "لروابط IG/X المقفلة خلف تسجيل الدخول، ضع instagram_cookies.txt / twitter_cookies.txt بجانب البوت.")

@dp.message(F.text.regexp(URL_RX))
async def handle_url(m: Message):
    url = URL_RX.search(m.text).group(0)
    await m.answer("جاري المعالجة…")
    sk = site_key(url)
    with tempfile.TemporaryDirectory() as td:
        td_path = pathlib.Path(td)
        try:
            fpath, duration = await download_with_ladder(url, td, sk)
        except Exception as e:
            msg = str(e)
            if "login" in msg.lower() or "authentication" in msg.lower():
                await m.answer("هذا الرابط يتطلب تسجيل دخول للموقع. أضف ملف كوكيز صالح لهذا الموقع ثم أعد الإرسال.")
            else:
                await m.answer(f"فشل التنزيل:\n<code>{msg[:4000]}</code>")
            return
        out_path = fpath
        if out_path.stat().st_size > TG_LIMIT:
            try:
                out_path = ffmpeg_transcode_fit(out_path, duration, max_h=360)
            except Exception as e:
                await m.answer(f"لم أستطع ضغط الفيديو تلقائيًا:\n<code>{e}</code>")
                return
        try:
            if out_path.stat().st_size <= TG_LIMIT:
                await m.answer_document(FSInputFile(str(out_path)))
            else:
                await m.answer("حتى بعد الضغط الحجم أكبر من حد تيليجرام. جرّب مقطع أقصر أو جودة أقل.")
        finally:
            for p in td_path.glob("*"):
                try: p.unlink(missing_ok=True)
                except: pass

async def main():
    # Polling واحد فقط لتجنّب 409
    await dp.start_polling(bot, allowed_updates=["message"])

if __name__ == "__main__":
    asyncio.run(main())
