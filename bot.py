import os, asyncio, tempfile, pathlib, re
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from yt_dlp import YoutubeDL

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN: raise SystemExit("Set BOT_TOKEN env var")

bot, dp = Bot(TOKEN), Dispatcher()
URL_RX = re.compile(r"https?://\S+")

@dp.message(Command("start"))
async def hi(m: Message): await m.answer("أرسل رابط فيديو وسأحمله لك (يوتيوب/تيك توك/تويتر/إنستقرام).")

@dp.message(F.text.regexp(URL_RX))
async def grab(m: Message):
    url = URL_RX.search(m.text).group(0)
    await m.answer("جاري التنزيل…")
    with tempfile.TemporaryDirectory() as td:
        out = str(pathlib.Path(td) / "%(title).80s.%(ext)s")
        opts = {"outtmpl": out, "format": "mp4/bestvideo+bestaudio/best",
                "merge_output_format": "mp4", "noprogress": True, "quiet": True}
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                fp = ydl.prepare_filename(info)
                mp4 = pathlib.Path(fp).with_suffix(".mp4")
                if mp4.exists(): fp = str(mp4)
        except Exception as e:
            return await m.answer(f"فشل التنزيل: {e}")
        f = pathlib.Path(fp); size_mb = f.stat().st_size/1024/1024
        if size_mb > 48: await m.answer("الملف كبير على تيليجرام؛ جرّب رابط بدقة أقل.")
        else: await m.answer_document(open(f, "rb"))

async def main(): await dp.start_polling(bot)
if __name__ == "__main__": asyncio.run(main())
