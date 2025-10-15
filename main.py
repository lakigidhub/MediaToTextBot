import os
import logging
import requests
import telebot
import json
from flask import Flask, request, abort, render_template_string, jsonify, redirect
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import threading
import time
import io
from pymongo import MongoClient
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
import subprocess
import tempfile
import glob
import speech_recognition as sr
from concurrent.futures import ThreadPoolExecutor
import re
from collections import Counter
import wave

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CHUNK_DURATION_SEC = int(os.environ.get("CHUNK_DURATION_SEC", "55"))
CHUNK_BATCH_SIZE = int(os.environ.get("CHUNK_BATCH_SIZE", "30"))
CHUNK_BATCH_PAUSE_SEC = int(os.environ.get("CHUNK_BATCH_PAUSE_SEC", "5"))
RECOGNITION_MAX_RETRIES = int(os.environ.get("RECOGNITION_MAX_RETRIES", "3"))
RECOGNITION_RETRY_WAIT = int(os.environ.get("RECOGNITION_RETRY_WAIT", "3"))
AUDIO_SAMPLE_RATE = int(os.environ.get("AUDIO_SAMPLE_RATE", "16000"))
AUDIO_CHANNELS = int(os.environ.get("AUDIO_CHANNELS", "1"))
TELEGRAM_MAX_BYTES = int(os.environ.get("TELEGRAM_MAX_BYTES", str(20 * 1024 * 1024)))
MAX_WEB_UPLOAD_MB = int(os.environ.get("MAX_WEB_UPLOAD_MB", "250"))
REQUEST_TIMEOUT_TELEGRAM = int(os.environ.get("REQUEST_TIMEOUT_TELEGRAM", "300"))
REQUEST_TIMEOUT_LLM = int(os.environ.get("REQUEST_TIMEOUT_LLM", "300"))
TRANSCRIBE_MAX_WORKERS = int(os.environ.get("TRANSCRIBE_MAX_WORKERS", "4"))
PREPEND_SILENCE_SEC = int(os.environ.get("PREPEND_SILENCE_SEC", "5"))
AMBIENT_CALIB_SEC = float(os.environ.get("AMBIENT_CALIB_SEC", "3"))
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
REQUEST_TIMEOUT_ASSEMBLY = int(os.environ.get("REQUEST_TIMEOUT_ASSEMBLY", "300"))

ASSEMBLYAI_API_KEY = os.environ.get("ASSEMBLYAI_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
WEBHOOK_BASE = os.environ.get("WEBHOOK_BASE", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
ADMIN_PANEL_SECRET = os.environ.get("ADMIN_PANEL_SECRET", SECRET_KEY)
MONGO_URI = os.environ.get("MONGO_URI", "")
DB_NAME = os.environ.get("DB_NAME", "telegram_bot_db")
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "")
raw_admins = os.environ.get("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = []
for part in [p.strip() for p in raw_admins.split(",") if p.strip()]:
    try:
        ADMIN_USER_IDS.append(int(part))
    except Exception:
        pass

raw_bot_tokens = os.environ.get("BOT_TOKENS", "")
BOT_TOKENS = [t.strip() for t in raw_bot_tokens.split(",") if t.strip()]

client = MongoClient(MONGO_URI) if MONGO_URI else MongoClient()
db = client[DB_NAME]
users_collection = db["users"]
groups_collection = db["groups"]
settings_collection = db["settings"]

app = Flask(__name__)
bots = [telebot.TeleBot(token, threaded=True, parse_mode='HTML') for token in BOT_TOKENS]
serializer = URLSafeTimedSerializer(SECRET_KEY)

LANG_OPTIONS = [("🇬🇧 English", "en"), ("🇸🇦 العربية", "ar"), ("🇪🇸 Español", "es"), ("🇫🇷 Français", "fr"), ("🇷🇺 Русский", "ru"), ("🇩🇪 Deutsch", "de"), ("🇮🇳 हिन्दी", "hi"), ("🇮🇷 فارسی", "fa"), ("🇮🇩 Indonesia", "id"), ("🇺🇦 Українська", "uk"), ("🇦🇿 Azərbaycan", "az"), ("🇮🇹 Italiano", "it"), ("🇹🇷 Türkçe", "tr"), ("🇧🇬 Български", "bg"), ("🇷🇸 Srpski", "sr"), ("🇵🇰 اردو", "ur"), ("🇹🇭 ไทย", "th"), ("🇻🇳 Tiếng Việt", "vi"), ("🇯🇵 日本語", "ja"), ("🇰🇷 한국어", "ko"), ("🇨🇳 中文", "zh"), ("🇳🇱 Nederlands", "nl"), ("🇸🇪 Svenska", "sv"), ("🇳🇴 Norsk", "no"), ("🇮🇱 עברית", "he"), ("🇩🇰 Dansk", "da"), ("🇪🇹 አማርኛ", "am"), ("🇫🇮 Suomi", "fi"), ("🇧🇩 বাংলা", "bn"), ("🇰🇪 Kiswahili", "sw"), ("🇪🇹 Oromoo", "om"), ("🇳🇵 नेपाली", "ne"), ("🇵🇱 Polski", "pl"), ("🇬🇷 Ελληνικά", "el"), ("🇨🇿 Čeština", "cs"), ("🇮🇸 Íslenska", "is"), ("🇱🇹 Lietuvių", "lt"), ("🇱🇻 Latviešu", "lv"), ("🇭🇷 Hrvatski", "hr"), ("🇷🇸 Bosanski", "bs"), ("🇭🇺 Magyar", "hu"), ("🇷🇴 Română", "ro"), ("🇸🇴 Somali", "so"), ("🇲🇾 Melayu", "ms"), ("🇺🇿 O'zbekcha", "uz"), ("🇵🇭 Tagalog", "tl"), ("🇵🇹 Português", "pt")]

CODE_TO_LABEL = {code: label for (label, code) in LANG_OPTIONS}
LABEL_TO_CODE = {label: code for (label, code) in LANG_OPTIONS}
STT_LANGUAGES = {label.split(" ", 1)[-1]: {"code": code, "emoji": label.split(" ", 1)[0], "native": label.split(" ", 1)[-1]} for label, code in LANG_OPTIONS}

user_transcriptions = {}
memory_lock = threading.Lock()
in_memory_data = {"pending_media": {}}
action_usage = {}

ALLOWED_EXTENSIONS = {"mp3", "wav", "m4a", "ogg", "webm", "flac", "mp4", "mkv", "avi", "mov", "hevc", "aac", "aiff", "amr", "wma", "opus", "m4v", "ts", "flv", "3gp"}

FFMPEG_ENV = os.environ.get("FFMPEG_BINARY", "")
POSSIBLE_FFMPEG_PATHS = [FFMPEG_ENV, "./ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]
FFMPEG_BINARY = None
for p in POSSIBLE_FFMPEG_PATHS:
    if not p: continue
    try:
        subprocess.run([p, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        FFMPEG_BINARY = p
        break
    except Exception: continue
if FFMPEG_BINARY is None: logging.warning("ffmpeg binary not found. Set FFMPEG_BINARY env var or place ffmpeg in ./ffmpeg or /usr/bin/ffmpeg")

ASSEMBLY_LANG_SET = {"en", "ar", "es", "fr", "ru", "de", "hi", "fa", "zh", "ko", "ja", "it", "uk"}

def check_subscription(user_id: int, bot_obj) -> bool:
    if not REQUIRED_CHANNEL or not REQUIRED_CHANNEL.strip():
        return True
    try:
        member = bot_obj.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except telebot.apihelper.ApiTelegramException:
        return False

def send_subscription_message(chat_id: int, bot_obj):
    if not REQUIRED_CHANNEL or not REQUIRED_CHANNEL.strip():
        return
    try:
        chat = bot_obj.get_chat(chat_id)
        if chat.type != 'private':
            return
    except Exception:
        return
    try:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton(
                "Click here to join the Group",
                url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"
            )
        )
        bot_obj.send_message(
            chat_id,
            "🔒 Access Locked You cannot use this bot until you join the Group.",
            reply_markup=markup
        )
    except Exception:
        pass

def update_user_activity(user_id: int):
    user_id_str = str(user_id)
    now = datetime.now()
    users_collection.update_one({"user_id": user_id_str}, {"$set": {"last_active": now}, "$setOnInsert": {"first_seen": now, "stt_conversion_count": 0}}, upsert=True)

def increment_processing_count(user_id: str, service_type: str):
    users_collection.update_one({"user_id": str(user_id)}, {"$inc": {f"{service_type}_conversion_count": 1}})

def get_stt_user_lang(user_id: str) -> str:
    user_data = users_collection.find_one({"user_id": user_id})
    return user_data.get("stt_language", "en") if user_data else "en"

def set_stt_user_lang(user_id: str, lang_code: str):
    users_collection.update_one({"user_id": user_id}, {"$set": {"stt_language": lang_code}}, upsert=True)

def get_user_send_mode(user_id: str) -> str:
    user_data = users_collection.find_one({"user_id": user_id})
    return user_data.get("stt_send_mode", "file") if user_data else "file"

def set_user_send_mode(user_id: str, mode: str):
    if mode not in ("file", "split"): mode = "file"
    users_collection.update_one({"user_id": user_id}, {"$set": {"stt_send_mode": mode}}, upsert=True)

def save_pending_media(user_id: str, media_type: str, data: dict):
    with memory_lock: in_memory_data["pending_media"][user_id] = {"media_type": media_type, "data": data, "saved_at": datetime.now()}

def pop_pending_media(user_id: str):
    with memory_lock: return in_memory_data["pending_media"].pop(user_id, None)

def delete_transcription_later(user_id: str, message_id: int):
    time.sleep(86400)
    with memory_lock:
        if user_id in user_transcriptions and message_id in user_transcriptions[user_id]: del user_transcriptions[user_id][message_id]

def is_transcoding_like_error(msg: str) -> bool:
    if not msg: return False
    m = msg.lower()
    checks = ["transcoding failed", "file does not appear to contain audio", "text/html", "html document", "unsupported media type", "could not decode"]
    return any(ch in m for ch in checks)

def build_lang_keyboard(callback_prefix: str, row_width: int = 3, message_id: int = None):
    markup = InlineKeyboardMarkup(row_width=row_width)
    buttons = [InlineKeyboardButton(label, callback_data=f"{callback_prefix}|{code}|{message_id}" if message_id else f"{callback_prefix}|{code}") for label, code in LANG_OPTIONS]
    for i in range(0, len(buttons), row_width): markup.add(*buttons[i:i+row_width])
    return markup

def build_result_mode_keyboard(prefix: str = "result_mode"):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("📄 .txt file", callback_data=f"{prefix}|file"), InlineKeyboardButton("💬 Split messages", callback_data=f"{prefix}|split"))
    return markup

def signed_upload_token(chat_id: int, lang_code: str, bot_index: int = 0):
    return serializer.dumps({"chat_id": chat_id, "lang": lang_code, "bot_index": int(bot_index)})

def unsign_upload_token(token: str, max_age_seconds: int = 3600):
    return serializer.loads(token, max_age=max_age_seconds)

def animate_processing_message(bot_obj, chat_id, message_id, stop_event):
    frames = ["🔄 Processing", "🔄 Processing.", "🔄 Processing..", "🔄 Processing..."]
    idx = 0
    while not stop_event():
        try: bot_obj.edit_message_text(frames[idx % len(frames)], chat_id=chat_id, message_id=message_id)
        except Exception: pass
        idx = (idx + 1) % len(frames)
        time.sleep(0.6)

def normalize_text_offline(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip() if text else text

def extract_key_points_offline(text: str, max_points: int = 6) -> str:
    if not text: return ""
    sentences = [s.strip() for s in re.split(r'(?<=[\.\!\?])\s+', text) if s.strip()]
    if not sentences: return ""
    words = [w for w in re.findall(r'\w+', text.lower()) if len(w) > 3]
    if not words: return "\n".join(f"- {s}" for s in sentences[:max_points])
    freq = Counter(words)
    sentence_scores = [(sum(freq.get(w, 0) for w in re.findall(r'\w+', s.lower())), s) for s in sentences]
    sentence_scores.sort(key=lambda x: x[0], reverse=True)
    top_sentences = sorted(sentence_scores[:max_points], key=lambda x: sentences.index(x[1]))
    return "\n".join(f"- {s}" for _, s in top_sentences)

def safe_extension_from_filename(filename: str):
    return filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""

def telegram_file_info_and_url(bot_token: str, file_id):
    url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_TELEGRAM)
    resp.raise_for_status()
    file_path = resp.json().get("result", {}).get("file_path")
    file_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    return type('Dummy', (), {'file_path': file_path})(), file_url

def convert_to_wav(input_path: str, output_wav_path: str):
    if FFMPEG_BINARY is None: raise RuntimeError("ffmpeg binary not found")
    subprocess.run([FFMPEG_BINARY, "-y", "-i", input_path, "-ar", str(AUDIO_SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), output_wav_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def get_wav_duration(wav_path: str) -> float:
    with wave.open(wav_path, 'rb') as wf: return wf.getnframes() / float(wf.getframerate())

def prepend_silence_to_wav(original_wav: str, output_wav: str, silence_sec: int):
    if FFMPEG_BINARY is None: raise RuntimeError("ffmpeg binary not found")
    tmp_dir = os.path.dirname(output_wav) or tempfile.gettempdir()
    silence_file = os.path.join(tmp_dir, f"silence_{int(time.time()*1000)}.wav")
    subprocess.run([FFMPEG_BINARY, "-y", "-f", "lavfi", "-i", f"anullsrc=channel_layout=mono:sample_rate={AUDIO_SAMPLE_RATE}", "-t", str(silence_sec), "-ar", str(AUDIO_SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), silence_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    subprocess.run([FFMPEG_BINARY, "-y", "-i", silence_file, "-i", original_wav, "-filter_complex", "[0:0][1:0]concat=n=2:v=0:a=1[out]", "-map", "[out]", output_wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    try: os.remove(silence_file)
    except Exception: pass

def split_wav_to_chunks(wav_path: str, out_dir: str, chunk_duration_sec: int):
    if FFMPEG_BINARY is None: raise RuntimeError("ffmpeg binary not found")
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "chunk%03d.wav")
    subprocess.run([FFMPEG_BINARY, "-y", "-i", wav_path, "-ar", str(AUDIO_SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), "-f", "segment", "-segment_time", str(chunk_duration_sec), "-reset_timestamps", "1", pattern], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return sorted(glob.glob(os.path.join(out_dir, "chunk*.wav")))

def create_prepended_chunk(chunk_path: str, silence_sec: int):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp.close()
    try:
        prepend_silence_to_wav(chunk_path, tmp.name, silence_sec)
        return tmp.name
    except Exception:
        try: os.remove(tmp.name)
        except Exception: pass
        raise

def recognize_chunk_file(recognizer, file_path: str, language: str):
    last_exc = None
    prepended_path = None
    for attempt in range(1, RECOGNITION_MAX_RETRIES + 1):
        try:
            prepended_path = create_prepended_chunk(file_path, PREPEND_SILENCE_SEC)
            use_path = prepended_path if prepended_path else file_path
            with sr.AudioFile(use_path) as source:
                try: recognizer.adjust_for_ambient_noise(source, duration=AMBIENT_CALIB_SEC)
                except Exception: pass
                audio = recognizer.record(source)
            text = recognizer.recognize_google(audio, language=language) if language else recognizer.recognize_google(audio)
            if prepended_path:
                try: os.remove(prepended_path)
                except Exception: pass
            return text
        except sr.UnknownValueError:
            if prepended_path:
                try: os.remove(prepended_path)
                except Exception: pass
            return ""
        except (sr.RequestError, ConnectionResetError, OSError) as e:
            last_exc = e
            if prepended_path:
                try: os.remove(prepended_path)
                except Exception: pass
            time.sleep(RECOGNITION_RETRY_WAIT * attempt)
            continue
    if last_exc is not None: raise last_exc
    return ""

def transcribe_file_with_speech_recognition(input_file_path: str, language_code: str):
    tmpdir = tempfile.mkdtemp(prefix="stt_")
    try:
        base_wav = os.path.join(tmpdir, "converted.wav")
        try: convert_to_wav(input_file_path, base_wav)
        except Exception as e: raise RuntimeError("Conversion to WAV failed: " + str(e))
        chunk_files = split_wav_to_chunks(base_wav, tmpdir, CHUNK_DURATION_SEC)
        if not chunk_files: raise RuntimeError("No audio chunks created")
        def transcribe_chunk(chunk_path): return recognize_chunk_file(sr.Recognizer(), chunk_path, language_code)
        with ThreadPoolExecutor(max_workers=TRANSCRIBE_MAX_WORKERS) as executor: results = list(executor.map(transcribe_chunk, chunk_files))
        return "\n".join(r for r in results if r)
    finally:
        try:
            for f in glob.glob(os.path.join(tmpdir, "*")):
                try: os.remove(f)
                except Exception: pass
            try: os.rmdir(tmpdir)
            except Exception: pass
        except Exception: pass

def transcribe_with_assemblyai(file_path: str, language_code: str, timeout_seconds: int = REQUEST_TIMEOUT_ASSEMBLY):
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    with open(file_path, "rb") as f:
        try:
            resp = requests.post("https://api.assemblyai.com/v2/upload", headers=headers, data=f, timeout=timeout_seconds)
            resp.raise_for_status()
            j = resp.json()
            upload_url = j.get("upload_url") or j.get("url") or j.get("data") or None
            if not upload_url:
                if isinstance(j, dict) and len(j) == 1:
                    val = next(iter(j.values()))
                    if isinstance(val, str) and val.startswith("http"): upload_url = val
            if not upload_url: raise RuntimeError("Upload failed: no upload_url returned")
        except Exception as e: raise RuntimeError("AssemblyAI upload failed: " + str(e))
    try:
        payload = {"audio_url": upload_url}
        if language_code: payload["language_code"] = language_code
        resp = requests.post("https://api.assemblyai.com/v2/transcript", headers={**headers, "content-type": "application/json"}, json=payload, timeout=timeout_seconds)
        resp.raise_for_status()
        job_id = resp.json().get("id")
        if not job_id: raise RuntimeError("AssemblyAI transcript creation failed")
        poll_url = f"https://api.assemblyai.com/v2/transcript/{job_id}"
        start = time.time()
        while True:
            r = requests.get(poll_url, headers=headers, timeout=30)
            r.raise_for_status()
            status_json = r.json()
            status = status_json.get("status")
            if status == "completed": return status_json.get("text", "")
            if status == "error": raise RuntimeError("AssemblyAI transcription error: " + str(status_json.get("error", "")))
            if time.time() - start > timeout_seconds: raise RuntimeError("AssemblyAI transcription timed out")
            time.sleep(3)
    except Exception as e: raise RuntimeError("AssemblyAI transcription failed: " + str(e))

def transcribe_via_selected_service(input_path: str, lang_code: str):
    use_assembly = lang_code in ASSEMBLY_LANG_SET
    if use_assembly:
        try:
            text = transcribe_with_assemblyai(input_path, lang_code)
            if text is None: raise RuntimeError("AssemblyAI returned no text")
            return text, "assemblyai"
        except Exception as e:
            logging.exception("AssemblyAI failed, falling back to speech_recognition")
            try: return transcribe_file_with_speech_recognition(input_path, lang_code), "speech_recognition"
            except Exception as e2: raise RuntimeError("Both AssemblyAI and speech_recognition failed: " + str(e2))
    else:
        try: return transcribe_file_with_speech_recognition(input_path, lang_code), "speech_recognition"
        except Exception as e:
            logging.exception("speech_recognition failed, attempting AssemblyAI as fallback")
            try: return transcribe_with_assemblyai(input_path, lang_code), "assemblyai"
            except Exception as e2: raise RuntimeError("Both speech_recognition and AssemblyAI failed: " + str(e2))

def split_text_into_chunks(text: str, limit: int = 4096):
    if not text: return []
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + limit, n)
        if end < n:
            last_space = text.rfind(" ", start, end)
            if last_space > start: end = last_space
        chunk = text[start:end].strip()
        if not chunk:
            end = start + limit
            chunk = text[start:end].strip()
        chunks.append(chunk)
        start = end
    return chunks

def attach_action_buttons(bot_obj, chat_id, message_id, text):
    try:
        include_summarize = len(text) > 1000 if text else False
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⭐️Clean transcript", callback_data=f"clean_up|{chat_id}|{message_id}"))
        if include_summarize:
            markup.add(InlineKeyboardButton("Get Summarize", callback_data=f"get_key_points|{chat_id}|{message_id}"))
        try:
            bot_obj.edit_message_reply_markup(chat_id, message_id, reply_markup=markup)
        except Exception:
            pass
    except Exception:
        pass
    try:
        action_usage[f"{chat_id}|{message_id}|clean_up"] = 0
        action_usage[f"{chat_id}|{message_id}|get_key_points"] = 0
    except Exception:
        pass

def handle_media_common(message, bot_obj, bot_token, bot_index=0):
    user_id_str = str(message.from_user.id)
    chat_id_str = str(message.chat.id)
    update_user_activity(message.from_user.id)
    if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
        send_subscription_message(message.chat.id, bot_obj)
        return
    file_id = None
    file_size = None
    filename = None
    if message.voice:
        file_id = message.voice.file_id
        file_size = message.voice.file_size
        filename = "voice.ogg"
    elif message.audio:
        file_id = message.audio.file_id
        file_size = message.audio.file_size
        filename = getattr(message.audio, "file_name", "audio")
    elif message.video:
        file_id = message.video.file_id
        file_size = message.video.file_size
        filename = getattr(message.video, "file_name", "video.mp4")
    elif message.document:
        mime = getattr(message.document, "mime_type", None)
        filename = getattr(message.document, "file_name", None) or "file"
        ext = safe_extension_from_filename(filename)
        if mime and ("audio" in mime or "video" in mime) or ext in ALLOWED_EXTENSIONS:
            file_id = message.document.file_id
            file_size = message.document.file_size
        else:
            bot_obj.send_message(message.chat.id, "Sorry, I can only transcribe audio or video files.")
            return

    lang = get_stt_user_lang(user_id_str)
    if file_size and file_size > TELEGRAM_MAX_BYTES:
        token = signed_upload_token(message.chat.id, lang, bot_index)
        upload_link = f"{WEBHOOK_BASE.rstrip('/')}/upload/{token}"
        max_display_mb = TELEGRAM_MAX_BYTES // (1024 * 1024)
        text = f'Telegram API doesn’t allow me to download your file if it’s larger than {max_display_mb}MB:👉🏻 <a href="{upload_link}">Click here to Upload  your file</a>'
        bot_obj.send_message(message.chat.id, text, disable_web_page_preview=True, parse_mode='HTML', reply_to_message_id=message.message_id)
        return
    processing_msg = bot_obj.send_message(message.chat.id, "🔄 Processing...", reply_to_message_id=message.message_id)
    processing_msg_id = processing_msg.message_id
    stop_animation = {"stop": False}
    animation_thread = threading.Thread(target=animate_processing_message, args=(bot_obj, message.chat.id, processing_msg_id, lambda: stop_animation["stop"]))
    animation_thread.start()
    try:
        tf, file_url = telegram_file_info_and_url(bot_token, file_id)
        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix="." + (safe_extension_from_filename(filename) or "tmp"))
        try:
            with requests.get(file_url, stream=True, timeout=REQUEST_TIMEOUT_TELEGRAM) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=256*1024):
                    if chunk: tmpf.write(chunk)
            tmpf.flush()
            tmpf.close()
            try: text, used_service = transcribe_via_selected_service(tmpf.name, lang)
            except Exception as e:
                error_msg = str(e)
                logging.exception("Error during transcription")
                if "ffmpeg" in error_msg.lower(): bot_obj.send_message(message.chat.id, "⚠️ Server error: ffmpeg not found or conversion failed. Contact admin @boyso.", reply_to_message_id=message.message_id)
                elif is_transcoding_like_error(error_msg): bot_obj.send_message(message.chat.id, "⚠️ Transcription error: file is not audible. Please send a different file.", reply_to_message_id=message.message_id)
                else: bot_obj.send_message(message.chat.id, f"Error during transcription: {error_msg}", reply_to_message_id=message.message_id)
                return
            corrected_text = normalize_text_offline(text)
            uid_key = str(message.chat.id)
            user_mode = get_user_send_mode(uid_key)
            if len(corrected_text) > 4000:
                if user_mode == "file":
                    f = io.BytesIO(corrected_text.encode("utf-8"))
                    f.name = "transcription.txt"
                    sent = bot_obj.send_document(message.chat.id, f, reply_to_message_id=message.message_id)
                    try:
                        attach_action_buttons(bot_obj, message.chat.id, sent.message_id, corrected_text)
                    except Exception: pass
                    try:
                        user_transcriptions.setdefault(uid_key, {})[sent.message_id] = corrected_text
                        threading.Thread(target=delete_transcription_later, args=(uid_key, sent.message_id), daemon=True).start()
                    except Exception: pass
                else:
                    chunks = split_text_into_chunks(corrected_text, limit=4096)
                    last_sent = None
                    for idx, chunk in enumerate(chunks):
                        if idx == 0: last_sent = bot_obj.send_message(message.chat.id, chunk, reply_to_message_id=message.message_id)
                        else: last_sent = bot_obj.send_message(message.chat.id, chunk)
                    try:
                        attach_action_buttons(bot_obj, message.chat.id, last_sent.message_id, corrected_text)
                    except Exception: pass
                    try:
                        user_transcriptions.setdefault(uid_key, {})[last_sent.message_id] = corrected_text
                        threading.Thread(target=delete_transcription_later, args=(uid_key, last_sent.message_id), daemon=True).start()
                    except Exception: pass
            else:
                sent_msg = bot_obj.send_message(message.chat.id, corrected_text or "⚠️ Warning Make sure the voice is clear or speaking in the language you Choosed.", reply_to_message_id=message.message_id)
                try:
                    attach_action_buttons(bot_obj, message.chat.id, sent_msg.message_id, corrected_text)
                except Exception: pass
                try:
                    user_transcriptions.setdefault(uid_key, {})[sent_msg.message_id] = corrected_text
                    threading.Thread(target=delete_transcription_later, args=(uid_key, sent_msg.message_id), daemon=True).start()
                except Exception: pass
            increment_processing_count(user_id_str, "stt")
        finally:
            try: os.remove(tmpf.name)
            except Exception: pass
    except Exception as e:
        error_msg = str(e)
        logging.exception("Error in transcription process")
        if is_transcoding_like_error(error_msg): bot_obj.send_message(message.chat.id, "⚠️ Transcription error: file is not audible. Please send a different file.", reply_to_message_id=message.message_id)
        else: bot_obj.send_message(message.chat.id, f"Error during transcription: {error_msg}", reply_to_message_id=message.message_id)
    finally:
        stop_animation["stop"] = True
        animation_thread.join()
        try: bot_obj.delete_message(message.chat.id, processing_msg_id)
        except Exception: pass

HTML_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Upload File</title><style>body{font-family:Arial,sans-serif;background-color:#f0f2f5;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;color:#333}.container{background-color:#fff;padding:30px;border-radius:12px;box-shadow:0 4px 15px rgba(0,0,0,0.1);text-align:center;width:90%;max-width:500px;box-sizing:border-box}h2{margin-top:0;color:#555;font-size:1.5rem}p{font-size:0.9rem;color:#666;margin-bottom:20px}.file-upload-wrapper{position:relative;overflow:hidden;display:inline-block;cursor:pointer;width:100%}.file-upload-input{position:absolute;left:0;top:0;opacity:0;cursor:pointer;font-size:100px;width:100%;height:100%}.file-upload-label{background-color:#007bff;color:#fff;padding:12px 20px;border-radius:8px;transition:background-color 0.3s;display:block;font-size:1rem}.file-upload-label:hover{background-color:#0056b3}#file-name{margin-top:15px;font-style:italic;color:#777;font-size:0.9rem;word-wrap:break-word;overflow-wrap:break-word;min-height:20px}#progress-bar-container{width:100%;background-color:#e0e0e0;border-radius:5px;margin-top:20px;display:none}#progress-bar{width:0%;height:15px;background-color:#28a745;border-radius:5px;text-align:center;color:white;line-height:15px;transition:width 0.3s ease}#status-message{margin-top:15px;font-weight:bold}.loading-spinner{display:none;width:40px;height:40px;border:4px solid #f3f3f3;border-top:4px solid #007bff;border-radius:50%;animation:spin 1s linear infinite;margin:20px auto}@keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}@media (max-width:600px){.container{padding:20px}}</style></head><body><div class="container"><h2>Upload Your Audio/Video</h2><p>Your file is too big for Telegram, but you can upload it here. Max size: {{ max_mb }}MB.</p><div class="file-upload-wrapper"><input type="file" id="file-input" class="file-upload-input" accept=".mp3,.wav,.m4a,.ogg,.webm,.flac,.mp4,.mkv,.avi,.mov,.hevc,.aac,.aiff,.amr,.wma,.opus,.m4v,.ts,.flv,.3gp"><label for="file-input" class="file-upload-label"><span id="upload-text">Choose File to Upload</span></label></div><div id="file-name"></div><div id="progress-bar-container"><div id="progress-bar">0%</div></div><div id="status-message"></div><div class="loading-spinner" id="spinner"></div></div><script>const fileInput=document.getElementById('file-input');const fileNameDiv=document.getElementById('file-name');const progressBarContainer=document.getElementById('progress-bar-container');const progressBar=document.getElementById('progress-bar');const statusMessageDiv=document.getElementById('status-message');const spinner=document.getElementById('spinner');const uploadTextSpan=document.getElementById('upload-text');const MAX_SIZE_MB={{ max_mb }};fileInput.addEventListener('change',function(){if(this.files.length>0){const file=this.files[0];fileNameDiv.textContent=`Selected: ${file.name}`;statusMessageDiv.textContent='';progressBarContainer.style.display='none';progressBar.style.width='0%';progressBar.textContent='0%';if(file.size>MAX_SIZE_MB*1024*1024){statusMessageDiv.style.color='red';statusMessageDiv.textContent=`Error: File size exceeds the maximum limit of ${MAX_SIZE_MB}MB.`;uploadTextSpan.textContent='Choose File to Upload';fileNameDiv.textContent=''}else{uploadFile(file)}}});function uploadFile(file){const formData=new FormData();formData.append('file',file);const xhr=new XMLHttpRequest();xhr.open('POST',window.location.href);xhr.upload.addEventListener('progress',function(e){if(e.lengthComputable){const percent=Math.round((e.loaded/e.total)*100);progressBarContainer.style.display='block';progressBar.style.width=percent+'%';progressBar.textContent=percent+'%';statusMessageDiv.textContent=`Uploading... ${percent}%`;if(percent===100){statusMessageDiv.textContent='Upload complete. Processing...';spinner.style.display='block'}}});xhr.onload=function(){spinner.style.display='none';if(xhr.status===200){statusMessageDiv.style.color='#28a745';statusMessageDiv.textContent='Success! Your transcript will be sent to your Telegram chat shortly.'}else{statusMessageDiv.style.color='red';statusMessageDiv.textContent=`Error: ${xhr.responseText||'An unknown error occurred.'}`}};xhr.onerror=function(){spinner.style.display='none';statusMessageDiv.style.color='red';statusMessageDiv.textContent='Network error. Please try again.'};xhr.send(formData);}</script></body></html>"""

ADMIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Admin Panel</title><style>body{font-family:Arial,sans-serif;margin:30px}.box{border:1px solid #ddd;padding:20px;border-radius:8px;max-width:900px}input[type="text"],textarea,select{width:100%;padding:8px;margin:6px 0 12px 0;box-sizing:border-box}label{font-weight:bold}.row{display:flex;gap:12px}.col{flex:1}button{padding:10px 16px;background:#007bff;color:#fff;border:none;border-radius:6px;cursor:pointer}button:disabled{opacity:0.6}</style></head><body><div class="box"><h2>Admin Panel</h2><p>Users: {{ users_count }} | Groups: {{ groups_count }}</p><hr><h3>Send Broadcast / Ads</h3><form method="post" action="/admin/send_ads" enctype="multipart/form-data"><input type="hidden" name="secret" value="{{ secret }}"><label>Send To</label><select name="target"><option value="all">All Users & Groups</option><option value="users">Users Only</option><option value="groups">Groups Only</option></select><label>Message Type</label><select name="msg_type"><option value="text">Text</option><option value="photo">Photo</option><option value="video">Video</option><option value="audio">Audio</option><option value="voice">Voice</option><option value="document">Document</option></select><label>Message Text (for text or caption)</label><textarea name="message_text" rows="6"></textarea><label>Attach File (optional)</label><input type="file" name="media_file"><label>Use Bot Index (0 for first bot)</label><input type="text" name="bot_index" value="0"><div style="margin-top:12px"><button type="submit">Send Ads</button></div></form><hr><form method="post" action="/admin/save_settings"><input type="hidden" name="secret" value="{{ secret }}"><label>Admin Panel Secret</label><input type="text" name="admin_secret" value="{{ admin_secret }}"><div style="margin-top:12px"><button type="submit">Save Settings</button></div></form></div></body></html>"""

def broadcast_worker(target_type, msg_type, text, file_path, bot_index):
    bot_to_use = bots[0] if bot_index<0 or bot_index>=len(bots) else bots[bot_index]
    recipients = []
    try:
        if target_type in ("all","users"):
            for u in users_collection.find({}, {"_id":1}):
                try: recipients.append(int(u["_id"]))
                except Exception:
                    try: recipients.append(int(str(u["_id"])))
                    except Exception: continue
        if target_type in ("all","groups"):
            for g in groups_collection.find({}, {"_id":1}):
                try: recipients.append(int(g["_id"]))
                except Exception:
                    try: recipients.append(int(str(g["_id"])))
                    except Exception: continue
    except Exception: logging.exception("Failed to gather recipients for broadcast")
    sent_count = 0
    last_exception = None
    for r in recipients:
        try:
            if msg_type == "text": bot_to_use.send_message(r, text, parse_mode='HTML')
            elif msg_type == "photo":
                if file_path:
                    with open(file_path, "rb") as f: bot_to_use.send_photo(r, f, caption=text or None)
                else: bot_to_use.send_message(r, text or "Photo not provided.")
            elif msg_type == "video":
                if file_path:
                    with open(file_path, "rb") as f: bot_to_use.send_video(r, f, caption=text or None)
                else: bot_to_use.send_message(r, text or "Video not provided.")
            elif msg_type == "audio":
                if file_path:
                    with open(file_path, "rb") as f: bot_to_use.send_audio(r, f, caption=text or None)
                else: bot_to_use.send_message(r, text or "Audio not provided.")
            elif msg_type == "voice":
                if file_path:
                    with open(file_path, "rb") as f: bot_to_use.send_voice(r, f, caption=text or None)
                else: bot_to_use.send_message(r, text or "Voice not provided.")
            elif msg_type == "document":
                if file_path:
                    with open(file_path, "rb") as f: bot_to_use.send_document(r, f, caption=text or None)
                else: bot_to_use.send_message(r, text or "Document not provided.")
            sent_count += 1
        except Exception as e:
            last_exception = e
            logging.exception("Broadcast send error")
        time.sleep(0.06)
    try:
        if file_path:
            try: os.remove(file_path)
            except Exception: pass
    except Exception: pass
    logging.info("Broadcast finished, sent=%s, last_exc=%s", sent_count, str(last_exception))

@app.route("/admin", methods=["GET"])
def admin_ui():
    secret = request.args.get("secret")
    if not secret or secret != ADMIN_PANEL_SECRET: return abort(403)
    try: users_count = users_collection.count_documents({})
    except Exception: users_count = 0
    try: groups_count = groups_collection.count_documents({})
    except Exception: groups_count = 0
    return render_template_string(ADMIN_HTML, users_count=users_count, groups_count=groups_count, secret=ADMIN_PANEL_SECRET, admin_secret=ADMIN_PANEL_SECRET)

@app.route("/admin/save_settings", methods=["POST"])
def admin_save_settings():
    global ADMIN_PANEL_SECRET
    secret = request.form.get("secret")
    if not secret or secret != ADMIN_PANEL_SECRET: return abort(403)
    new_secret = request.form.get("admin_secret")
    if new_secret:
        try:
            settings_collection.update_one({"_id":"admin_panel_secret"}, {"$set":{"value":new_secret}}, upsert=True)
            ADMIN_PANEL_SECRET = new_secret
        except Exception: pass
    return redirect(f"/admin?secret={ADMIN_PANEL_SECRET}")

@app.route("/admin/send_ads", methods=["POST"])
def admin_send_ads():
    secret = request.form.get("secret")
    if not secret or secret != ADMIN_PANEL_SECRET: return abort(403)
    target = request.form.get("target") or "all"
    msg_type = request.form.get("msg_type") or "text"
    message_text = request.form.get("message_text") or ""
    bot_index_str = request.form.get("bot_index") or "0"
    try: bot_index = int(bot_index_str)
    except Exception: bot_index = 0
    file = request.files.get("media_file")
    tmp_path = None
    if file:
        filename = file.filename or f"upload_{int(time.time()*1000)}"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix="."+safe_extension_from_filename(filename) if "." in filename else ".dat")
        try:
            tmp.write(file.read())
            tmp.flush(); tmp.close()
            tmp_path = tmp.name
        except Exception:
            try: tmp.close(); os.remove(tmp.name)
            except Exception: pass
            tmp_path = None
    t = threading.Thread(target=broadcast_worker, args=(target, msg_type, message_text, tmp_path, bot_index), daemon=True)
    t.start()
    return render_template_string("<html><body><h3>Broadcast started</h3><p>The broadcast is being processed. Check logs for progress.</p><p><a href='/admin?secret={{secret}}'>Back to Admin</a></p></body></html>", secret=ADMIN_PANEL_SECRET)

@app.route("/upload/<token>", methods=['GET', 'POST'])
def upload_large_file(token):
    try: data = unsign_upload_token(token, max_age_seconds=3600)
    except SignatureExpired: return "<h3>Link expired</h3>", 400
    except BadSignature: return "<h3>Invalid link</h3>", 400
    chat_id = data.get("chat_id")
    lang = data.get("lang", "en")
    bot_index = int(data.get("bot_index", 0))
    if bot_index < 0 or bot_index >= len(bots): bot_index = 0
    if request.method == 'GET': return render_template_string(HTML_TEMPLATE, lang_options=LANG_OPTIONS, selected_lang=lang, max_mb=MAX_WEB_UPLOAD_MB)
    file = request.files.get('file')
    if not file: return "No file uploaded", 400
    file_bytes = file.read()
    if len(file_bytes) > MAX_WEB_UPLOAD_MB * 1024 * 1024: return f"File too large. Max allowed is {MAX_WEB_UPLOAD_MB}MB.", 400
    def bytes_to_tempfile(b):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".upload")
        tmp.write(b)
        tmp.flush()
        tmp.close()
        return tmp.name
    def process_uploaded_file(chat_id_inner, lang_inner, path, bot_index_inner):
        try:
            bot_to_use = bots[bot_index_inner] if 0 <= bot_index_inner < len(bots) else bots[0]
            try: text, used = transcribe_via_selected_service(path, lang_inner)
            except Exception:
                try: bot_to_use.send_message(chat_id_inner, "Error occurred while transcribing the uploaded file.")
                except Exception: pass
                return
            corrected_text = normalize_text_offline(text)
            sent_msg = None
            try:
                uid_key = str(chat_id_inner)
                user_mode = get_user_send_mode(uid_key)
                if len(corrected_text) > 4000:
                    if user_mode == "file":
                        fobj = io.BytesIO(corrected_text.encode("utf-8"))
                        fobj.name = "transcription.txt"
                        sent_msg = bot_to_use.send_document(chat_id_inner, fobj, reply_markup=InlineKeyboardMarkup())
                    else:
                        chunks = split_text_into_chunks(corrected_text, limit=4096)
                        last_sent = None
                        for idx, chunk in enumerate(chunks):
                            if idx == 0: last_sent = bot_to_use.send_message(chat_id_inner, chunk)
                            else: last_sent = bot_to_use.send_message(chat_id_inner, chunk)
                        sent_msg = last_sent
                else: sent_msg = bot_to_use.send_message(chat_id_inner, corrected_text or "No transcription text was returned.", reply_markup=InlineKeyboardMarkup())
                try:
                    attach_action_buttons(bot_to_use, chat_id_inner, sent_msg.message_id, corrected_text)
                except Exception: pass
            except Exception:
                try: bot_to_use.send_message(chat_id_inner, "Error sending transcription message. The transcription completed but could not be delivered as a message.")
                except Exception: pass
                return
            try:
                uid_key = str(chat_id_inner)
                user_transcriptions.setdefault(uid_key, {})[sent_msg.message_id] = corrected_text
                threading.Thread(target=delete_transcription_later, args=(uid_key, sent_msg.message_id), daemon=True).start()
                increment_processing_count(str(chat_id_inner), "stt")
            except Exception: pass
        finally:
            try: os.remove(path)
            except Exception: pass
    tmp_path = bytes_to_tempfile(file_bytes)
    threading.Thread(target=process_uploaded_file, args=(chat_id, lang, tmp_path, bot_index), daemon=True).start()
    return jsonify({"status": "accepted", "message": "Upload accepted. Processing started. Your transcription will be sent to your Telegram chat when ready."})

def ask_gemini(text: str, instruction: str, timeout=REQUEST_TIMEOUT_GEMINI) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": instruction}, {"text": text}]}]}
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    result = resp.json()
    if "candidates" in result and isinstance(result["candidates"], list) and len(result["candidates"]) > 0:
        try: return result['candidates'][0]['content']['parts'][0]['text']
        except Exception: return json.dumps(result['candidates'][0])
    return json.dumps(result)

@app.route("/assemblyai", methods=["POST"])
def assemblyai_endpoint():
    lang = request.form.get("language", "en")
    f = request.files.get("file")
    if not f: return jsonify({"error": "no file provided"}), 400
    b = f.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".upload")
    try:
        tmp.write(b)
        tmp.flush()
        tmp.close()
        try: return jsonify({"text": transcribe_with_assemblyai(tmp.name, lang)}), 200
        except Exception as e:
            try: return jsonify({"text": transcribe_file_with_speech_recognition(tmp.name, lang), "fallback": "speech_recognition"}), 200
            except Exception as e2: return jsonify({"error": str(e2)}), 500
    finally:
        try: os.remove(tmp.name)
        except Exception: pass

def register_handlers(bot_obj, bot_token, bot_index):
    @bot_obj.message_handler(commands=['start'])
    def start_handler(message):
        try:
            update_user_activity(message.from_user.id)
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            bot_obj.send_message(message.chat.id, "Choose your file language for transcription using the below buttons:", reply_markup=build_lang_keyboard("start_select_lang"))
        except Exception: logging.exception("Error in start_handler")

    @bot_obj.callback_query_handler(func=lambda c: c.data and c.data.startswith("start_select_lang|"))
    def start_select_lang_callback(call):
        try:
            uid = str(call.from_user.id)
            _, lang_code = call.data.split("|", 1)
            lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
            set_stt_user_lang(uid, lang_code)
            try: bot_obj.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass
            welcome_text = "👋 Salaam!    \n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free"
            bot_obj.send_message(call.message.chat.id, welcome_text)
            bot_obj.answer_callback_query(call.id, f"✅ Language set to {lang_label}")
        except Exception:
            logging.exception("Error in start_select_lang_callback")
            try: bot_obj.answer_callback_query(call.id, "❌ Error setting language, try again.", show_alert=True)
            except Exception: pass

    @bot_obj.message_handler(commands=['help'])
    def handle_help(message):
        try:
            update_user_activity(message.from_user.id)
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            text = "Commands supported:\n/start - Show welcome message\n/lang  - Change language\n/mode  - Change result delivery mode\n/help  - This help message\n\nSend a voice/audio/video (up to 20MB for Telegram) and I will transcribe it.\nIf it's larger than Telegram limits, you'll be provided a secure web upload link (supports up to 250MB) Need more help? Contact: @boyso20"
            bot_obj.send_message(message.chat.id, text)
        except Exception: logging.exception("Error in handle_help")

    @bot_obj.message_handler(commands=['lang'])
    def handle_lang(message):
        try:
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            kb = build_lang_keyboard("stt_lang")
            bot_obj.send_message(message.chat.id, "Choose your file language for transcription using the below buttons:", reply_markup=kb)
        except Exception: logging.exception("Error in handle_lang")

    @bot_obj.message_handler(commands=['mode'])
    def handle_mode(message):
        try:
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            current_mode = get_user_send_mode(str(message.from_user.id))
            mode_text = "📄 .txt file" if current_mode == "file" else "💬 Split messages"
            bot_obj.send_message(message.chat.id, f"Result delivery mode: {mode_text}. Change it below:", reply_markup=build_result_mode_keyboard())
        except Exception: logging.exception("Error in handle_mode")

    @bot_obj.message_handler(commands=['admin'])
    def handle_admin(message):
        try:
            if message.from_user.id not in ADMIN_USER_IDS:
                bot_obj.send_message(message.chat.id, "Access Denied: Only admins can use this command.")
                return
            total_users = users_collection.count_documents({})
            total_groups = groups_collection.count_documents({})
            total_stt_conversions = sum(user.get("stt_conversion_count", 0) for user in users_collection.find({}))
            seven_days_ago = datetime.now() - timedelta(days=7)
            active_users_7d = users_collection.count_documents({"last_active": {"$gte": seven_days_ago}})
            report_text = f"<b>📊 Bot Admin Panel</b>\n\n👤 Total Users: <b>{total_users}</b>\n👥 Total Groups: <b>{total_groups}</b>\n🗣️ Total Transcriptions: <b>{total_stt_conversions:,}</b>\n🟢 Active Users (Last 7 Days): <b>{active_users_7d}</b>\n\n"
            link = f"{WEBHOOK_BASE.rstrip('/')}/admin?secret={ADMIN_PANEL_SECRET}"
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("Open Admin Panel", url=link))
            bot_obj.send_message(message.chat.id, report_text, parse_mode='HTML', reply_markup=markup)
        except Exception:
            logging.exception("Error in handle_admin")
            bot_obj.send_message(message.chat.id, "An error occurred while fetching admin stats.")

    @bot_obj.callback_query_handler(lambda c: c.data and c.data.startswith("stt_lang|"))
    def on_stt_language_select(call):
        try:
            uid = str(call.from_user.id)
            _, lang_code = call.data.split("|", 1)
            lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
            set_stt_user_lang(uid, lang_code)
            bot_obj.answer_callback_query(call.id, f"✅ Language set: {lang_label}")
            try: bot_obj.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass
        except Exception:
            logging.exception("Error in on_stt_language_select")
            try: bot_obj.answer_callback_query(call.id, "❌ Error setting language, try again.", show_alert=True)
            except Exception: pass

    @bot_obj.callback_query_handler(lambda c: c.data and c.data.startswith("result_mode|"))
    def on_result_mode_select(call):
        try:
            uid = str(call.from_user.id)
            _, mode = call.data.split("|", 1)
            set_user_send_mode(uid, mode)
            mode_text = "📄 .txt file" if mode == "file" else "💬 Split messages"
            try: bot_obj.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass
            bot_obj.answer_callback_query(call.id, f"✅ Result mode set: {mode_text}")
        except Exception:
            logging.exception("Error in on_result_mode_select")
            try: bot_obj.answer_callback_query(call.id, "❌ Error setting result mode, try again.", show_alert=True)
            except Exception: pass

    @bot_obj.message_handler(content_types=['new_chat_members'])
    def handle_new_chat_members(message):
        try:
            if message.new_chat_members[0].id == bot_obj.get_me().id:
                group_data = {'_id': str(message.chat.id), 'title': message.chat.title, 'type': message.chat.type, 'added_date': datetime.now()}
                groups_collection.update_one({'_id': group_data['_id']}, {'$set': group_data}, upsert=True)
                bot_obj.send_message(message.chat.id, "Thanks for adding me! I'm ready to transcribe your media files.")
        except Exception: logging.exception("Error in handle_new_chat_members")

    @bot_obj.message_handler(content_types=['left_chat_member'])
    def handle_left_chat_member(message):
        try:
            if message.left_chat_member.id == bot_obj.get_me().id: groups_collection.delete_one({'_id': str(message.chat.id)})
        except Exception: logging.exception("Error in handle_left_chat_member")

    @bot_obj.message_handler(content_types=['voice', 'audio', 'video', 'document'])
    def handle_media_types(message):
        try:
            handle_media_common(message, bot_obj, bot_token, bot_index)
        except Exception: logging.exception("Error in handle_media_types")

    @bot_obj.message_handler(content_types=['text'])
    def handle_text_messages(message):
        try:
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            bot_obj.send_message(message.chat.id, "For Text to Audio Use: @TextToSpeechBBot")
        except Exception: logging.exception("Error in handle_text_messages")

    @bot_obj.callback_query_handler(lambda c: c.data and c.data.startswith("get_key_points|"))
    def get_key_points_callback(call):
        try:
            parts = call.data.split("|")
            if len(parts) == 3: _, chat_id_part, msg_id_part = parts
            elif len(parts) == 2: _, msg_id_part = parts; chat_id_part = str(call.message.chat.id)
            else:
                bot_obj.answer_callback_query(call.id, "Invalid request", show_alert=True)
                return
            try: chat_id_val = int(chat_id_part); msg_id = int(msg_id_part)
            except Exception:
                bot_obj.answer_callback_query(call.id, "Invalid message id", show_alert=True)
                return
            usage_key = f"{chat_id_val}|{msg_id}|get_key_points"
            usage = action_usage.get(usage_key, 0)
            if usage >= 1:
                bot_obj.answer_callback_query(call.id, "Get Summarize unavailable (maybe expired)", show_alert=True)
                return
            action_usage[usage_key] = usage + 1
            uid_key = str(chat_id_val)
            stored = user_transcriptions.get(uid_key, {}).get(msg_id)
            if not stored:
                bot_obj.answer_callback_query(call.id, "Get Summarize unavailable (maybe expired)", show_alert=True)
                return
            bot_obj.answer_callback_query(call.id, "Generating...")
            status_msg = bot_obj.send_message(call.message.chat.id, "🔄 Processing...", reply_to_message_id=call.message.message_id)
            stop_animation = {"stop": False}
            animation_thread = threading.Thread(target=animate_processing_message, args=(bot_obj, call.message.chat.id, status_msg.message_id, lambda: stop_animation["stop"]))
            animation_thread.start()
            try:
                lang = get_stt_user_lang(str(chat_id_val)) or "en"
                instruction = f"What is this report about? What are the most important points? Please summarize them for me into (lang={lang}) without adding any introductions, notes, or extra phrases."
                try: summary = ask_gemini(stored, instruction)
                except Exception: summary = extract_key_points_offline(stored, max_points=6)
            except Exception: summary = ""
            stop_animation["stop"] = True
            animation_thread.join()
            if not summary:
                try: bot_obj.edit_message_text("No Summary returned.", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                except Exception: pass
            else:
                try: bot_obj.edit_message_text(f"{summary}", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                except Exception: pass
        except Exception: logging.exception("Error in get_key_points_callback")

    @bot_obj.callback_query_handler(lambda c: c.data and c.data.startswith("clean_up|"))
    def clean_up_callback(call):
        try:
            parts = call.data.split("|")
            if len(parts) == 3: _, chat_id_part, msg_id_part = parts
            elif len(parts) == 2: _, msg_id_part = parts; chat_id_part = str(call.message.chat.id)
            else:
                bot_obj.answer_callback_query(call.id, "Invalid request", show_alert=True)
                return
            try: chat_id_val = int(chat_id_part); msg_id = int(msg_id_part)
            except Exception:
                bot_obj.answer_callback_query(call.id, "Invalid message id", show_alert=True)
                return
            usage_key = f"{chat_id_val}|{msg_id}|clean_up"
            usage = action_usage.get(usage_key, 0)
            if usage >= 1:
                bot_obj.answer_callback_query(call.id, "Clean up unavailable (maybe expired)", show_alert=True)
                return
            action_usage[usage_key] = usage + 1
            uid_key = str(chat_id_val)
            stored = user_transcriptions.get(uid_key, {}).get(msg_id)
            if not stored:
                bot_obj.answer_callback_query(call.id, "Clean up unavailable (maybe expired)", show_alert=True)
                return
            bot_obj.answer_callback_query(call.id, "Cleaning up...")
            status_msg = bot_obj.send_message(call.message.chat.id, "🔄 Processing...", reply_to_message_id=call.message.message_id)
            stop_animation = {"stop": False}
            animation_thread = threading.Thread(target=animate_processing_message, args=(bot_obj, call.message.chat.id, status_msg.message_id, lambda: stop_animation["stop"]))
            animation_thread.start()
            try:
                lang = get_stt_user_lang(str(chat_id_val)) or "en"
                instruction = f"Clean and normalize this transcription (lang={lang}). Remove ASR artifacts like [inaudible], repeated words, filler noises, timestamps, and incorrect punctuation. Produce a clean, well-punctuated, readable text in the same language. Do not add introductions or explanations."
                try: cleaned = ask_gemini(stored, instruction)
                except Exception: cleaned = normalize_text_offline(stored)
            except Exception: cleaned = ""
            stop_animation["stop"] = True
            animation_thread.join()
            if not cleaned:
                try: bot_obj.edit_message_text("No cleaned text returned.", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                except Exception: pass
                return
            uid_key = str(chat_id_val)
            user_mode = get_user_send_mode(uid_key)
            if len(cleaned) > 4000:
                if user_mode == "file":
                    f = io.BytesIO(cleaned.encode("utf-8"))
                    f.name = "transcription_cleaned.txt"
                    try: bot_obj.delete_message(call.message.chat.id, status_msg.message_id)
                    except Exception: pass
                    sent = bot_obj.send_document(call.message.chat.id, f, reply_to_message_id=call.message.message_id)
                    try:
                        user_transcriptions.setdefault(uid_key, {})[sent.message_id] = cleaned
                        threading.Thread(target=delete_transcription_later, args=(uid_key, sent.message_id), daemon=True).start()
                    except Exception: pass
                    try:
                        action_usage[f"{call.message.chat.id}|{sent.message_id}|clean_up"] = 0
                        action_usage[f"{call.message.chat.id}|{sent.message_id}|get_key_points"] = 0
                    except Exception: pass
                else:
                    try: bot_obj.delete_message(call.message.chat.id, status_msg.message_id)
                    except Exception: pass
                    chunks = split_text_into_chunks(cleaned, limit=4096)
                    last_sent = None
                    for idx, chunk in enumerate(chunks):
                        if idx == 0: last_sent = bot_obj.send_message(call.message.chat.id, chunk, reply_to_message_id=call.message.message_id)
                        else: last_sent = bot_obj.send_message(call.message.chat.id, chunk)
                    try:
                        user_transcriptions.setdefault(uid_key, {})[last_sent.message_id] = cleaned
                        threading.Thread(target=delete_transcription_later, args=(uid_key, last_sent.message_id), daemon=True).start()
                    except Exception: pass
                    try:
                        action_usage[f"{call.message.chat.id}|{last_sent.message_id}|clean_up"] = 0
                        action_usage[f"{call.message.chat.id}|{last_sent.message_id}|get_key_points"] = 0
                    except Exception: pass
            else:
                try:
                    bot_obj.edit_message_text(f"{cleaned}", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                    uid_key = str(chat_id_val)
                    user_transcriptions.setdefault(uid_key, {})[status_msg.message_id] = cleaned
                    threading.Thread(target=delete_transcription_later, args=(uid_key, status_msg.message_id), daemon=True).start()
                    action_usage[f"{call.message.chat.id}|{status_msg.message_id}|clean_up"] = 0
                    action_usage[f"{call.message.chat.id}|{status_msg.message_id}|get_key_points"] = 0
                except Exception: pass
        except Exception: logging.exception("Error in clean_up_callback")

for idx, bot_obj in enumerate(bots): register_handlers(bot_obj, BOT_TOKENS[idx], idx)

@app.route("/", methods=["GET", "POST", "HEAD"])
def webhook_root():
    if request.method in ("GET", "HEAD"):
        bot_index = request.args.get("bot_index")
        try: bot_index_val = int(bot_index) if bot_index is not None else 0
        except Exception: bot_index_val = 0
        now_iso = datetime.utcnow().isoformat() + "Z"
        return jsonify({"status": "ok", "time": now_iso, "bot_index": bot_index_val}), 200
    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        if content_type and content_type.startswith("application/json"):
            raw = request.get_data().decode("utf-8")
            try: payload = json.loads(raw)
            except Exception: payload = None
            bot_index = request.args.get("bot_index")
            if not bot_index and isinstance(payload, dict): bot_index = payload.get("bot_index")
            header_idx = request.headers.get("X-Bot-Index")
            if header_idx: bot_index = header_idx
            try: bot_index_val = int(bot_index) if bot_index is not None else 0
            except Exception: bot_index_val = 0
            if bot_index_val < 0 or bot_index_val >= len(bots): return abort(404)
            try:
                update = telebot.types.Update.de_json(raw)
                bots[bot_index_val].process_new_updates([update])
            except Exception: logging.exception("Error processing incoming webhook update")
            return "", 200
    return abort(403)

@app.route("/set_webhook", methods=["GET", "POST"])
def set_webhook_route():
    results = []
    for idx, bot_obj in enumerate(bots):
        try:
            url = WEBHOOK_BASE.rstrip("/") + f"/?bot_index={idx}"
            bot_obj.delete_webhook()
            time.sleep(0.2)
            bot_obj.set_webhook(url=url)
            results.append({"index": idx, "url": url, "status": "ok"})
        except Exception as e:
            logging.error(f"Failed to set webhook for bot {idx}: {e}")
            results.append({"index": idx, "error": str(e)})
    return jsonify({"results": results}), 200

@app.route("/delete_webhook", methods=["GET", "POST"])
def delete_webhook_route():
    results = []
    for idx, bot_obj in enumerate(bots):
        try:
            bot_obj.delete_webhook()
            results.append({"index": idx, "status": "deleted"})
        except Exception as e:
            logging.error(f"Failed to delete webhook for bot {idx}: {e}")
            results.append({"index": idx, "error": str(e)})
    return jsonify({"results": results}), 200

def set_webhook_on_startup():
    for idx, bot_obj in enumerate(bots):
        try:
            bot_obj.delete_webhook()
            time.sleep(0.2)
            url = WEBHOOK_BASE.rstrip("/") + f"/?bot_index={idx}"
            bot_obj.set_webhook(url=url)
            logging.info(f"Main bot webhook set successfully to {url}")
        except Exception as e: logging.error(f"Failed to set main bot webhook on startup: {e}")

def set_bot_info_and_startup():
    set_webhook_on_startup()

if __name__ == "__main__":
    try:
        set_bot_info_and_startup()
        try:
            client.admin.command('ping')
            logging.info("Successfully connected to MongoDB!")
        except Exception as e: logging.error("Could not connect to MongoDB: %s", e)
    except Exception: logging.exception("Failed during startup")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
