# modified_bot_using_multiple_assemblyai_and_gemini.py
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
import re
from collections import Counter
import wave

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# -------------------------
# Config (env)
# -------------------------
CHUNK_DURATION_SEC = int(os.environ.get("CHUNK_DURATION_SEC", "55"))
CHUNK_BATCH_SIZE = int(os.environ.get("CHUNK_BATCH_SIZE", "30"))
CHUNK_BATCH_PAUSE_SEC = int(os.environ.get("CHUNK_BATCH_PAUSE_SEC", "5"))
AUDIO_SAMPLE_RATE = int(os.environ.get("AUDIO_SAMPLE_RATE", "16000"))
AUDIO_CHANNELS = int(os.environ.get("AUDIO_CHANNELS", "1"))
TELEGRAM_MAX_BYTES = int(os.environ.get("TELEGRAM_MAX_BYTES", str(20 * 1024 * 1024)))
MAX_WEB_UPLOAD_MB = int(os.environ.get("MAX_WEB_UPLOAD_MB", "250"))
REQUEST_TIMEOUT_TELEGRAM = int(os.environ.get("REQUEST_TIMEOUT_TELEGRAM", "300"))
REQUEST_TIMEOUT_GEMINI = int(os.environ.get("REQUEST_TIMEOUT_GEMINI", "300"))
REQUEST_TIMEOUT_ASSEMBLY = int(os.environ.get("REQUEST_TIMEOUT_ASSEMBLY", "300"))

# MULTI-KEY support (comma separated env or single key)
ASSEMBLYAI_API_KEYS = [t.strip() for t in os.environ.get("ASSEMBLYAI_API_KEYS", os.environ.get("ASSEMBLYAI_API_KEY", "")).split(",") if t.strip()]
GEMINI_API_KEYS = [t.strip() for t in os.environ.get("GEMINI_API_KEYS", os.environ.get("GEMINI_API_KEY", "")).split(",") if t.strip()]

ASSEMBLYAI_BASE_URL = "https://api.assemblyai.com/v2"
# use gemini-2.5-flash like earlier example
GEMINI_MODEL_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

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

# Warn if keys are not provided
if not ASSEMBLYAI_API_KEYS:
    logging.error("No AssemblyAI API keys found in ASSEMBLYAI_API_KEYS / ASSEMBLYAI_API_KEY. Bot will not transcribe without them.")
if not GEMINI_API_KEYS:
    logging.warning("No Gemini API keys found in GEMINI_API_KEYS / GEMINI_API_KEY. Summarization/cleanup will fallback to offline methods.")

# -------------------------
# DB, App, Bots
# -------------------------
client = MongoClient(MONGO_URI) if MONGO_URI else MongoClient()
db = client[DB_NAME]
users_collection = db["users"]
groups_collection = db["groups"]
settings_collection = db["settings"]

app = Flask(__name__)
bots = [telebot.TeleBot(token, threaded=True, parse_mode='HTML') for token in BOT_TOKENS]
serializer = URLSafeTimedSerializer(SECRET_KEY)

# -------------------------
# Language options (kept)
# -------------------------
LANG_OPTIONS = [("🇬🇧 English", "en"), ("🇸🇦 العربية", "ar"), ("🇪🇸 Español", "es"), ("🇫🇷 Français", "fr"),
                ("🇷🇺 Русский", "ru"), ("🇩🇪 Deutsch", "de"), ("🇮🇳 हिन्दी", "hi"), ("🇮🇷 فارسی", "fa"),
                ("🇮🇩 Indonesia", "id"), ("🇺🇦 Українська", "uk"), ("🇦🇿 Azərbaycan", "az"), ("🇮🇹 Italiano", "it"),
                ("🇹🇷 Türkçe", "tr"), ("🇧🇬 Български", "bg"), ("🇷🇸 Srpski", "sr"), ("🇵🇰 اردو", "ur"),
                ("🇹🇭 ไทย", "th"), ("🇻🇳 Tiếng Việt", "vi"), ("🇯🇵 日本語", "ja"), ("🇰🇷 한국어", "ko"),
                ("🇨🇳 中文", "zh"), ("🇳🇱 Nederlands", "nl"), ("🇸🇪 Svenska", "sv"), ("🇳🇴 Norsk", "no"),
                ("🇮🇱 עברית", "he"), ("🇩🇰 Dansk", "da"), ("🇪🇹 አማርኛ", "am"), ("🇫🇮 Suomi", "fi"),
                ("🇧🇩 বাংলা", "bn"), ("🇰🇪 Kiswahili", "sw"), ("🇪🇹 Oromoo", "om"), ("🇳🇵 नेपाली", "ne"),
                ("🇵🇱 Polski", "pl"), ("🇬🇷 Ελληνικά", "el"), ("🇨🇿 Čeština", "cs"), ("🇮🇸 Íslenska", "is"),
                ("🇱🇹 Lietuvių", "lt"), ("🇱🇻 Latviešu", "lv"), ("🇭🇷 Hrvatski", "hr"), ("🇷🇸 Bosanski", "bs"),
                ("🇭🇺 Magyar", "hu"), ("🇷🇴 Română", "ro"), ("🇸🇴 Somali", "so"), ("🇲🇾 Melayu", "ms"),
                ("🇺🇿 O'zbekcha", "uz"), ("🇵🇭 Tagalog", "tl"), ("🇵🇹 Português", "pt")]

CODE_TO_LABEL = {code: label for (label, code) in LANG_OPTIONS}
LABEL_TO_CODE = {label: code for (label, code) in LANG_OPTIONS}
STT_LANGUAGES = {label.split(" ", 1)[-1]: {"code": code, "emoji": label.split(" ", 1)[0], "native": label.split(" ", 1)[-1]} for label, code in LANG_OPTIONS}

# In-memory state
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
    except Exception:
        continue
if FFMPEG_BINARY is None:
    logging.warning("ffmpeg binary not found. Set FFMPEG_BINARY env var or place ffmpeg in ./ffmpeg or /usr/bin/ffmpeg")

# A subset of languages AssemblyAI supports in your original config
ASSEMBLY_LANG_SET = {"en", "ar", "es", "fr", "ru", "de", "hi", "fa", "zh", "ko", "ja", "it", "uk"}

# -------------------------
# Utility functions
# -------------------------
def norm_user_id(uid):
    try: return str(int(uid))
    except: return str(uid)

def check_subscription(user_id: int, bot_obj) -> bool:
    if not REQUIRED_CHANNEL or not REQUIRED_CHANNEL.strip():
        return True
    try:
        member = bot_obj.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

def send_subscription_message(chat_id: int, bot_obj):
    if not REQUIRED_CHANNEL or not REQUIRED_CHANNEL.strip(): return
    try:
        chat = bot_obj.get_chat(chat_id)
        if chat.type != 'private': return
    except Exception:
        return
    try:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("Click here to join the Group", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"))
        bot_obj.send_message(chat_id, "🔒 Access Locked You cannot use this bot until you join the Group.", reply_markup=markup)
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
        if user_id in user_transcriptions and message_id in user_transcriptions[user_id]:
            del user_transcriptions[user_id][message_id]

def is_transcoding_like_error(msg: str) -> bool:
    if not msg: return False
    m = msg.lower()
    checks = ["transcoding failed", "file does not appear to contain audio", "text/html", "html document", "unsupported media type", "could not decode"]
    return any(ch in m for ch in checks)

def build_lang_keyboard(callback_prefix: str, row_width: int = 3, message_id: int = None):
    markup = InlineKeyboardMarkup(row_width=row_width)
    buttons = [InlineKeyboardButton(label, callback_data=f"{callback_prefix}|{code}|{message_id}" if message_id else f"{callback_prefix}|{code}") for label, code in LANG_OPTIONS]
    for i in range(0, len(buttons), row_width):
        markup.add(*buttons[i:i+row_width])
    return markup

def build_result_mode_keyboard(prefix: str = "result_mode"):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("📄 .txt file", callback_data=f"{prefix}|file"), InlineKeyboardButton("💬 Split messages", callback_data=f"{prefix}|split"))
    return markup

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

# -------------------------
# ffmpeg helpers (kept)
# -------------------------
def convert_to_wav(input_path: str, output_wav_path: str):
    if FFMPEG_BINARY is None: raise RuntimeError("ffmpeg binary not found")
    subprocess.run([FFMPEG_BINARY, "-y", "-i", input_path, "-ar", str(AUDIO_SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS), output_wav_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

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

# -------------------------
# AssemblyAI: upload + transcript with multiple keys (rotation)
# -------------------------
def transcribe_file_with_assemblyai_multi_key(file_path: str, language_code: str, timeout_seconds: int = REQUEST_TIMEOUT_ASSEMBLY):
    if not ASSEMBLYAI_API_KEYS:
        raise RuntimeError("ASSEMBLYAI_API_KEYS not set")
    last_exception = None
    # try each key in order, fallback to next on errors
    for api_key in ASSEMBLYAI_API_KEYS:
        try:
            headers = {"authorization": api_key}
            # upload
            with open(file_path, "rb") as f:
                try:
                    resp = requests.post(f"{ASSEMBLYAI_BASE_URL}/upload", headers=headers, data=f, timeout=timeout_seconds)
                    resp.raise_for_status()
                    j = resp.json()
                    upload_url = j.get("upload_url") or j.get("url") or j.get("data") or None
                    if not upload_url:
                        # sometimes Assembly returns a dict with single http string
                        if isinstance(j, dict) and len(j) == 1:
                            val = next(iter(j.values()))
                            if isinstance(val, str) and val.startswith("http"):
                                upload_url = val
                    if not upload_url:
                        raise RuntimeError("Upload failed: no upload_url returned")
                except Exception as e:
                    raise RuntimeError("AssemblyAI upload failed: " + str(e))
            # submit transcript job
            try:
                payload = {"audio_url": upload_url}
                if language_code:
                    payload["language_code"] = language_code
                resp2 = requests.post(f"{ASSEMBLYAI_BASE_URL}/transcript", headers={**headers, "content-type": "application/json"}, json=payload, timeout=timeout_seconds)
                resp2.raise_for_status()
                job_id = resp2.json().get("id")
                if not job_id:
                    raise RuntimeError("AssemblyAI transcript creation failed")
                poll_url = f"{ASSEMBLYAI_BASE_URL}/transcript/{job_id}"
                start = time.time()
                while True:
                    r = requests.get(poll_url, headers=headers, timeout=30)
                    r.raise_for_status()
                    status_json = r.json()
                    status = status_json.get("status")
                    if status == "completed":
                        return status_json.get("text", "")
                    if status == "error":
                        raise RuntimeError("AssemblyAI transcription error: " + str(status_json.get("error", "")))
                    if time.time() - start > timeout_seconds:
                        raise RuntimeError("AssemblyAI transcription timed out")
                    time.sleep(3)
            except Exception as e:
                raise RuntimeError("AssemblyAI transcription failed: " + str(e))
        except Exception as e:
            logging.warning(f"AssemblyAI key failed: {str(e)}. Trying next key if available.")
            last_exception = e
            continue
    raise RuntimeError(f"All AssemblyAI keys failed. Last error: {str(last_exception) if last_exception else 'No keys were available.'}")

# -------------------------
# Gemini: ask with multiple keys (rotation)
# -------------------------
def ask_gemini_multi_key(text: str, instruction: str, timeout=REQUEST_TIMEOUT_GEMINI):
    if not GEMINI_API_KEYS:
        raise RuntimeError("GEMINI_API_KEYS not set")
    last_exception = None
    for api_key in GEMINI_API_KEYS:
        try:
            url = f"{GEMINI_MODEL_ENDPOINT}?key={api_key}"
            payload = {"contents": [{"parts": [{"text": instruction}, {"text": text}]}]}
            headers = {"Content-Type": "application/json"}
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            result = resp.json()
            if "candidates" in result and isinstance(result["candidates"], list) and len(result["candidates"]) > 0:
                try:
                    return result['candidates'][0]['content']['parts'][0]['text']
                except Exception:
                    return json.dumps(result['candidates'][0])
            raise RuntimeError(f"Gemini response lacks candidates: {json.dumps(result)}")
        except Exception as e:
            logging.warning(f"Gemini API key failed: {str(e)}. Trying next key if available.")
            last_exception = e
            continue
    raise RuntimeError(f"All Gemini API keys failed. Last error: {str(last_exception) if last_exception else 'No keys were available.'}")

# -------------------------
# Transcribe wrapper (now: ONLY AssemblyAI)
# -------------------------
def transcribe_via_selected_service(input_path: str, lang_code: str):
    """
    This bot is configured to use AssemblyAI only (multiple keys rotated).
    """
    try:
        text = transcribe_file_with_assemblyai_multi_key(input_path, lang_code)
        if text is None:
            raise RuntimeError("AssemblyAI returned no text")
        return text, "assemblyai"
    except Exception as e:
        logging.exception("AssemblyAI failed (all keys).")
        raise RuntimeError("AssemblyAI failed: " + str(e))

# -------------------------
# attach buttons helper (kept)
# -------------------------
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

# -------------------------
# Core media handling (kept mostly same)
# -------------------------
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
        token = serializer.dumps({"chat_id": message.chat.id, "lang": lang, "bot_index": int(bot_index)})
        upload_link = f"{WEBHOOK_BASE.rstrip('/')}/upload/{token}"
        max_display_mb = TELEGRAM_MAX_BYTES // (1024 * 1024)
        text = f'Telegram API doesn’t allow me to download your file if it’s larger than {max_display_mb}MB:👉🏻 <a href="{upload_link}">Click here to Upload your file</a>'
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
            try:
                # ALWAYS use AssemblyAI (multi-key)
                text, used_service = transcribe_via_selected_service(tmpf.name, lang)
            except Exception as e:
                error_msg = str(e)
                logging.exception("Error during transcription")
                if "ffmpeg" in error_msg.lower(): bot_obj.send_message(message.chat.id, "⚠️ Server error: ffmpeg not found or conversion failed. Contact admin.", reply_to_message_id=message.message_id)
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
                sent_msg = bot_obj.send_message(message.chat.id, corrected_text or "⚠️ Warning: Make sure the voice is clear or you're speaking in the chosen language.", reply_to_message_id=message.message_id)
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
        if is_transcoding_like_error(error_msg):
            bot_obj.send_message(message.chat.id, "⚠️ Transcription error: file is not audible. Please send a different file.", reply_to_message_id=message.message_id)
        else:
            bot_obj.send_message(message.chat.id, f"Error during transcription: {error_msg}", reply_to_message_id=message.message_id)
    finally:
        stop_animation["stop"] = True
        animation_thread.join()
        try: bot_obj.delete_message(message.chat.id, processing_msg_id)
        except Exception: pass

# -------------------------
# /assemblyai endpoint (kept for direct uploads) using multi-key
# -------------------------
@app.route("/assemblyai", methods=["POST"])
def assemblyai_endpoint():
    lang = request.form.get("language", "en")
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file provided"}), 400
    b = f.read()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".upload")
    try:
        tmp.write(b)
        tmp.flush()
        tmp.close()
        try:
            return jsonify({"text": transcribe_file_with_assemblyai_multi_key(tmp.name, lang)}), 200
        except Exception as e:
            logging.exception("AssemblyAI multi-key transcription failed")
            return jsonify({"error": str(e)}), 500
    finally:
        try: os.remove(tmp.name)
        except Exception: pass

# -------------------------
# Gemini callbacks (get_key_points / clean_up) now use multi-key ask_gemini
# -------------------------
# (handlers registered below will call ask_gemini_multi_key)

# -------------------------
# Bot handlers registration
# -------------------------
def register_handlers(bot_obj, bot_token, bot_index):
    @bot_obj.message_handler(commands=['start'])
    def start_handler(message):
        try:
            update_user_activity(message.from_user.id)
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            bot_obj.send_message(message.chat.id, "Choose your file language for transcription using the below buttons:", reply_markup=build_lang_keyboard("start_select_lang"))
        except Exception:
            logging.exception("Error in start_handler")

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
            text = "Commands supported:\n/start - Show welcome message\n/lang  - Change language\n/mode  - Change result delivery mode\n/help  - This help message\n\nSend a voice/audio/video (up to Telegram limit) and I will transcribe it."
            bot_obj.send_message(message.chat.id, text)
        except Exception:
            logging.exception("Error in handle_help")

    @bot_obj.message_handler(commands=['lang'])
    def handle_lang(message):
        try:
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            kb = build_lang_keyboard("stt_lang")
            bot_obj.send_message(message.chat.id, "Choose your file language for transcription using the below buttons:", reply_markup=kb)
        except Exception:
            logging.exception("Error in handle_lang")

    @bot_obj.message_handler(commands=['mode'])
    def handle_mode(message):
        try:
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            current_mode = get_user_send_mode(str(message.from_user.id))
            mode_text = "📄 .txt file" if current_mode == "file" else "💬 Split messages"
            bot_obj.send_message(message.chat.id, f"Result delivery mode: {mode_text}. Change it below:", reply_markup=build_result_mode_keyboard())
        except Exception:
            logging.exception("Error in handle_mode")

    @bot_obj.message_handler(content_types=['new_chat_members'])
    def handle_new_chat_members(message):
        try:
            if message.new_chat_members[0].id == bot_obj.get_me().id:
                group_data = {'_id': str(message.chat.id), 'title': message.chat.title, 'type': message.chat.type, 'added_date': datetime.now()}
                groups_collection.update_one({'_id': group_data['_id']}, {'$set': group_data}, upsert=True)
                bot_obj.send_message(message.chat.id, "Thanks for adding me! I'm ready to transcribe your media files.")
        except Exception:
            logging.exception("Error in handle_new_chat_members")

    @bot_obj.message_handler(content_types=['left_chat_member'])
    def handle_left_chat_member(message):
        try:
            if message.left_chat_member.id == bot_obj.get_me().id:
                groups_collection.delete_one({'_id': str(message.chat.id)})
        except Exception:
            logging.exception("Error in handle_left_chat_member")

    @bot_obj.message_handler(content_types=['voice', 'audio', 'video', 'document'])
    def handle_media_types(message):
        try:
            handle_media_common(message, bot_obj, bot_token, bot_index)
        except Exception:
            logging.exception("Error in handle_media_types")

    @bot_obj.message_handler(content_types=['text'])
    def handle_text_messages(message):
        try:
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            bot_obj.send_message(message.chat.id, "For Text to Audio Use: @TextToSpeechBBot")
        except Exception:
            logging.exception("Error in handle_text_messages")

    @bot_obj.callback_query_handler(lambda c: c.data and c.data.startswith("get_key_points|"))
    def get_key_points_callback(call):
        try:
            parts = call.data.split("|")
            if len(parts) == 3:
                _, chat_id_part, msg_id_part = parts
            elif len(parts) == 2:
                _, msg_id_part = parts
                chat_id_part = str(call.message.chat.id)
            else:
                bot_obj.answer_callback_query(call.id, "Invalid request", show_alert=True)
                return
            try:
                chat_id_val = int(chat_id_part); msg_id = int(msg_id_part)
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
                try:
                    summary = ask_gemini_multi_key(stored, instruction)
                except Exception:
                    summary = extract_key_points_offline(stored, max_points=6)
            except Exception:
                summary = ""
            stop_animation["stop"] = True
            animation_thread.join()
            if not summary:
                try: bot_obj.edit_message_text("No Summary returned.", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                except Exception: pass
            else:
                try: bot_obj.edit_message_text(f"{summary}", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                except Exception: pass
        except Exception:
            logging.exception("Error in get_key_points_callback")

    @bot_obj.callback_query_handler(lambda c: c.data and c.data.startswith("clean_up|"))
    def clean_up_callback(call):
        try:
            parts = call.data.split("|")
            if len(parts) == 3:
                _, chat_id_part, msg_id_part = parts
            elif len(parts) == 2:
                _, msg_id_part = parts
                chat_id_part = str(call.message.chat.id)
            else:
                bot_obj.answer_callback_query(call.id, "Invalid request", show_alert=True)
                return
            try:
                chat_id_val = int(chat_id_part); msg_id = int(msg_id_part)
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
                try:
                    cleaned = ask_gemini_multi_key(stored, instruction)
                except Exception:
                    cleaned = normalize_text_offline(stored)
            except Exception:
                cleaned = ""
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
        except Exception:
            logging.exception("Error in clean_up_callback")

# register handlers for each bot
for idx, bot_obj in enumerate(bots):
    register_handlers(bot_obj, BOT_TOKENS[idx] if idx < len(BOT_TOKENS) else "", idx)

# -------------------------
# Webhook endpoints (kept)
# -------------------------
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
            except Exception:
                logging.exception("Error processing incoming webhook update")
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
        except Exception as e:
            logging.error(f"Failed to set main bot webhook on startup: {e}")

def set_bot_info_and_startup():
    set_webhook_on_startup()

if __name__ == "__main__":
    try:
        set_bot_info_and_startup()
        try:
            client.admin.command('ping')
            logging.info("Successfully connected to MongoDB!")
        except Exception as e:
            logging.error("Could not connect to MongoDB: %s", e)
    except Exception:
        logging.exception("Failed during startup")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
