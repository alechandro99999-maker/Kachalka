from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def cleanup_file(path, delay=300):
    """Удаляем файл через 5 минут после скачивания"""
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

    # Настройки в зависимости от формата
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
        }
        ext = "mp3"
    elif fmt == "no_watermark":
        # Для TikTok без водяного знака
        ydl_opts = {
            "format": "best",
            "outtmpl": output_path + ".%(ext)s",
            "quiet": True,
            "extractor_args": {"tiktok": {"webpage_download": True}},
        }
        ext = "mp4"
    else:
        # Обычный MP4
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": output_path + ".%(ext)s",
            "quiet": True,
            "merge_output_format": "mp4",
        }
        ext = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")

        # Ищем скачанный файл
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(file_id):
                final_path = os.path.join(DOWNLOAD_DIR, f)
                cleanup_file(final_path)
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
    """Получить информацию о видео без скачивания"""
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

@app.route("/")
def index():
    return "Kachalka API работает!"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
