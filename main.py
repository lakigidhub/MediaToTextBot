import logging
import requests
import telebot
import json
import threading
import time
import io
import tempfile
from flask import Flask, request, abort, jsonify
from datetime import datetime
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
from dotenv import dotenv_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

_config = dotenv_values(".env")
def cfg(k, d=""):
    return _config.get(k, d)

TELEGRAM_MAX_BYTES = int(cfg("TELEGRAM_MAX_BYTES", str(20 * 1024 * 1024)))
REQUEST_TIMEOUT_TELEGRAM = int(cfg("REQUEST_TIMEOUT_TELEGRAM", "300"))
MAX_CONCURRENT_TRANSCRIPTS = int(cfg("MAX_CONCURRENT_TRANSCRIPTS", "2"))
MAX_PENDING_QUEUE = int(cfg("MAX_PENDING_QUEUE", "2"))
ASSEMBLYAI_API_KEY = cfg("ASSEMBLYAI_API_KEY", "").strip()
WEBHOOK_BASE = cfg("WEBHOOK_BASE", "")
MONGO_URI = cfg("MONGO_URI", "")
DB_NAME = cfg("DB_NAME", "telegram_bot_db")
REQUIRED_CHANNEL = cfg("REQUIRED_CHANNEL", "")
BOT_TOKENS = [t.strip() for t in cfg("BOT_TOKENS", "").split(",") if t.strip()]

client = MongoClient(MONGO_URI) if MONGO_URI else MongoClient()
db = client[DB_NAME]
users_collection = db["users"]
groups_collection = db["groups"]

app = Flask(__name__)
bots = [telebot.TeleBot(token, threaded=True, parse_mode="HTML") for token in BOT_TOKENS]

_LANG_RAW = "🇬🇧 English:en,🇸🇦 العربية:ar,🇪🇸 Español:es,🇫🇷 Français:fr,🇷🇺 Русский:ru,🇩🇪 Deutsch:de,🇮🇳 हिन्दी:hi,🇮🇷 فارسی:fa,🇮🇩 Indonesia:id,🇺🇦 Українська:uk,🇦🇿 Azərbaycan:az,🇮🇹 Italiano:it,🇹🇷 Türkçe:tr,🇧🇬 Български:bg,🇷🇸 Srpski:sr,🇵🇰 اردو:ur,🇹🇭 ไทย:th,🇻🇳 Tiếng Việt:vi,🇯🇵 日本語:ja,🇰🇷 한국어:ko,🇨🇳 中文:zh,🇳🇱 Nederlands:nl,🇸🇪 Svenska:sv,🇳🇴 Norsk:no,🇮🇱 עברית:he,🇩🇰 Dansk:da,🇪🇹 አማርኛ:am,🇫🇮 Suomi:fi,🇧🇩 বাংলা:bn,🇰🇪 Kiswahili:sw,🇪🇹 Oromoo:om,🇳🇵 नेपाली:ne,🇵🇱 Polski:pl,🇬🇷 Ελληνικά:el,🇨🇿 Čeština:cs,🇮🇸 Íslenska:is,🇱🇹 Lietuvių:lt,🇱🇻 Latviešu:lv,🇭🇷 Hrvatski:hr,🇷🇸 Bosanski:bs,🇭🇺 Magyar:hu,🇷🇴 Română:ro,🇸🇴 Somali:so,🇲🇾 Melayu:ms,🇺🇿 O'zbekcha:uz,🇵🇭 Tagalog:tl,🇵🇹 Português:pt"
LANG_OPTIONS = [(p.split(":", 1)[0].strip(), p.split(":", 1)[1].strip()) for p in _LANG_RAW.split(",")]
CODE_TO_LABEL = {code: label for label, code in LANG_OPTIONS}
LABEL_TO_CODE = {label: code for label, code in LANG_OPTIONS}

user_transcriptions = {}
in_memory_data = {"pending_media": {}}
action_usage = {}
memory_lock = threading.Lock()
PENDING_QUEUE = []
queue_lock = threading.Lock()
transcript_semaphore = threading.Semaphore(MAX_CONCURRENT_TRANSCRIPTS)
ALLOWED_EXTENSIONS = set(
    ["mp3", "wav", "m4a", "ogg", "webm", "flac", "mp4", "mkv", "avi", "mov", "aac", "aiff", "amr", "wma", "opus", "m4v", "ts", "flv", "3gp"]
)

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
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

def send_subscription_message(chat_id, bot_obj):
    if not REQUIRED_CHANNEL:
        return
    try:
        chat = bot_obj.get_chat(chat_id)
        if chat.type != "private":
            return
    except:
        return
    try:
        m = InlineKeyboardMarkup()
        m.add(InlineKeyboardButton("Click here to join the Group", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"))
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

def save_pending_media(user_id, media_type, data):
    with memory_lock:
        in_memory_data["pending_media"][user_id] = {"media_type": media_type, "data": data, "saved_at": datetime.now()}

def pop_pending_media(user_id):
    with memory_lock:
        return in_memory_data["pending_media"].pop(user_id, None)

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
        m.add(*buttons[i:i+row_width])
    return m

def build_result_mode_keyboard(prefix="result_mode"):
    m = InlineKeyboardMarkup(row_width=2)
    m.add(InlineKeyboardButton("📄 .txt file", callback_data=f"{prefix}|file"), InlineKeyboardButton("💬 Split messages", callback_data=f"{prefix}|split"))
    return m

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
    return " ".join(text.split()).strip() if text else text

def extract_key_points_offline(text, max_points=6):
    if not text:
        return ""
    sentences = [s.strip() for s in __import__("re").split(r"(?<=[\.\!\?])\s+", text) if s.strip()]
    if not sentences:
        return ""
    words = [w for w in __import__("re").findall(r"\w+", text.lower()) if len(w) > 3]
    if not words:
        return "\n".join(f"- {s}" for s in sentences[:max_points])
    from collections import Counter
    freq = Counter(words)
    sentence_scores = [(sum(freq.get(w, 0) for w in __import__("re").findall(r"\w+", s.lower())), s) for s in sentences]
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
    return type("T", (), {"file_path": file_path})(), f"https://api.telegram.org/file/bot{bot_token}/{file_path}"

def assemblyai_upload_file(path):
    if not ASSEMBLYAI_API_KEY:
        raise RuntimeError("AssemblyAI API key not set")
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    with open(path, "rb") as f:
        resp = requests.post("https://api.assemblyai.com/v2/upload", headers=headers, data=f, timeout=REQUEST_TIMEOUT_TELEGRAM)
    resp.raise_for_status()
    return resp.json()["upload_url"]

def assemblyai_create_transcript(upload_url, language_code=None):
    headers = {"authorization": ASSEMBLYAI_API_KEY, "content-type": "application/json"}
    payload = {"audio_url": upload_url}
    if language_code:
        payload["language_code"] = language_code
    resp = requests.post("https://api.assemblyai.com/v2/transcript", headers=headers, json=payload, timeout=REQUEST_TIMEOUT_TELEGRAM)
    resp.raise_for_status()
    return resp.json()["id"]

def assemblyai_poll_transcript(transcript_id, timeout=300):
    headers = {"authorization": ASSEMBLYAI_API_KEY}
    url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"
    start = time.time()
    while True:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_TELEGRAM)
        resp.raise_for_status()
        data = resp.json()
        status = data.get("status")
        if status == "completed":
            return data.get("text", "")
        if status == "failed":
            raise RuntimeError("AssemblyAI transcription failed: " + str(data.get("error", "")))
        if time.time() - start > timeout:
            raise RuntimeError("AssemblyAI transcription timed out")
        time.sleep(2)

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
        action_usage[f"{chat_id}|{message_id}|clean_up"] = 0
        action_usage[f"{chat_id}|{message_id}|get_key_points"] = 0
    except:
        pass

def process_media_file(message, bot_obj, bot_token, bot_index, file_id, file_size, filename):
    uid = str(message.from_user.id)
    chatid = str(message.chat.id)
    lang = get_stt_user_lang(uid)
    processing_msg = bot_obj.send_message(message.chat.id, "🔄 Processing...", reply_to_message_id=message.message_id)
    processing_msg_id = processing_msg.message_id
    stop = {"stop": False}
    animation_thread = threading.Thread(target=animate_processing_message, args=(bot_obj, message.chat.id, processing_msg_id, lambda: stop["stop"]))
    animation_thread.start()
    tmpf_name = None
    try:
        tf, file_url = telegram_file_info_and_url(bot_token, file_id)
        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix="." + (safe_extension_from_filename(filename) or "tmp"))
        tmpf_name = tmpf.name
        with requests.get(file_url, stream=True, timeout=REQUEST_TIMEOUT_TELEGRAM) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    tmpf.write(chunk)
        tmpf.flush()
        tmpf.close()
        try:
            upload_url = assemblyai_upload_file(tmpf_name)
            transcript_id = assemblyai_create_transcript(upload_url, language_code=lang)
            text = assemblyai_poll_transcript(transcript_id, timeout=REQUEST_TIMEOUT_TELEGRAM)
        except Exception as e:
            error_msg = str(e)
            logging.exception("Error during AssemblyAI transcription")
            if is_transcoding_like_error(error_msg):
                bot_obj.send_message(message.chat.id, "⚠️ Transcription error: file is not audible. Please send a different file.", reply_to_message_id=message.message_id)
            else:
                bot_obj.send_message(message.chat.id, f"Error during transcription: {error_msg}", reply_to_message_id=message.message_id)
            return
        corrected_text = normalize_text_offline(text)
        uid_key = str(message.chat.id)
        user_mode = get_user_send_mode(uid_key)
        if len(corrected_text) > 4000:
            if user_mode == "file":
                f = io.BytesIO(corrected_text.encode("utf-8"))
                f.name = "Transcript.txt"
                sent = bot_obj.send_document(message.chat.id, f, reply_to_message_id=message.message_id)
                try:
                    attach_action_buttons(bot_obj, message.chat.id, sent.message_id, corrected_text)
                except:
                    pass
                try:
                    user_transcriptions.setdefault(uid_key, {})[sent.message_id] = corrected_text
                    threading.Thread(target=delete_transcription_later, args=(uid_key, sent.message_id), daemon=True).start()
                except:
                    pass
            else:
                chunks = []
                text_remaining = corrected_text
                limit = 4096
                while text_remaining:
                    chunk = text_remaining[:limit]
                    last_space = chunk.rfind(" ")
                    if last_space > 0 and len(text_remaining) > limit:
                        chunk = chunk[:last_space]
                    chunks.append(chunk.strip())
                    text_remaining = text_remaining[len(chunk):].lstrip()
                last_sent = None
                for idx, chunk in enumerate(chunks):
                    if idx == 0:
                        last_sent = bot_obj.send_message(message.chat.id, chunk, reply_to_message_id=message.message_id)
                    else:
                        last_sent = bot_obj.send_message(message.chat.id, chunk)
                try:
                    attach_action_buttons(bot_obj, message.chat.id, last_sent.message_id, corrected_text)
                except:
                    pass
                try:
                    user_transcriptions.setdefault(uid_key, {})[last_sent.message_id] = corrected_text
                    threading.Thread(target=delete_transcription_later, args=(uid_key, last_sent.message_id), daemon=True).start()
                except:
                    pass
        else:
            sent_msg = bot_obj.send_message(message.chat.id, corrected_text or "⚠️ Warning Make sure the voice is clear or speaking in the language you Choosed.", reply_to_message_id=message.message_id)
            try:
                attach_action_buttons(bot_obj, message.chat.id, sent_msg.message_id, corrected_text)
            except:
                pass
            try:
                user_transcriptions.setdefault(uid_key, {})[sent_msg.message_id] = corrected_text
                threading.Thread(target=delete_transcription_later, args=(uid_key, sent_msg.message_id), daemon=True).start()
            except:
                pass
        increment_processing_count(uid, "stt")
    finally:
        if tmpf_name:
            try:
                import os
                os.remove(tmpf_name)
            except:
                pass
        stop["stop"] = True
        animation_thread.join()
        try:
            bot_obj.delete_message(message.chat.id, processing_msg_id)
        except:
            pass

def worker_thread():
    while True:
        item = None
        with queue_lock:
            if PENDING_QUEUE:
                item = PENDING_QUEUE.pop(0)
        if not item:
            time.sleep(0.5)
            continue
        transcript_semaphore.acquire()
        try:
            message, bot_obj, bot_token, bot_index, file_id, file_size, filename = item
            logging.info(f"Starting processing for user {message.from_user.id} (Chat {message.chat.id}) from queue. Current queue size: {len(PENDING_QUEUE)}")
            process_media_file(message, bot_obj, bot_token, bot_index, file_id, file_size, filename)
        except:
            logging.exception("Error in worker thread")
        finally:
            transcript_semaphore.release()

def start_worker_threads():
    for i in range(MAX_CONCURRENT_TRANSCRIPTS):
        t = threading.Thread(target=worker_thread, daemon=True)
        t.start()

start_worker_threads()

def handle_media_common(message, bot_obj, bot_token, bot_index=0):
    update_user_activity(message.from_user.id)
    if message.chat.type == "private" and not check_subscription(message.from_user.id, bot_obj):
        send_subscription_message(message.chat.id, bot_obj)
        return
    file_id = file_size = filename = None
    if getattr(message, "voice", None):
        file_id = message.voice.file_id
        file_size = message.voice.file_size
        filename = "voice.ogg"
    elif getattr(message, "audio", None):
        file_id = message.audio.file_id
        file_size = message.audio.file_size
        filename = getattr(message.audio, "file_name", "audio")
    elif getattr(message, "video", None):
        file_id = message.video.file_id
        file_size = message.video.file_size
        filename = getattr(message.video, "file_name", "video.mp4")
    elif getattr(message, "document", None):
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
        max_display_mb = TELEGRAM_MAX_BYTES // (1024 * 1024)
        bot_obj.send_message(message.chat.id, f"Just Send me a file less than {max_display_mb}MB 😎", reply_to_message_id=message.message_id)
        return
    with queue_lock:
        if len(PENDING_QUEUE) >= MAX_PENDING_QUEUE:
            bot_obj.send_message(message.chat.id, "⚠️ Server busy. Try again later.", reply_to_message_id=message.message_id)
            return
        PENDING_QUEUE.append((message, bot_obj, bot_token, bot_index, file_id, file_size, filename))

def ask_gemini(text, instruction, timeout=REQUEST_TIMEOUT_TELEGRAM):
    GEMINI_API_KEY = cfg("GEMINI_API_KEY", "")
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": instruction}, {"text": text}]}]}
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    result = resp.json()
    if "candidates" in result and isinstance(result["candidates"], list) and len(result["candidates"]) > 0:
        try:
            return result["candidates"][0]["content"]["parts"][0]["text"]
        except:
            return json.dumps(result["candidates"][0])
    return json.dumps(result)

def register_handlers(bot_obj, bot_token, bot_index):
    @bot_obj.message_handler(commands=["start"])
    def start_handler(message):
        try:
            update_user_activity(message.from_user.id)
            if message.chat.type == "private" and not check_subscription(message.from_user.id, bot_obj):
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

    @bot_obj.message_handler(commands=["help"])
    def handle_help(message):
        try:
            update_user_activity(message.from_user.id)
            if message.chat.type == "private" and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            text = "Commands supported:\n/start - Show welcome message\n/lang  - Change language\n/mode  - Change result delivery mode\n/help  - This help message\n\nSend a voice/audio/video (up to 20MB) and I will transcribe it Need help? Contact: @lakigithub"
            bot_obj.send_message(message.chat.id, text)
        except:
            logging.exception("Error in handle_help")

    @bot_obj.message_handler(commands=["lang"])
    def handle_lang(message):
        try:
            if message.chat.type == "private" and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            kb = build_lang_keyboard("stt_lang")
            bot_obj.send_message(message.chat.id, "Choose your file language for transcription using the below buttons:", reply_markup=kb)
        except:
            logging.exception("Error in handle_lang")

    @bot_obj.message_handler(commands=["mode"])
    def handle_mode(message):
        try:
            if message.chat.type == "private" and not check_subscription(message.from_user.id, bot_obj):
                send_subscription_message(message.chat.id, bot_obj)
                return
            current_mode = get_user_send_mode(str(message.from_user.id))
            mode_text = "📄 .txt file" if current_mode == "file" else "💬 Split messages"
            bot_obj.send_message(message.chat.id, f"Result delivery mode: {mode_text}. Change it below:", reply_markup=build_result_mode_keyboard())
        except:
            logging.exception("Error in handle_mode")

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

    @bot_obj.message_handler(content_types=["new_chat_members"])
    def handle_new_chat_members(message):
        try:
            if message.new_chat_members[0].id == bot_obj.get_me().id:
                group_data = {"_id": str(message.chat.id), "title": message.chat.title, "type": message.chat.type, "added_date": datetime.now()}
                groups_collection.update_one({"_id": group_data["_id"]}, {"$set": group_data}, upsert=True)
                bot_obj.send_message(message.chat.id, "Thanks for adding me! I'm ready to transcribe your media files.")
        except:
            logging.exception("Error in handle_new_chat_members")

    @bot_obj.message_handler(content_types=["left_chat_member"])
    def handle_left_chat_member(message):
        try:
            if message.left_chat_member.id == bot_obj.get_me().id:
                groups_collection.delete_one({"_id": str(message.chat.id)})
        except:
            logging.exception("Error in handle_left_chat_member")

    @bot_obj.message_handler(content_types=["voice", "audio", "video", "document"])
    def handle_media_types(message):
        try:
            handle_media_common(message, bot_obj, bot_token, bot_index)
        except:
            logging.exception("Error in handle_media_types")

    @bot_obj.message_handler(content_types=["text"])
    def handle_text_messages(message):
        try:
            if message.chat.type == "private" and not check_subscription(message.from_user.id, bot_obj):
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
                    chunks = []
                    text_remaining = cleaned
                    limit = 4096
                    while text_remaining:
                        chunk = text_remaining[:limit]
                        last_space = chunk.rfind(" ")
                        if last_space > 0 and len(text_remaining) > limit:
                            chunk = chunk[:last_space]
                        chunks.append(chunk.strip())
                        text_remaining = text_remaining[len(chunk):].lstrip()
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

for idx, bot_obj in enumerate(bots):
    register_handlers(bot_obj, BOT_TOKENS[idx], idx)

@app.route("/", methods=["GET", "POST", "HEAD"])
def webhook_root():
    if request.method in ("GET", "HEAD"):
        bot_index = request.args.get("bot_index")
        try:
            bot_index_val = int(bot_index) if bot_index is not None else 0
        except:
            bot_index_val = 0
        now_iso = datetime.utcnow().isoformat() + "Z"
        return jsonify({"status": "ok", "time": now_iso, "bot_index": bot_index_val}), 200
    if request.method == "POST":
        content_type = request.headers.get("Content-Type", "")
        if content_type and content_type.startswith("application/json"):
            raw = request.get_data().decode("utf-8")
            try:
                payload = json.loads(raw)
            except:
                payload = None
            bot_index = request.args.get("bot_index")
            if not bot_index and isinstance(payload, dict):
                bot_index = payload.get("bot_index")
            header_idx = request.headers.get("X-Bot-Index")
            if header_idx:
                bot_index = header_idx
            try:
                bot_index_val = int(bot_index) if bot_index is not None else 0
            except:
                bot_index_val = 0
            if bot_index_val < 0 or bot_index_val >= len(bots):
                return abort(404)
            try:
                update = telebot.types.Update.de_json(payload)
                bots[bot_index_val].process_new_updates([update])
            except:
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
            client.admin.command("ping")
            logging.info("Successfully connected to MongoDB!")
        except Exception as e:
            logging.error("Could not connect to MongoDB: %s", e)
    except:
        logging.exception("Failed during startup")
    port = int(cfg("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
