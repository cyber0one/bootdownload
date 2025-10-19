# bot.py  — ready for Render (Aiogram v3)
import os, asyncio, tempfile, pathlib, re
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from yt_dlp import YoutubeDL

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise SystemExit("BOT_TOKEN env var is missing.")

# Aiogram v3: parse_mode يُمرَّر عبر DefaultBotProperties
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

URL_RX = re.compile(r"https?://\S+")

@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer("✅ أرسل رابط فيديو (يوتيوب/تيك توك/تويتر/إنستغرام…)، وسأحمله لك.")

@dp.message(F.text.regexp(URL_RX))
async def handle_url(m: Message):
    url = URL_RX.search(m.text).group(0)
    await m.answer("🔄 جاري التنزيل…")

    with tempfile.TemporaryDirectory() as td:
        out_tmpl = str(pathlib.Path(td) / "%(title).80s.%(ext)s")
        is_yt = any(x in url for x in ("youtube.com", "youtu.be"))

        ydl_opts = {
            "outtmpl": out_tmpl,
            "format": "mp4/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "quiet": True,
            "noprogress": True,
            "nocheckcertificate": True,
            # يقلل رسائل الموافقة في بعض الحالات
            "http_headers": {"Cookie": "CONSENT=YES+1"},
        }

        # نستخدم الكوكيز لليوتيوب فقط (تجاوز "لست روبوت")
        cookies_path = pathlib.Path("youtube_cookies.txt")
        if is_yt and cookies_path.exists():
            ydl_opts["cookiefile"] = str(cookies_path)

        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                fpath = ydl.prepare_filename(info)
                mp4 = pathlib.Path(fpath).with_suffix(".mp4")
                if mp4.exists():
                    fpath = str(mp4)
        except Exception as e:
            await m.answer(f"❌ فشل التنزيل:\n<code>{e}</code>")
            return

        f = pathlib.Path(fpath)
        try:
            # حد عملي ~48MB للرفع المباشر
            if f.stat().st_size > 48 * 1024 * 1024:
                await m.answer("⚠️ الملف كبير لرفعه داخل تيليجرام. جرّب جودة أقل.")
            else:
                await m.answer_document(open(f, "rb"))
        finally:
            # تنظيف الملف المؤقت
            try: f.unlink(missing_ok=True)
            except: pass

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
