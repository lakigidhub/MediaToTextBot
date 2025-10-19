import os
import logging
import requests
import telebot
import json
import threading
import time
import io
import subprocess
import glob
import re
import wave
import math
import tempfile
from flask import Flask, request, abort, jsonify, render_template_string, redirect
from datetime import datetime, timedelta
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, deque
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

env = os.environ

TELEGRAM_MAX_BYTES = int(env.get("TELEGRAM_MAX_BYTES", str(20 * 1024 * 1024)))
REQUEST_TIMEOUT_TELEGRAM = int(env.get("REQUEST_TIMEOUT_TELEGRAM", "300"))
REQUEST_TIMEOUT_GEMINI = int(env.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_CONCURRENT_TRANSCRIPTS = int(env.get("MAX_CONCURRENT_TRANSCRIPTS", "4"))
MAX_PENDING_QUEUE = int(env.get("MAX_PENDING_QUEUE", "1"))
MAX_WEB_UPLOAD_MB = int(env.get("MAX_WEB_UPLOAD_MB", "250"))
REQUEST_TIMEOUT_ASSEMBLY = int(env.get("REQUEST_TIMEOUT_ASSEMBLY", "300"))

GEMINI_API_KEYS = [t.strip() for t in env.get("GEMINI_API_KEYS", env.get("GEMINI_API_KEY", "")).split(",") if t.strip()]
WEBHOOK_URL = env.get("WEBHOOK_BASE", "").rstrip("/")
SECRET_KEY = env.get("SECRET_KEY", "testkey123")
ADMIN_PANEL_SECRET = env.get("ADMIN_PANEL_SECRET", SECRET_KEY)
MONGO_URI = env.get("MONGO_URI", "")
DB_NAME = env.get("DB_NAME", "telegram_bot_db")
REQUIRED_CHANNEL = env.get("REQUIRED_CHANNEL", "")
BOT_TOKEN = ([t.strip() for t in env.get("BOT_TOKENS", "").split(",") if t.strip()] + [""])[0]
ASSEMBLYAI_API_KEYS = [t.strip() for t in env.get("ASSEMBLYAI_API_KEYS", env.get("ASSEMBLYAI_API_KEY", "")).split(",") if t.strip()]
ASSEMBLYAI_BASE_URL = "https://api.assemblyai.com/v2"
raw_admins = env.get("ADMIN_USER_IDS", "")
ADMIN_USER_IDS = []
for part in [p.strip() for p in raw_admins.split(",") if p.strip()]:
    try:
        ADMIN_USER_IDS.append(int(part))
    except Exception:
        pass

if not BOT_TOKEN:
    logging.error("BOT_TOKEN is not set. Bot will not function.")

client = MongoClient(MONGO_URI) if MONGO_URI else MongoClient()
db = client[DB_NAME]
users_collection = db["users"]
groups_collection = db["groups"]
settings_collection = db["settings"]

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, parse_mode='HTML') if BOT_TOKEN else None
serializer = URLSafeTimedSerializer(SECRET_KEY)

_LANG_RAW = "🇬🇧 English:en,🇸🇦 العربية:ar,🇪🇸 Español:es,🇫🇷 Français:fr,🇷🇺 Русский:ru,🇩🇪 Deutsch:de,🇮🇳 हिन्दी:hi,🇮🇷 فارسی:fa,🇮🇩 Indonesia:id,🇺🇦 Українська:uk,🇦🇿 Azərbaycan:az,🇮🇹 Italiano:it,🇹🇷 Türkçe:tr,🇧🇬 Български:bg,🇷🇸 Srpski:sr,🇵🇰 اردو:ur,🇹🇭 ไทย:th,🇻🇳 Tiếng Việt:vi,🇯🇵 日本語:ja,🇰🇷 한국어:ko,🇨🇳 中文:zh,🇳🇱 Nederlands:nl,🇸🇪 Svenska:sv,🇳🇴 Norsk:no,🇮🇱 עברית:he,🇩🇰 Dansk:da,🇪🇹 አማርኛ:am,🇫🇮 Suomi:fi,🇧🇩 বাংলা:bn,🇰🇪 Kiswahili:sw,🇪🇹 Oromoo:om,🇳🇵 नेपाली:ne,🇵🇱 Polski:pl,🇬🇷 Ελληνικά:el,🇨🇿 Čeština:cs,🇮🇸 Íslenska:is,🇱🇹 Lietuvių:lt,🇱🇻 Latviešu:lv,🇭🇷 Hrvatski:hr,🇷🇸 Bosanski:bs,🇭🇺 Magyar:hu,🇷🇴 Română:ro,🇸🇴 Somali:so,🇲🇾 Melayu:ms,🇺🇿 O'zbekcha:uz,🇵🇭 Tagalog:tl,🇵🇹 Português:pt"
LANG_OPTIONS = [(p.split(":", 1)[0].strip(), p.split(":", 1)[1].strip()) for p in _LANG_RAW.split(",")]
CODE_TO_LABEL = {code: label for label, code in LANG_OPTIONS}
LABEL_TO_CODE = {label: code for label, code in LANG_OPTIONS}

user_transcriptions = {}
in_memory_data = {"pending_media": {}}
action_usage = {}
memory_lock = threading.Lock()
ALLOWED_EXTENSIONS = set(["mp3", "wav", "m4a", "ogg", "webm", "flac", "mp4", "mkv", "avi", "mov", "hevc", "aac", "aiff", "amr", "wma", "opus", "m4v", "ts", "flv", "3gp"])
FFMPEG_BINARY = env.get("FFMPEG_BINARY", "")

transcript_semaphore = threading.Semaphore(MAX_CONCURRENT_TRANSCRIPTS)
PENDING_QUEUE = deque()

def norm_user_id(uid):
    try:
        return str(int(uid))
    except:
        return str(uid)

def check_subscription(user_id, bot_obj):
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = bot_obj.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def send_subscription_message(chat_id, bot_obj):
    if not REQUIRED_CHANNEL:
        return
    try:
        chat = bot_obj.get_chat(chat_id)
        if chat.type != 'private':
            return
    except:
        return
    try:
        m = InlineKeyboardMarkup()
        m.add(InlineKeyboardButton("Click here to join the Group ", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"))
        bot_obj.send_message(chat_id, "🔒 Access Locked You cannot use this bot until you join the Group.", reply_markup=m)
    except:
        pass

def update_user_activity(user_id):
    uid = norm_user_id(user_id)
    now = datetime.now()
    users_collection.update_one({"user_id": uid}, {"$set": {"last_active": now}, "$setOnInsert": {"first_seen": now, "stt_conversion_count": 0}}, upsert=True)

def increment_processing_count(user_id, service_type):
    users_collection.update_one({"user_id": norm_user_id(user_id)}, {"$inc": {f"{service_type}_conversion_count": 1}})

def get_stt_user_lang(user_id):
    ud = users_collection.find_one({"user_id": norm_user_id(user_id)})
    return ud.get("stt_language", "en") if ud else "en"

def set_stt_user_lang(user_id, lang_code):
    users_collection.update_one({"user_id": norm_user_id(user_id)}, {"$set": {"stt_language": lang_code}}, upsert=True)

def get_user_send_mode(user_id):
    ud = users_collection.find_one({"user_id": norm_user_id(user_id)})
    return ud.get("stt_send_mode", "file") if ud else "file"

def set_user_send_mode(user_id, mode):
    if mode not in ("file", "split"):
        mode = "file"
    users_collection.update_one({"user_id": norm_user_id(user_id)}, {"$set": {"stt_send_mode": mode}}, upsert=True)

def delete_transcription_later(user_id, message_id):
    time.sleep(86400)
    with memory_lock:
        if user_id in user_transcriptions and message_id in user_transcriptions[user_id]:
            del user_transcriptions[user_id][message_id]

def is_transcoding_like_error(msg):
    if not msg:
        return False
    m = msg.lower()
    checks = ["transcoding failed", "file does not appear to contain audio", "text/html", "html document", "unsupported media type", "could not decode"]
    return any(ch in m for ch in checks)

def build_lang_keyboard(callback_prefix, row_width=3, message_id=None):
    m = InlineKeyboardMarkup(row_width=row_width)
    buttons = [InlineKeyboardButton(label, callback_data=f"{callback_prefix}|{code}|{message_id}" if message_id else f"{callback_prefix}|{code}") for label, code in LANG_OPTIONS]
    for i in range(0, len(buttons), row_width):
        m.add(*buttons[i:i + row_width])
    return m

def build_result_mode_keyboard(prefix="result_mode"):
    m = InlineKeyboardMarkup(row_width=2)
    m.add(InlineKeyboardButton("📄 .txt file", callback_data=f"{prefix}|file"), InlineKeyboardButton("💬 Split messages", callback_data=f"{prefix}|split"))
    return m

def signed_upload_token(chat_id, lang_code):
    return serializer.dumps({"chat_id": chat_id, "lang": lang_code})

def unsign_upload_token(token, max_age_seconds=3600):
    return serializer.loads(token, max_age=max_age_seconds)

def animate_processing_message(bot_obj, chat_id, message_id, stop_event):
    frames = ["🔄 Processing", "🔄 Processing.", "🔄 Processing..", "🔄 Processing..."]
    idx = 0
    while not stop_event():
        try:
            bot_obj.edit_message_text(frames[idx % len(frames)], chat_id=chat_id, message_id=message_id)
        except:
            pass
        idx = (idx + 1) % len(frames)
        time.sleep(0.6)

def normalize_text_offline(text):
    return re.sub(r'\s+', ' ', text).strip() if text else text

def extract_key_points_offline(text, max_points=6):
    if not text:
        return ""
    sentences = [s.strip() for s in re.split(r'(?<=[\.\!\?])\s+', text) if s.strip()]
    if not sentences:
        return ""
    words = [w for w in re.findall(r'\w+', text.lower()) if len(w) > 3]
    if not words:
        return "\n".join(f"- {s}" for s in sentences[:max_points])
    freq = Counter(words)
    sentence_scores = [(sum(freq.get(w, 0) for w in re.findall(r'\w+', s.lower())), s) for s in sentences]
    sentence_scores.sort(key=lambda x: x[0], reverse=True)
    top_sentences = sorted(sentence_scores[:max_points], key=lambda x: sentences.index(x[1]))
    return "\n".join(f"- {s}" for _, s in top_sentences)

def safe_extension_from_filename(filename):
    return filename.rsplit(".", 1)[-1].lower() if filename and "." in filename else ""

def telegram_file_info_and_url(bot_token, file_id):
    url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_TELEGRAM)
    resp.raise_for_status()
    file_path = resp.json().get("result", {}).get("file_path")
    return type('T', (), {'file_path': file_path})(), f"https://api.telegram.org/file/bot{bot_token}/{file_path}"

def transcribe_file_with_assemblyai(audio_source, language_code):
    if not ASSEMBLYAI_API_KEYS:
        raise RuntimeError("ASSEMBLYAI_API_KEYS not set")
    last_exception = None
    for api_key in ASSEMBLYAI_API_KEYS:
        try:
            headers = {"Authorization": api_key, "Content-Type": "application/json", "Accept": "application/json"}
            config = {"audio_url": audio_source}
            if language_code != "en":
                config["language_code"] = language_code
            submit_url = f"{ASSEMBLYAI_BASE_URL}/transcript"
            submit_resp = requests.post(submit_url, headers=headers, json=config, timeout=REQUEST_TIMEOUT_ASSEMBLY)
            submit_resp.raise_for_status()
            transcript_id = submit_resp.json().get("id")
            if not transcript_id:
                raise RuntimeError("AssemblyAI submission failed: No transcript ID received")
            poll_url = f"{ASSEMBLYAI_BASE_URL}/transcript/{transcript_id}"
            start_time = time.time()
            while time.time() - start_time < REQUEST_TIMEOUT_ASSEMBLY:
                poll_resp = requests.get(poll_url, headers={"Authorization": api_key}, timeout=30)
                poll_resp.raise_for_status()
                status = poll_resp.json().get("status")
                if status == "completed":
                    return poll_resp.json().get("text", "")
                elif status in ["failed", "error"]:
                    raise RuntimeError(f"AssemblyAI transcription failed. Details: {poll_resp.json()}")
                elif status == "processing":
                    time.sleep(5)
                else:
                    time.sleep(3)
            raise RuntimeError("AssemblyAI transcription timed out")
        except Exception as e:
            logging.warning(f"AssemblyAI key failed: {str(e)}. Trying next key if available.")
            last_exception = e
            continue
    raise RuntimeError(f"All AssemblyAI keys failed. Last error: {str(last_exception) if last_exception else 'No keys were available.'}")

def upload_file_with_assemblyai(file_path):
    if not ASSEMBLYAI_API_KEYS:
        raise RuntimeError("ASSEMBLYAI_API_KEYS not set")
    last_exception = None
    for api_key in ASSEMBLYAI_API_KEYS:
        try:
            headers = {"Authorization": api_key, "Content-Type": "application/octet-stream"}
            with open(file_path, 'rb') as f:
                upload_resp = requests.post(f"{ASSEMBLYAI_BASE_URL}/upload", headers=headers, data=f, timeout=REQUEST_TIMEOUT_ASSEMBLY)
            upload_resp.raise_for_status()
            upload_url = upload_resp.json().get("upload_url")
            if not upload_url:
                raise RuntimeError("AssemblyAI upload failed: No upload_url received")
            return upload_url
        except Exception as e:
            logging.warning(f"AssemblyAI upload key failed: {str(e)}. Trying next key if available.")
            last_exception = e
            continue
    raise RuntimeError(f"All AssemblyAI upload keys failed. Last error: {str(last_exception) if last_exception else 'No keys were available.'}")

def transcribe_via_selected_service(input_source, lang_code, is_local_file=False):
    if is_local_file:
        try:
            upload_url = upload_file_with_assemblyai(input_source)
            text, service = transcribe_file_with_assemblyai(upload_url, lang_code), "assemblyai_upload"
            if text is None:
                raise RuntimeError("AssemblyAI returned no text")
            return text, service
        except Exception as e:
            raise RuntimeError("AssemblyAI upload and transcription failed: " + str(e))
    else:
        try:
            text, service = transcribe_file_with_assemblyai(input_source, lang_code), "assemblyai_url"
            if text is None:
                raise RuntimeError("AssemblyAI returned no text")
            return text, service
        except Exception as e:
            raise RuntimeError("AssemblyAI transcription failed: " + str(e))

def split_text_into_chunks(text, limit=4096):
    if not text:
        return []
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + limit, n)
        if end < n:
            last_space = text.rfind(" ", start, end)
            if last_space > start:
                end = last_space
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
        m = InlineKeyboardMarkup()
        m.add(InlineKeyboardButton("⭐️Clean transcript", callback_data=f"clean_up|{chat_id}|{message_id}"))
        if include_summarize:
            m.add(InlineKeyboardButton("Get Summarize", callback_data=f"get_key_points|{chat_id}|{message_id}"))
        try:
            bot_obj.edit_message_reply_markup(chat_id, message_id, reply_markup=m)
        except:
            pass
    except:
        pass
    try:
        action_usage[f"{chat_id}|{message_id}|clean_up"] = 0
        action_usage[f"{chat_id}|{message_id}|get_key_points"] = 0
    except:
        pass

def process_transcription_result(bot_obj, chat_id, message_id, corrected_text, uid):
    uid_key = str(chat_id)
    user_mode = get_user_send_mode(uid_key)
    sent_msg = None
    if len(corrected_text) > 4000:
        if user_mode == "file":
            f = io.BytesIO(corrected_text.encode("utf-8"))
            f.name = "Transcript.txt"
            sent = bot_obj.send_document(chat_id, f, reply_to_message_id=message_id)
            sent_msg = sent
        else:
            chunks = split_text_into_chunks(corrected_text, limit=4096)
            last_sent = None
            for idx, chunk in enumerate(chunks):
                if idx == 0:
                    last_sent = bot_obj.send_message(chat_id, chunk, reply_to_message_id=message_id)
                else:
                    last_sent = bot_obj.send_message(chat_id, chunk)
            sent_msg = last_sent
    else:
        sent_msg = bot_obj.send_message(chat_id, corrected_text or "⚠️ Warning Make sure the voice is clear or speaking in the language you Choosed.", reply_to_message_id=message_id)

    if sent_msg:
        try:
            attach_action_buttons(bot_obj, chat_id, sent_msg.message_id, corrected_text)
        except:
            pass
        try:
            user_transcriptions.setdefault(uid_key, {})[sent_msg.message_id] = corrected_text
            threading.Thread(target=delete_transcription_later, args=(uid_key, sent_msg.message_id), daemon=True).start()
        except:
            pass
    increment_processing_count(uid, "stt")

def process_file_source(message, bot_obj, bot_token, file_id, file_size, filename, is_local_file=False, local_file_path=None):
    uid = str(message.from_user.id)
    chatid = str(message.chat.id)
    lang = get_stt_user_lang(uid)
    original_msg_id = message.message_id
    is_web_upload = (local_file_path is not None)

    processing_msg = bot_obj.send_message(chatid, "🔄 Processing...", reply_to_message_id=original_msg_id if not is_web_upload else None)
    processing_msg_id = processing_msg.message_id
    stop = {"stop": False}
    animation_thread = threading.Thread(target=animate_processing_message, args=(bot_obj, chatid, processing_msg_id, lambda: stop["stop"]))
    animation_thread.start()

    try:
        if is_local_file:
            input_source = local_file_path
        else:
            tf, file_url = telegram_file_info_and_url(bot_token, file_id)
            input_source = file_url

        try:
            text, used_service = transcribe_via_selected_service(input_source, lang, is_local_file)
        except Exception as e:
            error_msg = str(e)
            logging.exception("Error during transcription")
            if is_transcoding_like_error(error_msg):
                bot_obj.send_message(chatid, "⚠️ Transcription error: file is not audible or format is unsupported.", reply_to_message_id=original_msg_id if not is_web_upload else None)
            else:
                bot_obj.send_message(chatid, f"Error during transcription: {error_msg}", reply_to_message_id=original_msg_id if not is_web_upload else None)
            return

        corrected_text = normalize_text_offline(text)
        process_transcription_result(bot_obj, chatid, original_msg_id, corrected_text, uid)

    finally:
        stop["stop"] = True
        animation_thread.join()
        try:
            bot_obj.delete_message(chatid, processing_msg_id)
        except:
            pass
        if is_local_file and local_file_path:
            try:
                os.remove(local_file_path)
            except:
                pass

def worker_thread():
    while True:
        try:
            transcript_semaphore.acquire()
            item = None
            with memory_lock:
                if PENDING_QUEUE:
                    item = PENDING_QUEUE.popleft()
            if item:
                source_type = item[0]
                if source_type == "telegram":
                    _, message, bot_obj, bot_token, file_id, file_size, filename = item
                    logging.info(f"Starting Telegram processing for user {message.from_user.id}. Queue size: {len(PENDING_QUEUE)}")
                    process_file_source(message, bot_obj, bot_token, file_id, file_size, filename)
                elif source_type == "web_upload":
                    _, chat_id, lang, local_file_path = item
                    # Create a dummy message object for uniformity (only need .from_user.id, .chat.id, .message_id)
                    DummyMessage = type('DummyMessage', (), {})
                    dummy_msg = DummyMessage()
                    dummy_msg.from_user = type('DummyUser', (), {'id': chat_id})()
                    dummy_msg.chat = type('DummyChat', (), {'id': chat_id})()
                    dummy_msg.message_id = -1 # A placeholder or use a log message ID if available
                    logging.info(f"Starting Web Upload processing for chat {chat_id}. Queue size: {len(PENDING_QUEUE)}")
                    process_file_source(dummy_msg, bot, BOT_TOKEN, None, None, None, is_local_file=True, local_file_path=local_file_path)
            else:
                transcript_semaphore.release()
        except:
            logging.exception("Error in worker thread")
        finally:
            if item:
                transcript_semaphore.release()
            time.sleep(0.5)

def start_worker_threads():
    for i in range(MAX_CONCURRENT_TRANSCRIPTS):
        t = threading.Thread(target=worker_thread, daemon=True)
        t.start()

start_worker_threads()

def handle_media_common(message, bot_obj, bot_token):
    if not bot_obj:
        return
    update_user_activity(message.from_user.id)
    if message.chat.type == 'private' and not check_subscription(message.from_user.id, bot_obj):
        send_subscription_message(message.chat.id, bot_obj)
        return
    file_id = file_size = filename = None
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
        if (mime and ("audio" in mime or "video" in mime)) or ext in ALLOWED_EXTENSIONS:
            file_id = message.document.file_id
            file_size = message.document.file_size
        else:
            bot_obj.send_message(message.chat.id, "Sorry, I can only transcribe audio or video files.")
            return

    if file_size and file_size > TELEGRAM_MAX_BYTES:
        lang = get_stt_user_lang(str(message.from_user.id))
        token = signed_upload_token(message.chat.id, lang)
        upload_link = f"{WEBHOOK_URL.rstrip('/')}/upload/{token}"
        max_display_mb = TELEGRAM_MAX_BYTES // (1024 * 1024)
        text = f'The file is larger than {max_display_mb}MB: <a href="{upload_link}">So upload it here</a>'
        bot_obj.send_message(message.chat.id, text, disable_web_page_preview=True, parse_mode='HTML', reply_to_message_id=message.message_id)
        return

    with memory_lock:
        if len(PENDING_QUEUE) >= MAX_PENDING_QUEUE:
            bot_obj.send_message(message.chat.id, "⚠️ Server busy. Try again later.", reply_to_message_id=message.message_id)
            return
        PENDING_QUEUE.append(("telegram", message, bot_obj, bot_token, file_id, file_size, filename))

def ask_gemini(text, instruction, timeout=REQUEST_TIMEOUT_GEMINI):
    if not GEMINI_API_KEYS:
        raise RuntimeError("GEMINI_API_KEYS not set")
    last_exception = None
    for api_key in GEMINI_API_KEYS:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
            payload = {"contents": [{"parts": [{"text": instruction}, {"text": text}]}]}
            headers = {"Content-Type": "application/json"}
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            result = resp.json()
            if "candidates" in result and isinstance(result["candidates"], list) and len(result["candidates"]) > 0:
                try:
                    return result['candidates'][0]['content']['parts'][0]['text']
                except:
                    return json.dumps(result['candidates'][0])
            raise RuntimeError(f"Gemini response lacks candidates: {json.dumps(result)}")
        except Exception as e:
            logging.warning(f"Gemini API key failed: {str(e)}. Trying next key if available.")
            last_exception = e
            continue
    raise RuntimeError(f"All Gemini API keys failed. Last error: {str(last_exception) if last_exception else 'No keys were available.'}")

def register_handlers(bot_obj, bot_token):
    if not bot_obj:
        return

    @bot_obj.message_handler(commands=['start'])
    def start_handler(message):
        try:
            update_user_activity(message.from_user.id)
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            bot_obj.send_message(message.chat.id, "Choose your file language for transcription using the below buttons:", reply_markup=build_lang_keyboard("start_select_lang"))
        except:
            logging.exception("Error in start_handler")

    @bot_obj.callback_query_handler(func=lambda c: c.data and c.data.startswith("start_select_lang|"))
    def start_select_lang_callback(call):
        try:
            uid = str(call.from_user.id)
            _, lang_code = call.data.split("|", 1)
            lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
            set_stt_user_lang(uid, lang_code)
            try:
                bot_obj.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            welcome_text = "👋 Salaam!    \n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free"
            bot_obj.send_message(call.message.chat.id, welcome_text)
            bot_obj.answer_callback_query(call.id, f"✅ Language set to {lang_label}")
        except:
            logging.exception("Error in start_select_lang_callback")
            try:
                bot_obj.answer_callback_query(call.id, "❌ Error setting language, try again.", show_alert=True)
            except:
                pass

    @bot_obj.message_handler(commands=['help'])
    def handle_help(message):
        try:
            update_user_activity(message.from_user.id)
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            text = "Commands supported:\n/start - Show welcome message\n/lang  - Change language\n/mode  - Change result delivery mode\n/help  - This help message\n\nSend a voice/audio/video (up to 20MB for Telegram) and I will transcribe it.\nIf it's larger than Telegram limits, you'll be provided a secure web upload link (supports up to 250MB) Need more help? Contact: @lakigithub"
            bot_obj.send_message(message.chat.id, text)
        except:
            logging.exception("Error in handle_help")

    @bot_obj.message_handler(commands=['lang'])
    def handle_lang(message):
        try:
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            kb = build_lang_keyboard("stt_lang")
            bot_obj.send_message(message.chat.id, "Choose your file language for transcription using the below buttons:", reply_markup=kb)
        except:
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
        except:
            logging.exception("Error in handle_mode")

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
            link = f"{WEBHOOK_URL.rstrip('/')}/admin?secret={ADMIN_PANEL_SECRET}"
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
            try:
                bot_obj.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
        except:
            logging.exception("Error in on_stt_language_select")
            try:
                bot_obj.answer_callback_query(call.id, "❌ Error setting language, try again.", show_alert=True)
            except:
                pass

    @bot_obj.callback_query_handler(lambda c: c.data and c.data.startswith("result_mode|"))
    def on_result_mode_select(call):
        try:
            uid = str(call.from_user.id)
            _, mode = call.data.split("|", 1)
            set_user_send_mode(uid, mode)
            mode_text = "📄 .txt file" if mode == "file" else "💬 Split messages"
            try:
                bot_obj.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass
            bot_obj.answer_callback_query(call.id, f"✅ Result mode set: {mode_text}")
        except:
            logging.exception("Error in on_result_mode_select")
            try:
                bot_obj.answer_callback_query(call.id, "❌ Error setting result mode, try again.", show_alert=True)
            except:
                pass

    @bot_obj.message_handler(content_types=['new_chat_members'])
    def handle_new_chat_members(message):
        try:
            if message.new_chat_members[0].id == bot_obj.get_me().id:
                group_data = {'_id': str(message.chat.id), 'title': message.chat.title, 'type': message.chat.type, 'added_date': datetime.now()}
                groups_collection.update_one({'_id': group_data['_id']}, {'$set': group_data}, upsert=True)
                bot_obj.send_message(message.chat.id, "Thanks for adding me! I'm ready to transcribe your media files.")
        except:
            logging.exception("Error in handle_new_chat_members")

    @bot_obj.message_handler(content_types=['left_chat_member'])
    def handle_left_chat_member(message):
        try:
            if message.left_chat_member.id == bot_obj.get_me().id:
                groups_collection.delete_one({'_id': str(message.chat.id)})
        except:
            logging.exception("Error in handle_left_chat_member")

    @bot_obj.message_handler(content_types=['voice', 'audio', 'video', 'document'])
    def handle_media_types(message):
        try:
            handle_media_common(message, bot_obj, bot_token)
        except:
            logging.exception("Error in handle_media_types")

    @bot_obj.message_handler(content_types=['text'])
    def handle_text_messages(message):
        try:
            if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            bot_obj.send_message(message.chat.id, "For Text to Audio Use: @TextToSpeechBBot")
        except:
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
                chat_id_val = int(chat_id_part)
                msg_id = int(msg_id_part)
            except:
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
            stop = {"stop": False}
            animation_thread = threading.Thread(target=animate_processing_message, args=(bot_obj, call.message.chat.id, status_msg.message_id, lambda: stop["stop"]))
            animation_thread.start()
            try:
                lang = get_stt_user_lang(str(chat_id_val)) or "en"
                instruction = f"What is this report and what is it about? Please summarize them for me into (lang={lang}) without adding any introductions, notes, or extra phrases."
                try:
                    summary = ask_gemini(stored, instruction)
                except:
                    summary = extract_key_points_offline(stored, max_points=6)
            except:
                summary = ""
            stop["stop"] = True
            animation_thread.join()
            if not summary:
                try:
                    bot_obj.edit_message_text("No Summary returned.", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                except:
                    pass
            else:
                try:
                    bot_obj.edit_message_text(f"{summary}", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                except:
                    pass
        except:
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
                chat_id_val = int(chat_id_part)
                msg_id = int(msg_id_part)
            except:
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
            stop = {"stop": False}
            animation_thread = threading.Thread(target=animate_processing_message, args=(bot_obj, call.message.chat.id, status_msg.message_id, lambda: stop["stop"]))
            animation_thread.start()
            try:
                lang = get_stt_user_lang(str(chat_id_val)) or "en"
                instruction = f"Clean and normalize this transcription (lang={lang}). Remove ASR artifacts like [inaudible], repeated words, filler noises, timestamps, and incorrect punctuation. Produce a clean, well-punctuated, readable text in the same language. Do not add introductions or explanations."
                try:
                    cleaned = ask_gemini(stored, instruction)
                except:
                    cleaned = normalize_text_offline(stored)
            except:
                cleaned = ""
            stop["stop"] = True
            animation_thread.join()
            if not cleaned:
                try:
                    bot_obj.edit_message_text("No cleaned text returned.", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                except:
                    pass
                return
            uid_key = str(chat_id_val)
            user_mode = get_user_send_mode(uid_key)
            if len(cleaned) > 4000:
                if user_mode == "file":
                    f = io.BytesIO(cleaned.encode("utf-8"))
                    f.name = "cleaned.txt"
                    try:
                        bot_obj.delete_message(call.message.chat.id, status_msg.message_id)
                    except:
                        pass
                    sent = bot_obj.send_document(call.message.chat.id, f, reply_to_message_id=call.message.message_id)
                    try:
                        user_transcriptions.setdefault(uid_key, {})[sent.message_id] = cleaned
                        threading.Thread(target=delete_transcription_later, args=(uid_key, sent.message_id), daemon=True).start()
                    except:
                        pass
                    try:
                        action_usage[f"{call.message.chat.id}|{sent.message_id}|clean_up"] = 0
                        action_usage[f"{call.message.chat.id}|{sent.message_id}|get_key_points"] = 0
                    except:
                        pass
                else:
                    try:
                        bot_obj.delete_message(call.message.chat.id, status_msg.message_id)
                    except:
                        pass
                    chunks = split_text_into_chunks(cleaned, limit=4096)
                    last_sent = None
                    for idx, chunk in enumerate(chunks):
                        if idx == 0:
                            last_sent = bot_obj.send_message(call.message.chat.id, chunk, reply_to_message_id=call.message.message_id)
                        else:
                            last_sent = bot_obj.send_message(call.message.chat.id, chunk)
                    try:
                        user_transcriptions.setdefault(uid_key, {})[last_sent.message_id] = cleaned
                        threading.Thread(target=delete_transcription_later, args=(uid_key, last_sent.message_id), daemon=True).start()
                    except:
                        pass
                    try:
                        action_usage[f"{call.message.chat.id}|{last_sent.message_id}|clean_up"] = 0
                        action_usage[f"{call.message.chat.id}|{last_sent.message_id}|get_key_points"] = 0
                    except:
                        pass
            else:
                try:
                    bot_obj.edit_message_text(f"{cleaned}", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                    uid_key = str(chat_id_val)
                    user_transcriptions.setdefault(uid_key, {})[status_msg.message_id] = cleaned
                    threading.Thread(target=delete_transcription_later, args=(uid_key, status_msg.message_id), daemon=True).start()
                    action_usage[f"{call.message.chat.id}|{status_msg.message_id}|clean_up"] = 0
                    action_usage[f"{call.message.chat.id}|{status_msg.message_id}|get_key_points"] = 0
                except:
                    pass
        except:
            logging.exception("Error in clean_up_callback")

if bot:
    register_handlers(bot, BOT_TOKEN)

HTML_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Upload File</title><style>body{font-family:Arial,sans-serif;background-color:#f0f2f5;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;color:#333}.container{background-color:#fff;padding:30px;border-radius:12px;box-shadow:0 4px 15px rgba(0,0,0,0.1);text-align:center;width:90%;max-width:500px;box-sizing:border-box}h2{margin-top:0;color:#555;font-size:1.5rem}p{font-size:0.9rem;color:#666;margin-bottom:20px}.file-upload-wrapper{position:relative;overflow:hidden;display:inline-block;cursor:pointer;width:100%}.file-upload-input{position:absolute;left:0;top:0;opacity:0;cursor:pointer;font-size:100px;width:100%;height:100%}.file-upload-label{background-color:#007bff;color:#fff;padding:12px 20px;border-radius:8px;transition:background-color 0.3s;display:block;font-size:1rem}.file-upload-label:hover{background-color:#0056b3}#file-name{margin-top:15px;font-style:italic;color:#777;font-size:0.9rem;word-wrap:break-word;overflow-wrap:break-word;min-height:20px}#progress-bar-container{width:100%;background-color:#e0e0e0;border-radius:5px;margin-top:20px;display:none}#progress-bar{width:0%;height:15px;background-color:#28a745;border-radius:5px;text-align:center;color:white;line-height:15px;transition:width 0.3s ease}#status-message{margin-top:15px;font-weight:bold}.loading-spinner{display:none;width:40px;height:40px;border:4px solid #f3f3f3;border-top:4px solid #007bff;border-radius:50%;animation:spin 1s linear infinite;margin:20px auto}@keyframes spin{0%{transform:rotate(0deg)}100%{transform:rotate(360deg)}}@media (max-width:600px){.container{padding:20px}}</style></head><body><div class="container"><h2>Upload Your Audio/Video</h2><p>Your file is too big for Telegram, but you can upload it here. Max size: {{ max_mb }}MB.</p><div class="file-upload-wrapper"><input type="file" id="file-input" class="file-upload-input" accept=".mp3,.wav,.m4a,.ogg,.webm,.flac,.mp4,.mkv,.avi,.mov,.hevc,.aac,.aiff,.amr,.wma,.opus,.m4v,.ts,.flv,.3gp"><label for="file-input" class="file-upload-label"><span id="upload-text">Choose File to Upload</span></label></div><div id="file-name"></div><div id="progress-bar-container"><div id="progress-bar">0%</div></div><div id="status-message"></div><div class="loading-spinner" id="spinner"></div></div><script>const fileInput=document.getElementById('file-input');const fileNameDiv=document.getElementById('file-name');const progressBarContainer=document.getElementById('progress-bar-container');const progressBar=document.getElementById('progress-bar');const statusMessageDiv=document.getElementById('status-message');const spinner=document.getElementById('spinner');const uploadTextSpan=document.getElementById('upload-text');const MAX_SIZE_MB={{ max_mb }};fileInput.addEventListener('change',function(){if(this.files.length>0){const file=this.files[0];fileNameDiv.textContent=`Selected: ${file.name}`;statusMessageDiv.textContent='';progressBarContainer.style.display='none';progressBar.style.width='0%';progressBar.textContent='0%';if(file.size>MAX_SIZE_MB*1024*1024){statusMessageDiv.style.color='red';statusMessageDiv.textContent=`Error: File size exceeds the maximum limit of ${MAX_SIZE_MB}MB.`;uploadTextSpan.textContent='Choose File to Upload';fileNameDiv.textContent=''}else{uploadFile(file)}}});function uploadFile(file){const formData=new FormData();formData.append('file',file);const xhr=new XMLHttpRequest();xhr.open('POST',window.location.href);xhr.upload.addEventListener('progress',function(e){if(e.lengthComputable){const percent=Math.round((e.loaded/e.total)*100);progressBarContainer.style.display='block';progressBar.style.width=percent+'%';progressBar.textContent=percent+'%';statusMessageDiv.textContent=`Uploading... ${percent}%`;if(percent===100){statusMessageDiv.textContent='Upload complete. Processing...';spinner.style.display='block'}}});xhr.onload=function(){spinner.style.display='none';if(xhr.status===200){statusMessageDiv.style.color='#28a745';statusMessageDiv.textContent='Success! Your transcript will be sent to your Telegram chat shortly.'}else{statusMessageDiv.style.color='red';statusMessageDiv.textContent=`Error: ${xhr.responseText||'An unknown error occurred.'}`}};xhr.onerror=function(){spinner.style.display='none';statusMessageDiv.style.color='red';statusMessageDiv.textContent='Network error. Please try again.'};xhr.send(formData);}</script></body></html>"""

ADMIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Admin Panel</title><style>body{font-family:Arial,sans-serif;margin:30px}.box{border:1px solid #ddd;padding:20px;border-radius:8px;max-width:900px}input[type="text"],textarea,select{width:100%;padding:8px;margin:6px 0 12px 0;box-sizing:border-box}label{font-weight:bold}.row{display:flex;gap:12px}.col{flex:1}button{padding:10px 16px;background:#007bff;color:#fff;border:none;border-radius:6px;cursor:pointer}button:disabled{opacity:0.6}</style></head><body><div class="box"><h2>Admin Panel</h2><p>Users: {{ users_count }} | Groups: {{ groups_count }}</p><hr><h3>Send Broadcast / Ads</h3><form method="post" action="/admin/send_ads" enctype="multipart/form-data"><input type="hidden" name="secret" value="{{ secret }}"><label>Send To</label><select name="target"><option value="all">All Users & Groups</option><option value="users">Users Only</option><option value="groups">Groups Only</option></select><label>Message Type</label><select name="msg_type"><option value="text">Text</option><option value="photo">Photo</option><option value="video">Video</option><option value="audio">Audio</option><option value="voice">Voice</option><option value="document">Document</option></select><label>Message Text (for text or caption)</label><textarea name="message_text" rows="6"></textarea><label>Attach File (optional)</label><input type="file" name="media_file"><div style="margin-top:12px"><button type="submit">Send Ads</button></div></form><hr><form method="post" action="/admin/save_settings"><input type="hidden" name="secret" value="{{ secret }}"><label>Admin Panel Secret</label><input type="text" name="admin_secret" value="{{ admin_secret }}"><div style="margin-top:12px"><button type="submit">Save Settings</button></div></form></div></body></html>"""

def broadcast_worker(target_type, msg_type, text, file_path):
    if not bot:
        return
    recipients = []
    try:
        if target_type in ("all", "users"):
            for u in users_collection.find({}, {"user_id": 1}):
                try:
                    recipients.append(int(u["user_id"]))
                except Exception:
                    continue
        if target_type in ("all", "groups"):
            for g in groups_collection.find({}, {"_id": 1}):
                try:
                    recipients.append(int(g["_id"]))
                except Exception:
                    continue
    except Exception:
        logging.exception("Failed to gather recipients for broadcast")
    sent_count = 0
    last_exception = None
    for r in recipients:
        try:
            if msg_type == "text":
                bot.send_message(r, text, parse_mode='HTML')
            elif msg_type == "photo":
                if file_path:
                    with open(file_path, "rb") as f:
                        bot.send_photo(r, f, caption=text or None)
                else:
                    bot.send_message(r, text or "Photo not provided.")
            elif msg_type == "video":
                if file_path:
                    with open(file_path, "rb") as f:
                        bot.send_video(r, f, caption=text or None)
                else:
                    bot.send_message(r, text or "Video not provided.")
            elif msg_type == "audio":
                if file_path:
                    with open(file_path, "rb") as f:
                        bot.send_audio(r, f, caption=text or None)
                else:
                    bot.send_message(r, text or "Audio not provided.")
            elif msg_type == "voice":
                if file_path:
                    with open(file_path, "rb") as f:
                        bot.send_voice(r, f, caption=text or None)
                else:
                    bot.send_message(r, text or "Voice not provided.")
            elif msg_type == "document":
                if file_path:
                    with open(file_path, "rb") as f:
                        bot.send_document(r, f, caption=text or None)
                else:
                    bot.send_message(r, text or "Document not provided.")
            sent_count += 1
        except Exception as e:
            last_exception = e
            logging.warning(f"Broadcast send error to {r}: {e}")
        time.sleep(0.06)
    try:
        if file_path:
            try:
                os.remove(file_path)
            except Exception:
                pass
    except Exception:
        pass
    logging.info(f"Broadcast finished, sent={sent_count}, last_exc={str(last_exception)}")

@app.route("/admin", methods=["GET"])
def admin_ui():
    global ADMIN_PANEL_SECRET
    secret = request.args.get("secret")
    if not secret or secret != ADMIN_PANEL_SECRET:
        return abort(403)
    try:
        users_count = users_collection.count_documents({})
    except Exception:
        users_count = 0
    try:
        groups_count = groups_collection.count_documents({})
    except Exception:
        groups_count = 0
    return render_template_string(ADMIN_HTML, users_count=users_count, groups_count=groups_count, secret=ADMIN_PANEL_SECRET, admin_secret=ADMIN_PANEL_SECRET)

@app.route("/admin/save_settings", methods=["POST"])
def admin_save_settings():
    global ADMIN_PANEL_SECRET
    secret = request.form.get("secret")
    if not secret or secret != ADMIN_PANEL_SECRET:
        return abort(403)
    new_secret = request.form.get("admin_secret")
    if new_secret:
        try:
            settings_collection.update_one({"_id": "admin_panel_secret"}, {"$set": {"value": new_secret}}, upsert=True)
            ADMIN_PANEL_SECRET = new_secret
        except Exception:
            pass
    return redirect(f"/admin?secret={ADMIN_PANEL_SECRET}")

@app.route("/admin/send_ads", methods=["POST"])
def admin_send_ads():
    secret = request.form.get("secret")
    if not secret or secret != ADMIN_PANEL_SECRET:
        return abort(403)
    target = request.form.get("target") or "all"
    msg_type = request.form.get("msg_type") or "text"
    message_text = request.form.get("message_text") or ""
    file = request.files.get("media_file")
    tmp_path = None
    if file:
        filename = file.filename or f"upload_{int(time.time()*1000)}"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix="." + safe_extension_from_filename(filename) if "." in filename else ".dat")
        try:
            file.save(tmp.name)
            tmp_path = tmp.name
        except Exception:
            try:
                tmp.close()
                os.remove(tmp.name)
            except Exception:
                pass
            tmp_path = None
    t = threading.Thread(target=broadcast_worker, args=(target, msg_type, message_text, tmp_path), daemon=True)
    t.start()
    return render_template_string("<html><body><h3>Broadcast started</h3><p>The broadcast is being processed. Check logs for progress.</p><p><a href='/admin?secret={{secret}}'>Back to Admin</a></p></body></html>", secret=ADMIN_PANEL_SECRET)

@app.route("/upload/<token>", methods=['GET', 'POST'])
def upload_large_file(token):
    if not bot:
        return "Bot is not initialized", 503
    try:
        data = unsign_upload_token(token, max_age_seconds=3600)
    except SignatureExpired:
        return "<h3>Link expired</h3>", 400
    except BadSignature:
        return "<h3>Invalid link</h3>", 400

    chat_id = data.get("chat_id")
    lang = data.get("lang", "en")

    if request.method == 'GET':
        return render_template_string(HTML_TEMPLATE, max_mb=MAX_WEB_UPLOAD_MB)

    file = request.files.get('file')
    if not file:
        return "No file uploaded", 400

    file_bytes = file.read()
    if len(file_bytes) > MAX_WEB_UPLOAD_MB * 1024 * 1024:
        return f"File too large. Max allowed is {MAX_WEB_UPLOAD_MB}MB.", 400

    def bytes_to_tempfile(b, filename):
        ext = safe_extension_from_filename(filename) or ".tmp"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
        tmp.write(b)
        tmp.flush()
        tmp.close()
        return tmp.name

    tmp_path = bytes_to_tempfile(file_bytes, file.filename or "upload_file")

    with memory_lock:
        if len(PENDING_QUEUE) >= MAX_PENDING_QUEUE:
            try: os.remove(tmp_path)
            except: pass
            return jsonify({"status": "rejected", "message": "Server busy. Try again later."}), 503
        PENDING_QUEUE.append(("web_upload", chat_id, lang, tmp_path))

    return jsonify({"status": "accepted", "message": "Upload accepted. Processing started. Your transcription will be sent to your Telegram chat when ready."})

@app.route("/", methods=["GET", "POST", "HEAD"])
def webhook():
    if not bot:
        return abort(503)
    if request.method in ("GET", "HEAD"):
        now_iso = datetime.utcnow().isoformat() + "Z"
        return jsonify({"status": "ok", "time": now_iso}), 200
    if request.method == "POST":
        ct = request.headers.get("Content-Type", "")
        if ct and ct.startswith("application/json"):
            try:
                update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
                bot.process_new_updates([update])
                return "", 200
            except:
                logging.exception("Error processing incoming webhook update")
                return "", 200
    return abort(403)

@app.route("/set_webhook", methods=["GET", "POST"])
def set_webhook_route():
    if not bot:
        return jsonify({"error": "Bot is not initialized."}), 503
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        return f"Webhook set to {WEBHOOK_URL}", 200
    except Exception as e:
        logging.error(f"Failed to set webhook: {e}")
        return f"Failed to set webhook: {e}", 500

@app.route("/delete_webhook", methods=["GET", "POST"])
def delete_webhook_route():
    if not bot:
        return jsonify({"error": "Bot is not initialized."}), 503
    try:
        bot.delete_webhook()
        return "Webhook deleted.", 200
    except Exception as e:
        logging.error(f"Failed to delete webhook: {e}")
        return f"Failed to delete webhook: {e}", 500

def set_webhook_on_startup():
    if not bot:
        return
    try:
        bot.delete_webhook()
        time.sleep(1)
        if WEBHOOK_URL:
            bot.set_webhook(url=WEBHOOK_URL)
            logging.info(f"Main bot webhook set successfully to {WEBHOOK_URL}")
        else:
            logging.warning("WEBHOOK_BASE is not set. Webhook not set.")
    except Exception as e:
        logging.error(f"Failed to set main bot webhook on startup: {e}")

def set_bot_info_and_startup():
    set_webhook_on_startup()

if __name__ == "__main__":
    if not BOT_TOKEN:
        logging.error("Startup failed: BOT_TOKEN is not configured.")
    else:
        try:
            set_bot_info_and_startup()
            try:
                client.admin.command('ping')
                logging.info("Successfully connected to MongoDB!")
            except Exception as e:
                logging.error("Could not connect to MongoDB: %s", e)
        except:
            logging.exception("Failed during startup")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
