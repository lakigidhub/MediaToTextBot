import os
import logging
import requests
import json
import threading
import time
import io
import tempfile
import re
from flask import Flask, request, abort, jsonify, render_template_string, redirect
from datetime import datetime, timedelta
from pymongo import MongoClient
from collections import Counter
from itsdangerous import URLSafeSerializer, SignatureExpired, BadSignature
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

env = os.environ

TELEGRAM_MAX_BYTES = int(env.get("TELEGRAM_MAX_BYTES", str(20 * 1024 * 1024)))
REQUEST_TIMEOUT_TELEGRAM = int(env.get("REQUEST_TIMEOUT_TELEGRAM", "300"))
REQUEST_TIMEOUT_GEMINI = int(env.get("REQUEST_TIMEOUT_GEMINI", "300"))
MAX_WEB_UPLOAD_MB = int(env.get("MAX_WEB_UPLOAD_MB", "250"))
REQUEST_TIMEOUT_ASSEMBLY = int(env.get("REQUEST_TIMEOUT_ASSEMBLY", "300"))

GEMINI_API_KEYS = [t.strip() for t in env.get("GEMINI_API_KEYS", env.get("GEMINI_API_KEY", "")).split(",") if t.strip()]
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

flask_app = Flask(__name__)

serializer = URLSafeSerializer(SECRET_KEY)

_LANG_RAW = "🇬🇧 English:en,🇸🇦 العربية:ar,🇪🇸 Español:es,🇫🇷 Français:fr,🇷🇺 Русский:ru,🇩🇪 Deutsch:de,🇮🇳 हिन्दी:hi,🇮🇷 فارسی:fa,🇮🇩 Indonesia:id,🇺🇦 Українська:uk,🇦🇿 Azərbaycan:az,🇮🇹 Italiano:it,🇹🇷 Türkçe:tr,🇧🇬 Български:bg,🇷🇸 Srpski:sr,🇵🇰 اردو:ur,🇹🇭 ไทย:th,🇻🇳 Tiếng Việt:vi,🇯🇵 日本語:ja,🇰🇷 한국어:ko,🇨🇳 中文:zh,🇳🇱 Nederlands:nl,🇸🇪 Svenska:sv,🇳🇴 Norsk:no,🇮🇱 עברית:he,🇩🇰 Dansk:da,🇪🇹 አማርኛ:am,🇫🇮 Suomi:fi,🇧🇩 বাংলা:bn,🇰🇪 Kiswahili:sw,🇪🇹 Oromoo:om,🇳🇵 नेपाली:ne,🇵🇱 Polski:pl,🇬🇷 Ελληνικά:el,🇨🇿 Čeština:cs,🇮🇸 Íslenska:is,🇱🇹 Lietuvių:lt,🇱🇻 Latviešu:lv,🇭🇷 Hrvatski:hr,🇷🇸 Bosanski:bs,🇭🇺 Magyar:hu,🇷🇴 Română:ro,🇸🇴 Somali:so,🇲🇾 Melayu:ms,🇺🇿 O'zbekcha:uz,🇵🇭 Tagalog:tl,🇵🇹 Português:pt"
LANG_OPTIONS = [(p.split(":", 1)[0].strip(), p.split(":", 1)[1].strip()) for p in _LANG_RAW.split(",")]
CODE_TO_LABEL = {code: label for label, code in LANG_OPTIONS}
LABEL_TO_CODE = {label: code for label, code in LANG_OPTIONS}

user_transcriptions = {}
action_usage = {}
memory_lock = threading.Lock()
ALLOWED_EXTENSIONS = set(["mp3", "wav", "m4a", "ogg", "webm", "flac", "mp4", "mkv", "avi", "mov", "hevc", "aac", "aiff", "amr", "wma", "opus", "m4v", "ts", "flv", "3gp"])
FFMPEG_BINARY = env.get("FFMPEG_BINARY", "")

def norm_user_id(uid):
    try:
        return str(int(uid))
    except:
        return str(uid)

def check_subscription(user_id):
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = pyro.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def send_subscription_message(chat_id):
    if not REQUIRED_CHANNEL:
        return
    try:
        chat = pyro.get_chat(chat_id)
        if chat.type != 'private':
            return
    except:
        return
    try:
        m = InlineKeyboardMarkup([[InlineKeyboardButton("Click here to join the Group ", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}")]])
        run_coroutine_threadsafe(pyro.send_message(chat_id, "🔒 Access Locked You cannot use this bot until you join the Group.", reply_markup=m))
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

def is_transcoding_like_error(msg):
    if not msg:
        return False
    m = msg.lower()
    checks = ["transcoding failed", "file does not appear to contain audio", "text/html", "html document", "unsupported media type", "could not decode"]
    return any(ch in m for ch in checks)

def build_lang_keyboard(callback_prefix, row_width=3, message_id=None):
    buttons = []
    for label, code in LANG_OPTIONS:
        cb = f"{callback_prefix}|{code}"
        if message_id:
            cb = f"{callback_prefix}|{code}|{message_id}"
        buttons.append([InlineKeyboardButton(label, callback_data=cb)])
    return InlineKeyboardMarkup(buttons)

def build_result_mode_keyboard(prefix="result_mode"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("📄 .txt file", callback_data=f"{prefix}|file"), InlineKeyboardButton("💬 Split messages", callback_data=f"{prefix}|split")]])

def signed_upload_token(chat_id, lang_code):
    return serializer.dumps({"chat_id": chat_id, "lang": lang_code})

def unsign_upload_token(token, max_age_seconds=None):
    try:
        return serializer.loads(token)
    except SignatureExpired:
        raise BadSignature("Token signature failed.")

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

def transcribe_via_selected_service(input_source, lang_code, is_local_file=False):
    if is_local_file:
        try:
            upload_url = upload_file_with_assemblyai(input_source)
            text = transcribe_file_with_assemblyai(upload_url, lang_code)
            if text is None:
                raise RuntimeError("AssemblyAI returned no text")
            return text, "assemblyai_upload"
        except Exception as e:
            raise RuntimeError("AssemblyAI upload and transcription failed: " + str(e))
    else:
        try:
            text = transcribe_file_with_assemblyai(input_source, lang_code)
            if text is None:
                raise RuntimeError("AssemblyAI returned no text")
            return text, "assemblyai_url"
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

def attach_action_buttons(chat_id, message_id, text):
    try:
        include_summarize = len(text) > 1000 if text else False
        buttons = [[InlineKeyboardButton("⭐️Clean transcript", callback_data=f"clean_up|{chat_id}|{message_id}")]]
        if include_summarize:
            buttons.append([InlineKeyboardButton("Get Summarize", callback_data=f"get_key_points|{chat_id}|{message_id}")])
        m = InlineKeyboardMarkup(buttons)
        run_coroutine_threadsafe(pyro.edit_message_reply_markup(chat_id, message_id, reply_markup=m))
    except:
        pass
    try:
        action_usage[f"{chat_id}|{message_id}|clean_up"] = 0
        action_usage[f"{chat_id}|{message_id}|get_key_points"] = 0
    except:
        pass

def process_transcription_result(chat_id, message_id, corrected_text, uid):
    uid_key = str(chat_id)
    user_mode = get_user_send_mode(uid_key)
    if len(corrected_text) > 4000:
        if user_mode == "file":
            f = io.BytesIO(corrected_text.encode("utf-8"))
            f.name = "Transcript.txt"
            fut = pyro.send_document(chat_id, f, reply_to_message_id=message_id)
            run_coroutine_threadsafe(fut)
        else:
            chunks = split_text_into_chunks(corrected_text, limit=4096)
            last = None
            for idx, chunk in enumerate(chunks):
                if idx == 0:
                    fut = pyro.send_message(chat_id, chunk, reply_to_message_id=message_id)
                else:
                    fut = pyro.send_message(chat_id, chunk)
                run_coroutine_threadsafe(fut)
                last = fut
    else:
        fut = pyro.send_message(chat_id, corrected_text or "⚠️ Warning Make sure the voice is clear or speaking in the language you Choosed.", reply_to_message_id=message_id)
        run_coroutine_threadsafe(fut)
    try:
        increment_processing_count(uid, "stt")
    except:
        pass

def process_file_sync(local_file_path, chat_id, reply_to_message_id, uid):
    try:
        lang = get_stt_user_lang(str(uid))
        try:
            text, used_service = transcribe_via_selected_service(local_file_path, lang, True)
        except Exception as e:
            error_msg = str(e)
            if is_transcoding_like_error(error_msg):
                fut = pyro.send_message(chat_id, "⚠️ Transcription error: file is not audible or format is unsupported.", reply_to_message_id=reply_to_message_id)
                run_coroutine_threadsafe(fut)
            else:
                fut = pyro.send_message(chat_id, f"Error during transcription: {error_msg}", reply_to_message_id=reply_to_message_id)
                run_coroutine_threadsafe(fut)
            return
        corrected_text = normalize_text_offline(text)
        process_transcription_result(chat_id, reply_to_message_id, corrected_text, uid)
    finally:
        try:
            os.remove(local_file_path)
        except:
            pass

def worker_start_for_downloaded(local_file_path, chat_id, reply_to_message_id, uid):
    t = threading.Thread(target=process_file_sync, args=(local_file_path, chat_id, reply_to_message_id, uid), daemon=True)
    t.start()

HTML_TEMPLATE = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Upload File</title></head><body><h3>Upload page removed</h3></body></html>"""

ADMIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Admin Panel</title></head><body><h2>Admin Panel</h2><p>Users: {{ users_count }} | Groups: {{ groups_count }}</p></body></html>"""

def broadcast_worker(target_type, msg_type, text, file_path):
    recipients = []
    try:
        if target_type in ("all", "users"):
            for u in users_collection.find({}, {"user_id": 1}):
                try:
                    recipients.append(int(u["user_id"]))
                except:
                    continue
        if target_type in ("all", "groups"):
            for g in groups_collection.find({}, {"_id": 1}):
                try:
                    recipients.append(int(g["_id"]))
                except:
                    continue
    except:
        pass
    for r in recipients:
        try:
            if msg_type == "text":
                run_coroutine_threadsafe(pyro.send_message(r, text, disable_web_page_preview=True))
            elif msg_type == "photo":
                if file_path:
                    run_coroutine_threadsafe(pyro.send_photo(r, file_path, caption=text or None))
                else:
                    run_coroutine_threadsafe(pyro.send_message(r, text or "Photo not provided."))
            elif msg_type == "video":
                if file_path:
                    run_coroutine_threadsafe(pyro.send_video(r, file_path, caption=text or None))
                else:
                    run_coroutine_threadsafe(pyro.send_message(r, text or "Video not provided."))
            elif msg_type == "audio":
                if file_path:
                    run_coroutine_threadsafe(pyro.send_audio(r, file_path, caption=text or None))
                else:
                    run_coroutine_threadsafe(pyro.send_message(r, text or "Audio not provided."))
            elif msg_type == "voice":
                if file_path:
                    run_coroutine_threadsafe(pyro.send_voice(r, file_path, caption=text or None))
                else:
                    run_coroutine_threadsafe(pyro.send_message(r, text or "Voice not provided."))
            elif msg_type == "document":
                if file_path:
                    run_coroutine_threadsafe(pyro.send_document(r, file_path, caption=text or None))
                else:
                    run_coroutine_threadsafe(pyro.send_message(r, text or "Document not provided."))
        except:
            pass
        time.sleep(0.06)
    try:
        if file_path:
            try:
                os.remove(file_path)
            except:
                pass
    except:
        pass

@flask_app.route("/admin", methods=["GET"])
def admin_ui():
    secret = request.args.get("secret")
    if not secret or secret != ADMIN_PANEL_SECRET:
        return abort(403)
    try:
        users_count = users_collection.count_documents({})
    except:
        users_count = 0
    try:
        groups_count = groups_collection.count_documents({})
    except:
        groups_count = 0
    return render_template_string(ADMIN_HTML, users_count=users_count, groups_count=groups_count, secret=ADMIN_PANEL_SECRET, admin_secret=ADMIN_PANEL_SECRET)

@flask_app.route("/admin/save_settings", methods=["POST"])
def admin_save_settings():
    secret = request.form.get("secret")
    if not secret or secret != ADMIN_PANEL_SECRET:
        return abort(403)
    new_secret = request.form.get("admin_secret")
    if new_secret:
        try:
            settings_collection.update_one({"_id": "admin_panel_secret"}, {"$set": {"value": new_secret}}, upsert=True)
            global ADMIN_PANEL_SECRET
            ADMIN_PANEL_SECRET = new_secret
        except:
            pass
    return redirect(f"/admin?secret={ADMIN_PANEL_SECRET}")

@flask_app.route("/admin/send_ads", methods=["POST"])
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
        except:
            try:
                tmp.close()
                os.remove(tmp.name)
            except:
                pass
            tmp_path = None
    t = threading.Thread(target=broadcast_worker, args=(target, msg_type, message_text, tmp_path), daemon=True)
    t.start()
    return render_template_string("<html><body><h3>Broadcast started</h3><p><a href='/admin?secret={{secret}}'>Back to Admin</a></p></body></html>", secret=ADMIN_PANEL_SECRET)

@flask_app.route("/", methods=["GET", "POST", "HEAD"])
def keep_alive():
    return "Bot is alive ✅", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

pyro = Client("bot", bot_token=BOT_TOKEN, workers=int(env.get("PYRO_WORKERS", "8")))

def run_coroutine_threadsafe(coro):
    try:
        return pyro.loop.create_task(coro)
    except:
        try:
            import asyncio
            return asyncio.get_event_loop().create_task(coro)
        except:
            pass

@pyro.on_message(filters.command("start"))
async def start_handler(client, message):
    try:
        update_user_activity(message.from_user.id)
        if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id):
            send_subscription_message(message.chat.id)
            return
        kb = build_lang_keyboard("start_select_lang")
        await message.reply("Choose your file language for transcription using the below buttons:", reply_markup=kb)
    except:
        pass

@pyro.on_callback_query(filters.regex(r"^start_select_lang\|"))
async def start_select_lang_callback(client, call):
    try:
        uid = str(call.from_user.id)
        parts = call.data.split("|")
        if len(parts) >= 2:
            lang_code = parts[1]
            set_stt_user_lang(uid, lang_code)
            lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
            try:
                await call.message.delete()
            except:
                pass
            welcome_text = "👋 Salaam!    \n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free"
            await client.send_message(call.message.chat.id, welcome_text)
            await call.answer(f"✅ Language set to {lang_label}")
    except:
        try:
            await call.answer("❌ Error setting language, try again.", show_alert=True)
        except:
            pass

@pyro.on_message(filters.command("help"))
async def handle_help(client, message):
    try:
        update_user_activity(message.from_user.id)
        if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id):
            send_subscription_message(message.chat.id)
            return
        text = "Commands supported:\n/start - Show welcome message\n/lang  - Change language\n/mode  - Change result delivery mode\n/help  - This help message\n\nSend a voice/audio/video (up to 20MB for Telegram) and I will transcribe it."
        await message.reply(text)
    except:
        pass

@pyro.on_message(filters.command("lang"))
async def handle_lang(client, message):
    try:
        if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id):
            send_subscription_message(message.chat.id)
            return
        kb = build_lang_keyboard("stt_lang")
        await message.reply("Choose your file language for transcription using the below buttons:", reply_markup=kb)
    except:
        pass

@pyro.on_message(filters.command("mode"))
async def handle_mode(client, message):
    try:
        if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id):
            send_subscription_message(message.chat.id)
            return
        current_mode = get_user_send_mode(str(message.from_user.id))
        mode_text = "📄 .txt file" if current_mode == "file" else "💬 Split messages"
        await message.reply(f"Result delivery mode: {mode_text}. Change it below:", reply_markup=build_result_mode_keyboard())
    except:
        pass

@pyro.on_message(filters.command("admin"))
async def handle_admin(client, message):
    try:
        if message.from_user.id not in ADMIN_USER_IDS:
            await message.reply("Access Denied: Only admins can use this command.")
            return
        total_users = users_collection.count_documents({})
        total_groups = groups_collection.count_documents({})
        total_stt_conversions = sum(user.get("stt_conversion_count", 0) for user in users_collection.find({}))
        seven_days_ago = datetime.now() - timedelta(days=7)
        active_users_7d = users_collection.count_documents({"last_active": {"$gte": seven_days_ago}})
        report_text = f"<b>📊 Bot Admin Panel</b>\n\n👤 Total Users: <b>{total_users}</b>\n👥 Total Groups: <b>{total_groups}</b>\n🗣️ Total Transcriptions: <b>{total_stt_conversions:,}</b>\n🟢 Active Users (Last 7 Days): <b>{active_users_7d}</b>\n\n"
        link = f"/admin?secret={ADMIN_PANEL_SECRET}"
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open Admin Panel", url=link)]])
        await message.reply(report_text, reply_markup=markup)
    except:
        try:
            await message.reply("An error occurred while fetching admin stats.")
        except:
            pass

@pyro.on_callback_query(filters.regex(r"^stt_lang\|"))
async def on_stt_language_select(client, call):
    try:
        uid = str(call.from_user.id)
        parts = call.data.split("|")
        if len(parts) >= 2:
            lang_code = parts[1]
            set_stt_user_lang(uid, lang_code)
            lang_label = CODE_TO_LABEL.get(lang_code, lang_code)
            try:
                await call.message.delete()
            except:
                pass
            await call.answer(f"✅ Language set: {lang_label}")
    except:
        try:
            await call.answer("❌ Error setting language, try again.", show_alert=True)
        except:
            pass

@pyro.on_callback_query(filters.regex(r"^result_mode\|"))
async def on_result_mode_select(client, call):
    try:
        uid = str(call.from_user.id)
        _, mode = call.data.split("|", 1)
        set_user_send_mode(uid, mode)
        mode_text = "📄 .txt file" if mode == "file" else "💬 Split messages"
        try:
            await call.message.delete()
        except:
            pass
        await call.answer(f"✅ Result mode set: {mode_text}")
    except:
        try:
            await call.answer("❌ Error setting result mode, try again.", show_alert=True)
        except:
            pass

@pyro.on_message(filters.new_chat_members)
async def handle_new_chat_members(client, message):
    try:
        if message.new_chat_members[0].id == (await client.get_me()).id:
            group_data = {'_id': str(message.chat.id), 'title': message.chat.title, 'type': message.chat.type, 'added_date': datetime.now()}
            groups_collection.update_one({'_id': group_data['_id']}, {'$set': group_data}, upsert=True)
            await client.send_message(message.chat.id, "Thanks for adding me! I'm ready to transcribe your media files.")
    except:
        pass

@pyro.on_message(filters.left_chat_member)
async def handle_left_chat_member(client, message):
    try:
        if message.left_chat_member.id == (await client.get_me()).id:
            groups_collection.delete_one({'_id': str(message.chat.id)})
    except:
        pass

@pyro.on_message(filters.voice | filters.audio | filters.video | filters.document)
async def handle_media_types(client, message):
    try:
        if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id):
            send_subscription_message(message.chat.id)
            return
        update_user_activity(message.from_user.id)
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
            if (mime and ("audio" in mime or "video" in mime)) or ext in ALLOWED_EXTENSIONS:
                file_id = message.document.file_id
                file_size = message.document.file_size
            else:
                await message.reply("Sorry, I can only transcribe audio or video files.")
                return
        if file_size and file_size > TELEGRAM_MAX_BYTES:
            lang = get_stt_user_lang(str(message.from_user.id))
            await message.reply(f'Telegram API doesn’t allow me to download your file if it\'s larger than {TELEGRAM_MAX_BYTES//(1024*1024)}MB.')
            return
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix="." + safe_extension_from_filename(filename) if "." in filename else ".tmp")
        tmp.close()
        try:
            downloaded_path = await message.download(file_name=tmp.name)
        except:
            try:
                os.remove(tmp.name)
            except:
                pass
            await message.reply("Failed to download file from Telegram.")
            return
        await message.reply("🔄 Processing...")
        worker_start_for_downloaded(downloaded_path, message.chat.id, message.message_id, message.from_user.id)
    except:
        pass

@pyro.on_message(filters.text)
async def handle_text_messages(client, message):
    try:
        if message.chat.type == 'private' and str(message.from_user.id) not in ADMIN_USER_IDS and not check_subscription(message.from_user.id):
            send_subscription_message(message.chat.id)
            return
        await message.reply("For Text to Audio Use: @TextToSpeechBBot")
    except:
        pass

@pyro.on_callback_query(filters.regex(r"^get_key_points\|") | filters.regex(r"^get_key_points\Z"))
async def get_key_points_callback(client, call):
    try:
        parts = call.data.split("|")
        if len(parts) == 3:
            _, chat_id_part, msg_id_part = parts
        elif len(parts) == 2:
            _, msg_id_part = parts
            chat_id_part = str(call.message.chat.id)
        else:
            await call.answer("Invalid request", show_alert=True)
            return
        try:
            chat_id_val = int(chat_id_part)
            msg_id = int(msg_id_part)
        except:
            await call.answer("Invalid message id", show_alert=True)
            return
        usage_key = f"{chat_id_val}|{msg_id}|get_key_points"
        usage = action_usage.get(usage_key, 0)
        if usage >= 1:
            await call.answer("Get Summarize unavailable (maybe expired)", show_alert=True)
            return
        action_usage[usage_key] = usage + 1
        uid_key = str(chat_id_val)
        stored = user_transcriptions.get(uid_key, {}).get(msg_id)
        if not stored:
            await call.answer("Get Summarize unavailable (maybe expired)", show_alert=True)
            return
        await call.answer("Generating...")
        status_msg = await client.send_message(call.message.chat.id, "🔄 Processing...", reply_to_message_id=call.message.message_id)
        stop = {"stop": False}
        try:
            lang = get_stt_user_lang(str(chat_id_val)) or "en"
            instruction = f"What is this report and what is it about? Please summarize them for me into (lang={lang}) without adding any introductions, notes, or extra phrases."
            try:
                summary = ask_gemini(stored, instruction)
            except:
                summary = extract_key_points_offline(stored, max_points=6)
        except:
            summary = ""
        if not summary:
            try:
                await client.edit_message_text("No Summary returned.", chat_id=call.message.chat.id, message_id=status_msg.message_id)
            except:
                pass
        else:
            try:
                await client.edit_message_text(f"{summary}", chat_id=call.message.chat.id, message_id=status_msg.message_id)
            except:
                pass
    except:
        pass

@pyro.on_callback_query(filters.regex(r"^clean_up\|") | filters.regex(r"^clean_up\Z"))
async def clean_up_callback(client, call):
    try:
        parts = call.data.split("|")
        if len(parts) == 3:
            _, chat_id_part, msg_id_part = parts
        elif len(parts) == 2:
            _, msg_id_part = parts
            chat_id_part = str(call.message.chat.id)
        else:
            await call.answer("Invalid request", show_alert=True)
            return
        try:
            chat_id_val = int(chat_id_part)
            msg_id = int(msg_id_part)
        except:
            await call.answer("Invalid message id", show_alert=True)
            return
        usage_key = f"{chat_id_val}|{msg_id}|clean_up"
        usage = action_usage.get(usage_key, 0)
        if usage >= 1:
            await call.answer("Clean up unavailable (maybe expired)", show_alert=True)
            return
        action_usage[usage_key] = usage + 1
        uid_key = str(chat_id_val)
        stored = user_transcriptions.get(uid_key, {}).get(msg_id)
        if not stored:
            await call.answer("Clean up unavailable (maybe expired)", show_alert=True)
            return
        await call.answer("Cleaning up...")
        status_msg = await client.send_message(call.message.chat.id, "🔄 Processing...", reply_to_message_id=call.message.message_id)
        try:
            lang = get_stt_user_lang(str(chat_id_val)) or "en"
            instruction = f"Clean and normalize this transcription (lang={lang}). Remove ASR artifacts like [inaudible], repeated words, filler noises, timestamps, and incorrect punctuation. Produce a clean, well-punctuated, readable text in the same language. Do not add introductions or explanations."
            try:
                cleaned = ask_gemini(stored, instruction)
            except:
                cleaned = normalize_text_offline(stored)
        except:
            cleaned = ""
        if not cleaned:
            try:
                await client.edit_message_text("No cleaned text returned.", chat_id=call.message.chat.id, message_id=status_msg.message_id)
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
                    await client.delete_messages(call.message.chat.id, status_msg.message_id)
                except:
                    pass
                sent = await client.send_document(call.message.chat.id, f, reply_to_message_id=call.message.message_id)
                try:
                    user_transcriptions.setdefault(uid_key, {})[sent.message_id] = cleaned
                except:
                    pass
            else:
                try:
                    await client.delete_messages(call.message.chat.id, status_msg.message_id)
                except:
                    pass
                chunks = split_text_into_chunks(cleaned, limit=4096)
                last_sent = None
                for idx, chunk in enumerate(chunks):
                    if idx == 0:
                        last_sent = await client.send_message(call.message.chat.id, chunk, reply_to_message_id=call.message.message_id)
                    else:
                        last_sent = await client.send_message(call.message.chat.id, chunk)
                try:
                    user_transcriptions.setdefault(uid_key, {})[last_sent.message_id] = cleaned
                except:
                    pass
        else:
            try:
                await client.edit_message_text(f"{cleaned}", chat_id=call.message.chat.id, message_id=status_msg.message_id)
                uid_key = str(chat_id_val)
                user_transcriptions.setdefault(uid_key, {})[status_msg.message_id] = cleaned
            except:
                pass
    except:
        pass

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
            last_exception = e
            continue
    raise RuntimeError(f"All Gemini API keys failed. Last error: {str(last_exception) if last_exception else 'No keys were available.'}")

if __name__ == "__main__":
    try:
        t = threading.Thread(target=run_flask, daemon=True)
        t.start()
    except:
        pass
    try:
        try:
            client.admin.command('ping')
            logging.info("Successfully connected to MongoDB!")
        except:
            logging.warning("Could not connect to MongoDB")
        pyro.run()
    except:
        logging.exception("Failed during startup")
