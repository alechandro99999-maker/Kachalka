from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time
from datetime import date
import psycopg2
import requests

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        for key in ["total", "today", "tiktok", "instagram", "youtube"]:
            cur.execute("INSERT INTO stats (key, value) VALUES (%s, 0) ON CONFLICT DO NOTHING", (key,))
        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("INSERT INTO meta (key, value) VALUES ('today_date', %s) ON CONFLICT DO NOTHING", (str(date.today()),))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB init error: {e}")

def increment_stats(url):
    try:
        conn = get_db()
        cur = conn.cursor()
        today = str(date.today())
        cur.execute("SELECT value FROM meta WHERE key = 'today_date'")
        row = cur.fetchone()
        if row and row[0] != today:
            cur.execute("UPDATE stats SET value = 0 WHERE key = 'today'")
            cur.execute("UPDATE meta SET value = %s WHERE key = 'today_date'", (today,))
        cur.execute("UPDATE stats SET value = value + 1 WHERE key = 'total'")
        cur.execute("UPDATE stats SET value = value + 1 WHERE key = 'today'")
        if "tiktok.com" in url:
            cur.execute("UPDATE stats SET value = value + 1 WHERE key = 'tiktok'")
        elif "instagram.com" in url:
            cur.execute("UPDATE stats SET value = value + 1 WHERE key = 'instagram'")
        elif "youtube.com" in url or "youtu.be" in url:
            cur.execute("UPDATE stats SET value = value + 1 WHERE key = 'youtube'")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Stats error: {e}")

def get_stats_db():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM stats")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return {row[0]: row[1] for row in rows}
    except Exception as e:
        print(f"Get stats error: {e}")
        return {"total": 0, "today": 0, "tiktok": 0, "instagram": 0, "youtube": 0}

def cleanup_file(path, delay=300):
    def delete():
        time.sleep(delay)
        if os.path.exists(path):
            os.remove(path)
    threading.Thread(target=delete, daemon=True).start()

def get_cookie_file(url):
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube_cookies.txt"
    return "cookies.txt"

def tg_send(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})

def tg_send_video(chat_id, path, caption=""):
    with open(path, "rb") as f:
        requests.post(f"{TELEGRAM_API}/sendVideo", data={"chat_id": chat_id, "caption": caption}, files={"video": f})

def tg_send_audio(chat_id, path, caption=""):
    with open(path, "rb") as f:
        requests.post(f"{TELEGRAM_API}/sendAudio", data={"chat_id": chat_id, "caption": caption}, files={"audio": f})

def process_tg_download(chat_id, url):
    tg_send(chat_id, "⏳ Скачиваю видео, подожди...")

    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, file_id)
    cookie_file = get_cookie_file(url)

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "cookiefile": cookie_file,
        "outtmpl": output_path + ".%(ext)s",
        "quiet": True,
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")

        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(file_id):
                final_path = os.path.join(DOWNLOAD_DIR, f)
                ext = f.split(".")[-1]
                if ext == "mp3":
                    tg_send_audio(chat_id, final_path, caption=f"🎵 {title[:100]}")
                else:
                    tg_send_video(chat_id, final_path, caption=f"📹 {title[:100]}")
                cleanup_file(final_path)
                increment_stats(url)
                return

        tg_send(chat_id, "❌ Не удалось найти файл после скачивания")

    except Exception as e:
        tg_send(chat_id, f"❌ Ошибка: {str(e)[:200]}")

@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.json
    if not data:
        return "ok"

    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return "ok"

    if text == "/start":
        tg_send(chat_id, "👋 Привет! Я Качалка — бот для скачивания видео.\n\nПросто отправь мне ссылку на видео из TikTok или Instagram и я пришлю тебе файл!")
    elif text.startswith("http"):
        threading.Thread(target=process_tg_download, args=(chat_id, text)).start()
    else:
        tg_send(chat_id, "Отправь мне ссылку на видео из TikTok или Instagram 👇")

    return "ok"

@app.route("/api/download", methods=["POST"])
def download():
    data = request.json
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp4")
    if not url:
        return jsonify({"error": "Ссылка не указана"}), 400
    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, file_id)
    cookie_file = get_cookie_file(url)
    if fmt == "mp3":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_path + ".%(ext)s",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            "quiet": True,
            "cookiefile": cookie_file,
        }
    elif fmt == "no_watermark":
        ydl_opts = {
            "format": "best",
            "outtmpl": output_path + ".%(ext)s",
            "cookiefile": cookie_file,
            "quiet": True,
            "extractor_args": {"tiktok": {"webpage_download": True}},
        }
    else:
        ydl_opts = {
            "format": "best[ext=mp4]/best",
            "cookiefile": cookie_file,
            "outtmpl": output_path + ".%(ext)s",
            "quiet": True,
            "merge_output_format": "mp4",
        }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(file_id):
                final_path = os.path.join(DOWNLOAD_DIR, f)
                cleanup_file(final_path)
                increment_stats(url)
                return send_file(final_path, as_attachment=True, download_name=f"{title[:60]}.{f.split('.')[-1]}")
        return jsonify({"error": "Файл не найден после скачивания"}), 500
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Ошибка скачивания: {str(e)[:200]}"}), 400
    except Exception as e:
        return jsonify({"error": f"Что-то пошло не так: {str(e)[:200]}"}), 500

@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats_db())

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

def set_webhook():
    time.sleep(3)
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if railway_url and TELEGRAM_TOKEN:
        webhook_url = f"https://{railway_url}/webhook/{TELEGRAM_TOKEN}"
        requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": webhook_url})
        print(f"Webhook set: {webhook_url}")

if __name__ == "__main__":
    init_db()
    threading.Thread(target=set_webhook, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
