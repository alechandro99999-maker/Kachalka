from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time
from datetime import date
import psycopg2

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL")

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

@app.route("/api/download", methods=["POST"])
def download():
    data = request.json
    url = data.get("url", "").strip()
    fmt = data.get("format", "mp4")
    if "youtube.com" in url or "youtu.be" in url:
            cookie_file = "youtube_cookies.txt"
        else:
            cookie_file = "cookies.txt"
    if not url:
        return jsonify({"error": "Ссылка не указана"}), 400
    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, file_id)
    if fmt == "mp3":
        ydl_opts = {
            "cookiefile": cookie_file,
            "outtmpl": output_path + ".%(ext)s",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            "quiet": True,
            "cookiefile": "cookies.txt",
        }
    "cookiefile": cookie_file,
        ydl_opts = {
            "format": "best",
            "outtmpl": output_path + ".%(ext)s",
            "cookiefile": "cookies.txt",
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

@app.route("/api/info", methods=["POST"])
def info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "Ссылка не указана"}), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({"title": info.get("title", "Видео"), "platform": info.get("extractor_key", "")})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 400

@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats_db())

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
