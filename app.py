
from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time
import random
import subprocess
from datetime import date, datetime, timedelta
import psycopg2
import requests
 
app = Flask(__name__)
CORS(app)
 
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
 
DATABASE_URL = os.environ.get("DATABASE_URL")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
 
# Telegram: бот может скачать файл не больше 20 МБ и отправить не больше 50 МБ
TG_MAX_DOWNLOAD = 20 * 1024 * 1024
TG_MAX_UPLOAD = 50 * 1024 * 1024
 
# На бесплатном Render мало CPU/RAM — обрабатываем по одному видео
UNIQ_LOCK = threading.Semaphore(1)
 
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
        for key in ["total", "today", "tiktok", "instagram", "youtube", "uniq"]:
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
 
def increment_uniq():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO stats (key, value) VALUES ('uniq', 1) ON CONFLICT (key) DO UPDATE SET value = stats.value + 1")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Uniq stats error: {e}")
 
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
 
def safe_err(e, limit=200):
    """Текст ошибки без токена бота."""
    text = str(e)
    if TELEGRAM_TOKEN:
        text = text.replace(TELEGRAM_TOKEN, "***")
    return text[:limit]
 
def tg_send(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})
 
def tg_send_video(chat_id, path, caption=""):
    with open(path, "rb") as f:
        requests.post(f"{TELEGRAM_API}/sendVideo", data={"chat_id": chat_id, "caption": caption, "supports_streaming": "true"}, files={"video": f})
 
def tg_send_audio(chat_id, path, caption=""):
    with open(path, "rb") as f:
        requests.post(f"{TELEGRAM_API}/sendAudio", data={"chat_id": chat_id, "caption": caption}, files={"audio": f})
 
# ---------------------------------------------------------------------------
# Уникализация видео (ffmpeg)
# ---------------------------------------------------------------------------
 
def has_audio(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=60,
    )
    return bool(r.stdout.strip())
 
def video_codec(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=60,
    )
    return r.stdout.strip()
 
def ensure_iphone_compatible(path):
    """Если видео не H.264 в mp4 (VP9/AV1/HEVC/webm) — iPhone не сохранит его в Фото.
    Перекодируем в H.264 + AAC. Возвращает путь к готовому файлу."""
    if path.lower().endswith(".mp4") and video_codec(path) == "h264":
        return path
    out = os.path.splitext(path)[0] + "_h264.mp4"
    cmd = ["ffmpeg", "-y", "-i", path, "-map_metadata", "-1",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-threads", "1",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(out):
        raise RuntimeError("ffmpeg: " + (r.stderr or "")[-300:])
    os.remove(path)
    return out
 
def uniqualize_video(src, dst):
    """
    Лёгкая уникализация: небольшой кроп/зум со случайным смещением,
    цвет, шум, скорость ±3%, звук (темп/громкость), чистка метаданных,
    случайные параметры кодирования. Параметры случайные при каждом запуске.
    """
    rnd = random.Random()
 
    k = rnd.uniform(0.96, 0.985)          # зум 1.5–4%
    rx, ry = rnd.random(), rnd.random()   # положение кропа
    bright = rnd.uniform(-0.03, 0.03)
    contrast = rnd.uniform(0.96, 1.05)
    saturation = rnd.uniform(0.94, 1.08)
    noise = rnd.randint(2, 5)
    speed = rnd.uniform(0.97, 1.03)
    volume = rnd.uniform(0.92, 1.08)
 
    vf = (
        f"crop=trunc(iw*{k:.4f}/2)*2:trunc(ih*{k:.4f}/2)*2:(iw-ow)*{rx:.3f}:(ih-oh)*{ry:.3f},"
        f"eq=brightness={bright:.3f}:contrast={contrast:.3f}:saturation={saturation:.3f},"
        f"noise=alls={noise}:allf=t,"
        f"setpts=PTS/{speed:.4f}"
    )
    af = f"atempo={speed:.4f},volume={volume:.3f},aresample=44100"
 
    created = datetime.utcnow() - timedelta(days=rnd.randint(0, 60), seconds=rnd.randint(0, 86399))
 
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-map_metadata", "-1",
        "-map", "0:v:0",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", str(rnd.randint(22, 26)),
        "-pix_fmt", "yuv420p", "-threads", "1",
    ]
    if has_audio(src):
        cmd += ["-map", "0:a:0", "-af", af, "-c:a", "aac", "-b:a", f"{rnd.choice([112, 128, 160])}k"]
    else:
        cmd += ["-an"]
    cmd += [
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-metadata", f"creation_time={created.strftime('%Y-%m-%dT%H:%M:%S.000000Z')}",
        "-movflags", "+faststart",
        dst,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or not os.path.exists(dst):
        raise RuntimeError("ffmpeg: " + (r.stderr or "")[-300:])
 
def tg_download_file(file_id, dest):
    r = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=30).json()
    if not r.get("ok"):
        raise RuntimeError(r.get("description", "getFile error"))
    file_path = r["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(256 * 1024):
                f.write(chunk)
 
def send_uniq_result(chat_id, src_path, caption):
    out_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}_uniq.mp4")
    try:
        uniqualize_video(src_path, out_path)
        if os.path.getsize(out_path) > TG_MAX_UPLOAD:
            tg_send(chat_id, "❌ Готовое видео больше 50 МБ — Telegram не даёт отправить. Пришли ролик покороче.")
            return False
        tg_send_video(chat_id, out_path, caption=caption)
        increment_uniq()
        return True
    finally:
        for p in (out_path,):
            if os.path.exists(p):
                os.remove(p)
 
def process_uniq_file(chat_id, file_id):
    tg_send(chat_id, "⏳ Уникализирую видео, это может занять 1–3 минуты...")
    src = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}_src")
    try:
        with UNIQ_LOCK:
            tg_download_file(file_id, src)
            send_uniq_result(chat_id, src, "✅ Готово. Видео уникализировано")
    except Exception as e:
        tg_send(chat_id, f"❌ Ошибка: {safe_err(e)}")
    finally:
        if os.path.exists(src):
            os.remove(src)
 
def process_uniq_link(chat_id, url):
    tg_send(chat_id, "⏳ Скачиваю и уникализирую видео, это может занять 1–3 минуты...")
    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, file_id)
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "cookiefile": get_cookie_file(url),
        "outtmpl": output_path + ".%(ext)s",
        "quiet": True,
        "merge_output_format": "mp4",
    }
    src = None
    try:
        with UNIQ_LOCK:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "video")
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(file_id):
                    src = os.path.join(DOWNLOAD_DIR, f)
                    break
            if not src:
                tg_send(chat_id, "❌ Не удалось найти файл после скачивания")
                return
            send_uniq_result(chat_id, src, f"✅ {title[:100]}")
    except Exception as e:
        tg_send(chat_id, f"❌ Ошибка: {safe_err(e)}")
    finally:
        if src and os.path.exists(src):
            os.remove(src)
 
# ---------------------------------------------------------------------------
# Обычное скачивание (как раньше)
# ---------------------------------------------------------------------------
 
def process_tg_download(chat_id, url):
    tg_send(chat_id, "⏳ Скачиваю видео, подожди...")
 
    file_id = str(uuid.uuid4())
    output_path = os.path.join(DOWNLOAD_DIR, file_id)
    cookie_file = get_cookie_file(url)
 
    ydl_opts = {
        "format": "best[ext=mp4][vcodec^=avc1]/best[ext=mp4]/best",
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
                    with UNIQ_LOCK:
                        final_path = ensure_iphone_compatible(final_path)
                    tg_send_video(chat_id, final_path, caption=f"📹 {title[:100]}")
                cleanup_file(final_path)
                increment_stats(url)
                return
 
        tg_send(chat_id, "❌ Не удалось найти файл после скачивания")
 
    except Exception as e:
        tg_send(chat_id, f"❌ Ошибка: {safe_err(e)}")
 
HELP_TEXT = (
    "👋 Привет! Я Качалка.\n\n"
    "🔗 Отправь ссылку из TikTok, Instagram или YouTube — я предложу: скачать или уникализировать.\n\n"
    "🎬 Или пришли видеофайл (до 20 МБ) — я его уникализирую.\n\n"
    "Уникализация: лёгкий зум/кроп, цвет, шум, скорость ±3%, звук, новые метаданные."
)
 
# Ссылки, ожидающие выбора действия: {id: (url, время)}. Хранится в памяти.
PENDING = {}
PENDING_TTL = 3600
 
def pending_put(url):
    now = time.time()
    for k in [k for k, (_, t) in PENDING.items() if now - t > PENDING_TTL]:
        PENDING.pop(k, None)
    key = uuid.uuid4().hex[:10]
    PENDING[key] = (url, now)
    return key
 
def tg_send_menu(chat_id, url):
    key = pending_put(url)
    requests.post(f"{TELEGRAM_API}/sendMessage", json={
        "chat_id": chat_id,
        "text": "Что сделать с видео?",
        "reply_markup": {"inline_keyboard": [[
            {"text": "📥 Скачать", "callback_data": f"dl:{key}"},
            {"text": "🎬 Уникализировать", "callback_data": f"uq:{key}"},
        ]]},
    })
 
def handle_callback(cb):
    cb_id = cb.get("id")
    chat_id = cb.get("message", {}).get("chat", {}).get("id")
    data = cb.get("data", "")
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": cb_id})
    if not chat_id or ":" not in data:
        return
    action, key = data.split(":", 1)
    item = PENDING.get(key)
    if not item:
        tg_send(chat_id, "⌛ Ссылка устарела. Отправь её ещё раз.")
        return
    url = item[0]
    if action == "dl":
        threading.Thread(target=process_tg_download, args=(chat_id, url)).start()
    elif action == "uq":
        threading.Thread(target=process_uniq_link, args=(chat_id, url)).start()
 
@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.json
    if not data:
        return "ok"
 
    if data.get("callback_query"):
        handle_callback(data["callback_query"])
        return "ok"
 
    message = data.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()
 
    if not chat_id:
        return "ok"
 
    # Видео/файл от пользователя -> уникализация
    media = message.get("video") or message.get("video_note")
    doc = message.get("document")
    if not media and doc and (doc.get("mime_type") or "").startswith("video/"):
        media = doc
    if media:
        if (media.get("file_size") or 0) > TG_MAX_DOWNLOAD:
            tg_send(chat_id, "❌ Файл больше 20 МБ — бот не может его скачать у Telegram. Пришли файл поменьше или ссылку: /u ссылка")
        else:
            threading.Thread(target=process_uniq_file, args=(chat_id, media["file_id"])).start()
        return "ok"
 
    if not text:
        return "ok"
 
    if text in ("/start", "/help"):
        tg_send(chat_id, HELP_TEXT)
    elif text.split()[0].split("@")[0] in ("/u", "/uniq"):
        parts = text.split(None, 1)
        if len(parts) == 2 and parts[1].strip().startswith("http"):
            threading.Thread(target=process_uniq_link, args=(chat_id, parts[1].strip())).start()
        else:
            tg_send(chat_id, "Отправь так: /u ссылка_на_видео — или просто пришли видеофайл.")
    elif text.startswith("http"):
        tg_send_menu(chat_id, text)
    else:
        tg_send(chat_id, "Отправь ссылку на видео или видеофайл 👇")
 
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
    domain = os.environ.get("RENDER_PUBLIC_DOMAIN") or os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
    if domain and TELEGRAM_TOKEN:
        webhook_url = f"https://{domain}/webhook/{TELEGRAM_TOKEN}"
        requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": webhook_url, "allowed_updates": ["message", "callback_query"]})
        print("Webhook set", flush=True)
 
if __name__ == "__main__":
    init_db()
    threading.Thread(target=set_webhook, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
 
