# Python slim مناسب لـ Render
FROM python:3.12-slim

# أدوات ffmpeg للضغط
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# انسخ الكود و(اختياري) ملفات كوكيز إن وُجدت
COPY bot.py ./
# COPY instagram_cookies.txt ./
# COPY twitter_cookies.txt ./
# COPY youtube_cookies.txt ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
