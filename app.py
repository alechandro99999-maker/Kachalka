from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time
from datetime import date

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

stats = {
    "total": 0,
    "today": 0,
    "today_date": str(date.today()),
    "tiktok": 0,
    "instagram": 0,
    "youtube": 0,
}

def detect_platform(url):
    if "tiktok.com" in url:
        return "tiktok"
    elif "instagram.com" in url:
        return "instagram"
    elif "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    return "other"

def increment_stats(url):
    global stats
    today = str(date.today())
    if stats["today_date"] != today:
        stats["today"] = 0
        stats["today_date"] = today
    stats["total"] += 1
    stats["today"] += 1
    platform = detect_platform(url)
    if platform in stats:
        stats[platform] += 1

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

    if not url:
        return jsonify({"error": "Ссылка не указана"}), 400

    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, file_id)

    if fmt == "mp3":
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_path + ".%(ext)s",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
            "cookiefile": "cookies.txt",
        }
    elif fmt == "no_watermark":
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
            "cookiefile": "cookies.txt",
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
                return send_file(
                    final_path,
                    as_attachment=True,
                    download_name=f"{title[:60]}.{f.split('.')[-1]}"
                )

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
    ydl_opts = {"quiet": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                "title": info.get("title", "Видео"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "platform": info.get("extractor_key", ""),
                "uploader": info.get("uploader", ""),
            })
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 400

@app.route("/api/stats")
def get_stats():
    return jsonify(stats)

@app.route("/")
def index():
    return send_from_directory(".", "index.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
