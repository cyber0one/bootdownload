# bot.py — Render-ready. Auto-picks lower formats; if still > TG limit, re-encodes via ffmpeg to fit.
import os, asyncio, tempfile, pathlib, re, math, subprocess
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from yt_dlp import YoutubeDL

# ==== config ====
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN env var is missing.")
TG_LIMIT = 48 * 1024 * 1024       # ~48MB Bot API practical ceiling
TARGET_MARGIN = 46 * 1024 * 1024  # aim slightly under the ceiling
AUDIO_KBIT = 96                   # target audio bitrate if we re-encode
LADDER = [360, 240, 144, None]    # try native qualities before re-encoding

# ==== aiogram v3 wiring ====
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
URL_RX = re.compile(r"https?://\S+")

def build_opts(tmpdir: str, height: int | None, is_yt: bool):
    out_tmpl = str(pathlib.Path(tmpdir) / "%(title).80s.%(ext)s")
    fmt = f"bv*[height<={height}]+ba/b[height<={height}]" if height else "mp4/bestvideo+bestaudio/best"
    opts = {
        "outtmpl": out_tmpl,
        "format": fmt,
        "merge_output_format": "mp4",
        "quiet": True, "noprogress": True, "nocheckcertificate": True,
        "http_headers": {"Cookie": "CONSENT=YES+1"},
        "retries": 3,
    }
    if is_yt and pathlib.Path("youtube_cookies.txt").exists():
        opts["cookiefile"] = "youtube_cookies.txt"
    return opts

def ffmpeg_transcode_fit(src: pathlib.Path, duration_sec: float, max_h: int = 360) -> pathlib.Path:
    """
    Re-encode with H.264 + AAC to fit under TARGET_MARGIN using simple bitrate budgeting.
    """
    # safety
    duration_sec = max(1.0, float(duration_sec or 1.0))
    audio_bps = AUDIO_KBIT * 1000
    # desired total bitrate (bits/s) to hit TARGET_MARGIN bytes
    target_total_bps = (TARGET_MARGIN * 8) / duration_sec
    video_bps = max(200_000, int(target_total_bps - audio_bps))  # floor at 200 kbps
    # output path
    out = src.with_suffix(".tgfit.mp4")
    # ffmpeg command
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"scale='min(iw,ih)*{max_h}/max(iw,ih)':'{max_h}':force_original_aspect_ratio=decrease",
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", str(video_bps),
        "-maxrate", str(int(video_bps*1.2)), "-bufsize", str(int(video_bps*2)),
        "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", f"{AUDIO_KBIT}k", "-ac", "2",
        str(out)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return out

async def download_with_ladder(url: str, tmpdir: str, is_yt: bool):
    """
    Try a quality ladder; return (pathlib.Path file_path, duration_seconds).
    If all native qualities exceed limit, return smallest (last) and let caller transcode.
    """
    last_path = None
    last_duration = None
    for h in LADDER:
        with YoutubeDL(build_opts(tmpdir, h, is_yt)) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = float(info.get("duration") or 0)
            fpath = pathlib.Path(ydl.prepare_filename(info))
            mp4 = fpath.with_suffix(".mp4")
            if mp4.exists():
                fpath = mp4
        last_path, last_duration = fpath, duration
        if fpath.stat().st_size <= TG_LIMIT:
            return fpath, duration
    return last_path, last_duration  # still large; let caller re-encode

@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer("✅ أرسل رابط فيديو وسأحمّله لك. إن كان كبيرًا سأخفض الجودة تلقائيًا، وإن لزم سأضغطه بـ ffmpeg ليلائم تيليجرام.")

@dp.message(F.text.regexp(URL_RX))
async def handle_url(m: Message):
    url = URL_RX.search(m.text).group(0)
    await m.answer("🔄 جاري المعالجة…")

    is_yt = any(x in url for x in ("youtube.com", "youtu.be"))

    with tempfile.TemporaryDirectory() as td:
        td_path = pathlib.Path(td)
        try:
            fpath, duration = await download_with_ladder(url, td, is_yt)
        except Exception as e:
            await m.answer(f"❌ فشل التنزيل:\n<code>{e}</code>")
            return

        out_path = fpath
        if out_path.stat().st_size > TG_LIMIT:
            # last resort: re-encode to fit
            try:
                out_path = ffmpeg_transcode_fit(out_path, duration, max_h=360)
            except Exception as e:
                await m.answer(f"❌ لم أستطع ضغط الفيديو تلقائيًا:\n<code>{e}</code>")
                return

        try:
            if out_path.stat().st_size <= TG_LIMIT:
                await m.answer_document(open(out_path, "rb"))
            else:
                await m.answer("⚠️ حتى بعد الضغط، الحجم ما يزال أعلى من حد تيليجرام. جرّب مقتطف أقصر أو رابط بجودة أقل.")
        finally:
            # تنظيف مؤقت
            for p in td_path.glob("*"):
                try: p.unlink(missing_ok=True)
                except: pass

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
