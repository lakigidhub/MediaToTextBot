import os,logging,requests,telebot,json,threading,time,io,subprocess,tempfile,glob,re,wave,math
from flask import Flask,request,abort,jsonify
from datetime import datetime
from telebot.types import InlineKeyboardMarkup,InlineKeyboardButton
from pymongo import MongoClient
# import speech_recognition as sr # Waa laga saaray
from concurrent.futures import ThreadPoolExecutor
from collections import Counter,deque

logging.basicConfig(level=logging.INFO,format='%(asctime)s - %(levelname)s - %(message)s')

env=os.environ
# CHUNK_DURATION_SEC=int(env.get("CHUNK_DURATION_SEC","40")) # Laga saaray
# CHUNK_OVERLAP_SEC=float(env.get("CHUNK_OVERLAP_SEC","1.0")) # Laga saaray
TRANSCRIBE_MAX_WORKERS=int(env.get("TRANSCRIBE_MAX_WORKERS","4"))
RECOGNITION_MAX_RETRIES=int(env.get("RECOGNITION_MAX_RETRIES","4"))
RECOGNITION_RETRY_WAIT=int(env.get("RECOGNITION_RETRY_WAIT","3"))
AUDIO_SAMPLE_RATE=int(env.get("AUDIO_SAMPLE_RATE","8000"))
AUDIO_CHANNELS=int(env.get("AUDIO_CHANNELS","1"))
PREPEND_SILENCE_SEC=int(env.get("PREPEND_SILENCE_SEC","20"))
AMBIENT_CALIB_SEC=float(env.get("AMBIENT_CALIB_SEC","0.5"))
TELEGRAM_MAX_BYTES=int(env.get("TELEGRAM_MAX_BYTES",str(20*1024*1024)))
REQUEST_TIMEOUT_TELEGRAM=int(env.get("REQUEST_TIMEOUT_TELEGRAM","300"))
REQUEST_TIMEOUT_GEMINI=int(env.get("REQUEST_TIMEOUT_GEMINI","300"))
MAX_CONCURRENT_TRANSCRIPTS=int(env.get("MAX_CONCURRENT_TRANSCRIPTS","2"))
MAX_PENDING_QUEUE=int(env.get("MAX_PENDING_QUEUE","2"))
GEMINI_API_KEY=env.get("GEMINI_API_KEY","")
WEBHOOK_BASE=env.get("WEBHOOK_BASE","")
SECRET_KEY=env.get("SECRET_KEY","testkey123")
MONGO_URI=env.get("MONGO_URI","")
DB_NAME=env.get("DB_NAME","telegram_bot_db")
REQUIRED_CHANNEL=env.get("REQUIRED_CHANNEL","")
BOT_TOKENS=[t.strip() for t in env.get("BOT_TOKENS","").split(",") if t.strip()]

# AssemblyAI Configuration
ASSEMBLYAI_API_KEY=env.get("ASSEMBLYAI_API_KEY","26293b7d8dbf43d883ce8a43d3c06f63") # Your AssemblyAI token
ASSEMBLYAI_BASE_URL="https://api.assemblyai.com/v2"

client=MongoClient(MONGO_URI) if MONGO_URI else MongoClient()
db=client[DB_NAME]
users_collection=db["users"]
groups_collection=db["groups"]

app=Flask(__name__)
bots=[telebot.TeleBot(token,threaded=True,parse_mode='HTML') for token in BOT_TOKENS]

_LANG_RAW="🇬🇧 English:en,🇸🇦 العربية:ar,🇪🇸 Español:es,🇫🇷 Français:fr,🇷🇺 Русский:ru,🇩🇪 Deutsch:de,🇮🇳 हिन्दी:hi,🇮🇷 فارسی:fa,🇮🇩 Indonesia:id,🇺🇦 Українська:uk,🇦🇿 Azərbaycan:az,🇮🇹 Italiano:it,🇹🇷 Türkçe:tr,🇧🇬 Български:bg,🇷🇸 Srpski:sr,🇵🇰 اردو:ur,🇹🇭 ไทย:th,🇻🇳 Tiếng Việt:vi,🇯🇵 日本語:ja,🇰🇷 한국어:ko,🇨🇳 中文:zh,🇳🇱 Nederlands:nl,🇸🇪 Svenska:sv,🇳🇴 Norsk:no,🇮🇱 עברית:he,🇩🇰 Dansk:da,🇪🇹 አማርኛ:am,🇫🇮 Suomi:fi,🇧🇩 বাংলা:bn,🇰🇪 Kiswahili:sw,🇪🇹 Oromoo:om,🇳🇵 नेपाली:ne,🇵🇱 Polski:pl,🇬🇷 Ελληνικά:el,🇨🇿 Čeština:cs,🇮🇸 Íslenska:is,🇱🇹 Lietuvių:lt,🇱🇻 Latviešu:lv,🇭🇷 Hrvatski:hr,🇷🇸 Bosanski:bs,🇭🇺 Magyar:hu,🇷🇴 Română:ro,🇸🇴 Somali:so,🇲🇾 Melayu:ms,🇺🇿 O'zbekcha:uz,🇵🇭 Tagalog:tl,🇵🇹 Português:pt"
LANG_OPTIONS=[(p.split(":",1)[0].strip(),p.split(":",1)[1].strip()) for p in _LANG_RAW.split(",")]
CODE_TO_LABEL={code:label for label,code in LANG_OPTIONS}
LABEL_TO_CODE={label:code for label,code in LANG_OPTIONS}

user_transcriptions={}
in_memory_data={"pending_media":{}}
action_usage={}
memory_lock=threading.Lock()
ALLOWED_EXTENSIONS=set(["mp3","wav","m4a","ogg","webm","flac","mp4","mkv","avi","mov","hevc","aac","aiff","amr","wma","opus","m4v","ts","flv","3gp"])
POSSIBLE_FFMPEG_PATHS=[env.get("FFMPEG_BINARY",""),"./ffmpeg","/usr/bin/ffmpeg","/usr/local/bin/ffmpeg","ffmpeg"]
FFMPEG_BINARY=None
for p in POSSIBLE_FFMPEG_PATHS:
    if not p: continue
    try:
        subprocess.run([p,"-version"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3)
        FFMPEG_BINARY=p; break
    except: pass
if FFMPEG_BINARY is None:
    logging.warning("ffmpeg binary not found. Set FFMPEG_BINARY env var or place ffmpeg in PATH")

transcript_semaphore=threading.Semaphore(MAX_CONCURRENT_TRANSCRIPTS)
PENDING_QUEUE=deque()

def norm_user_id(uid):
    try: return str(int(uid))
    except: return str(uid)

def check_subscription(user_id,bot_obj):
    if not REQUIRED_CHANNEL: return True
    try:
        member=bot_obj.get_chat_member(REQUIRED_CHANNEL,user_id)
        return member.status in ['member','administrator','creator']
    except: return False

def send_subscription_message(chat_id,bot_obj):
    if not REQUIRED_CHANNEL: return
    try:
        chat=bot_obj.get_chat(chat_id)
        if chat.type!='private': return
    except: return
    try:
        m=InlineKeyboardMarkup(); m.add(InlineKeyboardButton("Click here to join the Group ",url=f"https://tme/{REQUIRED_CHANNEL.lstrip('@')}"))
        bot_obj.send_message(chat_id,"🔒 Access Locked You cannot use this bot until you join the Group.",reply_markup=m)
    except: pass

def update_user_activity(user_id):
    uid=norm_user_id(user_id); now=datetime.now()
    users_collection.update_one({"user_id":uid},{"$set":{"last_active":now},"$setOnInsert":{"first_seen":now,"stt_conversion_count":0}},upsert=True)

def increment_processing_count(user_id,service_type):
    users_collection.update_one({"user_id":norm_user_id(user_id)},{"$inc":{f"{service_type}_conversion_count":1}})

def get_stt_user_lang(user_id):
    ud=users_collection.find_one({"user_id":norm_user_id(user_id)})
    return ud.get("stt_language","en") if ud else "en"

def set_stt_user_lang(user_id,lang_code):
    users_collection.update_one({"user_id":norm_user_id(user_id)},{"$set":{"stt_language":lang_code}},upsert=True)

def get_user_send_mode(user_id):
    ud=users_collection.find_one({"user_id":norm_user_id(user_id)})
    return ud.get("stt_send_mode","file") if ud else "file"

def set_user_send_mode(user_id,mode):
    if mode not in ("file","split"): mode="file"
    users_collection.update_one({"user_id":norm_user_id(user_id)},{"$set":{"stt_send_mode":mode}},upsert=True)

def save_pending_media(user_id,media_type,data):
    with memory_lock:
        in_memory_data["pending_media"][user_id]={"media_type":media_type,"data":data,"saved_at":datetime.now()}

def pop_pending_media(user_id):
    with memory_lock:
        return in_memory_data["pending_media"].pop(user_id,None)

def delete_transcription_later(user_id,message_id):
    time.sleep(86400)
    with memory_lock:
        if user_id in user_transcriptions and message_id in user_transcriptions[user_id]:
            del user_transcriptions[user_id][message_id]

def is_transcoding_like_error(msg):
    if not msg: return False
    m=msg.lower()
    checks=["transcoding failed","file does not appear to contain audio","text/html","html document","unsupported media type","could not decode","unsupported audio format","invalid media type"]
    return any(ch in m for ch in checks)

def build_lang_keyboard(callback_prefix,row_width=3,message_id=None):
    m=InlineKeyboardMarkup(row_width=row_width)
    buttons=[InlineKeyboardButton(label,callback_data=f"{callback_prefix}|{code}|{message_id}" if message_id else f"{callback_prefix}|{code}") for label,code in LANG_OPTIONS]
    for i in range(0,len(buttons),row_width): m.add(*buttons[i:i+row_width])
    return m

def build_result_mode_keyboard(prefix="result_mode"):
    m=InlineKeyboardMarkup(row_width=2)
    m.add(InlineKeyboardButton("📄 .txt file",callback_data=f"{prefix}|file"),InlineKeyboardButton("💬 Split messages",callback_data=f"{prefix}|split"))
    return m

def animate_processing_message(bot_obj,chat_id,message_id,stop_event):
    frames=["🔄 Processing","🔄 Processing.","🔄 Processing..","🔄 Processing..."]; idx=0
    while not stop_event():
        try: bot_obj.edit_message_text(frames[idx%len(frames)],chat_id=chat_id,message_id=message_id)
        except: pass
        idx=(idx+1)%len(frames); time.sleep(0.6)

def normalize_text_offline(text):
    return re.sub(r'\s+',' ',text).strip() if text else text

def extract_key_points_offline(text,max_points=6):
    if not text: return ""
    sentences=[s.strip() for s in re.split(r'(?<=[\.\!\?])\s+',text) if s.strip()]
    if not sentences: return ""
    words=[w for w in re.findall(r'\w+',text.lower()) if len(w)>3]
    if not words: return "\n".join(f"- {s}" for s in sentences[:max_points])
    freq=Counter(words)
    sentence_scores=[(sum(freq.get(w,0) for w in re.findall(r'\w+',s.lower())),s) for s in sentences]
    sentence_scores.sort(key=lambda x:x[0],reverse=True)
    top_sentences=sorted(sentence_scores[:max_points],key=lambda x:sentences.index(x[1]))
    return "\n".join(f"- {s}" for _,s in top_sentences)

def safe_extension_from_filename(filename):
    return filename.rsplit(".",1)[-1].lower() if filename and "." in filename else ""

def telegram_file_info_and_url(bot_token,file_id):
    url=f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
    resp=requests.get(url,timeout=REQUEST_TIMEOUT_TELEGRAM); resp.raise_for_status()
    file_path=resp.json().get("result",{}).get("file_path")
    return type('T',(),{'file_path':file_path})(), f"https://api.telegram.org/file/bot{bot_token}/{file_path}"

# AssemblyAI Transcribe function
def transcribe_via_assemblyai(audio_url, language_code):
    if not ASSEMBLYAI_API_KEY: raise RuntimeError("ASSEMBLYAI_API_KEY not set")
    
    headers = {"authorization": ASSEMBLYAI_API_KEY,"content-type": "application/json"}
    
    data = {
        "audio_url": audio_url,
        "language_code": language_code,
        "speaker_diarization": True, # Ama True ama False sidaad rabto
        "punctuate": True,
        "format_text": True,
        "auto_chapters": False,
        "speech_model": "best" # universal ama best
    }

    submit_url = ASSEMBLYAI_BASE_URL + "/transcript"
    response = requests.post(submit_url, json=data, headers=headers, timeout=REQUEST_TIMEOUT_GEMINI)
    response.raise_for_status()
    
    transcript_id = response.json().get('id')
    if not transcript_id: raise RuntimeError("Failed to get transcript ID from AssemblyAI")
    
    polling_endpoint = ASSEMBLYAI_BASE_URL + "/transcript/" + transcript_id
    
    while True:
        transcription_result = requests.get(polling_endpoint, headers=headers, timeout=REQUEST_TIMEOUT_GEMINI).json()
        status = transcription_result.get('status')
        
        if status == 'completed':
            text = transcription_result.get('text')
            if text is None: raise RuntimeError("AssemblyAI returned 'completed' but with no text.")
            return text
        
        elif status == 'error':
            error_msg = transcription_result.get('error', 'Unknown AssemblyAI error')
            raise RuntimeError(f"AssemblyAI Transcription failed: {error_msg}")
        
        elif status == 'queued' or status == 'processing':
            time.sleep(3) # Waiting for a few seconds before polling again
        
        else:
            raise RuntimeError(f"AssemblyAI returned unexpected status: {status}")

def transcribe_via_selected_service(input_file_url, lang_code):
    try:
        text=transcribe_via_assemblyai(input_file_url, lang_code)
        if text is None: raise RuntimeError("AssemblyAI returned no text")
        return text,"assemblyai"
    except Exception as e:
        logging.exception("AssemblyAI failed")
        error_message = str(e)
        # Handle known AssemblyAI-related errors specifically
        if "Unsupported audio format" in error_message or "invalid media type" in error_message:
            raise RuntimeError("AssemblyAI: Unsupported audio format or invalid media type. Please try a common format like MP3, M4A, or OGG.")
        elif "Transcoding failed" in error_message:
            raise RuntimeError("AssemblyAI: Transcoding failed, the file may be corrupt or an unusual format.")
        elif "authorization" in error_message:
            raise RuntimeError("AssemblyAI: API Key authorization failed.")
        else:
            raise RuntimeError("AssemblyAI failed: "+error_message)


def split_text_into_chunks(text,limit=4096):
    if not text: return []
    chunks=[]; start=0; n=len(text)
    while start<n:
        end=min(start+limit,n)
        if end<n:
            last_space=text.rfind(" ",start,end)
            if last_space>start: end=last_space
        chunk=text[start:end].strip()
        if not chunk:
            end=start+limit
            chunk=text[start:end].strip()
        chunks.append(chunk); start=end
    return chunks

def attach_action_buttons(bot_obj,chat_id,message_id,text):
    try:
        include_summarize = len(text)>1000 if text else False
        m=InlineKeyboardMarkup()
        m.add(InlineKeyboardButton("⭐️Clean transcript",callback_data=f"clean_up|{chat_id}|{message_id}"))
        if include_summarize: m.add(InlineKeyboardButton("Get Summarize",callback_data=f"get_key_points|{chat_id}|{message_id}"))
        try: bot_obj.edit_message_reply_markup(chat_id,message_id,reply_markup=m)
        except: pass
    except: pass
    try:
        action_usage[f"{chat_id}|{message_id}|clean_up"]=0
        action_usage[f"{chat_id}|{message_id}|get_key_points"]=0
    except: pass

def process_media_file(message,bot_obj,bot_token,bot_index,file_id,file_size,filename):
    uid=str(message.from_user.id); chatid=str(message.chat.id)
    lang=get_stt_user_lang(uid)
    processing_msg=bot_obj.send_message(message.chat.id,"🔄 Processing...",reply_to_message_id=message.message_id)
    processing_msg_id=processing_msg.message_id
    stop={"stop":False}
    animation_thread=threading.Thread(target=animate_processing_message,args=(bot_obj,message.chat.id,processing_msg_id,lambda:stop["stop"]))
    animation_thread.start()
    
    try:
        # Get Telegram file URL
        tf,file_url=telegram_file_info_and_url(bot_token,file_id)
        
        try:
            # Use AssemblyAI to transcribe directly from the Telegram URL
            text,used_service=transcribe_via_selected_service(file_url,lang)
        except Exception as e:
            error_msg=str(e); logging.exception("Error during transcription")
            if "authorization" in error_msg.lower():
                bot_obj.send_message(message.chat.id,"⚠️ AssemblyAI Error: API Key Authorization Failed. Please check your key.",reply_to_message_id=message.message_id)
            elif is_transcoding_like_error(error_msg) or "unsupported audio format" in error_msg.lower() or "transcoding failed" in error_msg.lower():
                bot_obj.send_message(message.chat.id,"⚠️ Transcription error: The file is not audible or the format is unsupported by AssemblyAI. Please send a different file.",reply_to_message_id=message.message_id)
            else:
                bot_obj.send_message(message.chat.id,f"Error during transcription: {error_msg}",reply_to_message_id=message.message_id)
            return
            
        corrected_text=normalize_text_offline(text)
        uid_key=str(message.chat.id); user_mode=get_user_send_mode(uid_key)
        
        if len(corrected_text)>4000:
            if user_mode=="file":
                f=io.BytesIO(corrected_text.encode("utf-8")); f.name="Transcript.txt"
                sent=bot_obj.send_document(message.chat.id,f,reply_to_message_id=message.message_id)
                try: attach_action_buttons(bot_obj,message.chat.id,sent.message_id,corrected_text)
                except: pass
                try:
                    user_transcriptions.setdefault(uid_key,{})[sent.message_id]=corrected_text
                    threading.Thread(target=delete_transcription_later,args=(uid_key,sent.message_id),daemon=True).start()
                except: pass
            else:
                chunks=split_text_into_chunks(corrected_text,limit=4096); last_sent=None
                for idx,chunk in enumerate(chunks):
                    if idx==0: last_sent=bot_obj.send_message(message.chat.id,chunk,reply_to_message_id=message.message_id)
                    else: last_sent=bot_obj.send_message(message.chat.id,chunk)
                try: attach_action_buttons(bot_obj,message.chat.id,last_sent.message_id,corrected_text)
                except: pass
                try:
                    user_transcriptions.setdefault(uid_key,{})[last_sent.message_id]=corrected_text
                    threading.Thread(target=delete_transcription_later,args=(uid_key,last_sent.message_id),daemon=True).start()
                except: pass
        else:
            sent_msg=bot_obj.send_message(message.chat.id,corrected_text or "⚠️ Warning Make sure the voice is clear or speaking in the language you Choosed.",reply_to_message_id=message.message_id)
            try: attach_action_buttons(bot_obj,message.chat.id,sent_msg.message_id,corrected_text)
            except: pass
            try:
                user_transcriptions.setdefault(uid_key,{})[sent_msg.message_id]=corrected_text
                threading.Thread(target=delete_transcription_later,args=(uid_key,sent_msg.message_id),daemon=True).start()
            except: pass
        increment_processing_count(uid,"stt")
        
    finally:
        stop["stop"]=True; animation_thread.join()
        try: bot_obj.delete_message(message.chat.id,processing_msg_id)
        except: pass

def worker_thread():
    while True:
        try:
            transcript_semaphore.acquire()
            item=None
            with memory_lock:
                if PENDING_QUEUE: item=PENDING_QUEUE.popleft()
            if item:
                message,bot_obj,bot_token,bot_index,file_id,file_size,filename=item
                logging.info(f"Starting processing for user {message.from_user.id} (Chat {message.chat.id}) from queue. Current queue size: {len(PENDING_QUEUE)}")
                process_media_file(message,bot_obj,bot_token,bot_index,file_id,file_size,filename)
            else:
                transcript_semaphore.release()
        except:
            logging.exception("Error in worker thread")
        finally:
            if item: transcript_semaphore.release()
            time.sleep(0.5)

def start_worker_threads():
    for i in range(MAX_CONCURRENT_TRANSCRIPTS):
        t=threading.Thread(target=worker_thread,daemon=True); t.start()

start_worker_threads()

def handle_media_common(message,bot_obj,bot_token,bot_index=0):
    update_user_activity(message.from_user.id)
    if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj):
        send_subscription_message(message.chat.id,bot_obj); return
    file_id=file_size=filename=None
    if message.voice:
        file_id=message.voice.file_id; file_size=message.voice.file_size; filename="voice.ogg"
    elif message.audio:
        file_id=message.audio.file_id; file_size=message.audio.file_size; filename=getattr(message.audio,"file_name","audio")
    elif message.video:
        file_id=message.video.file_id; file_size=message.video.file_size; filename=getattr(message.video,"file_name","video.mp4")
    elif message.document:
        mime=getattr(message.document,"mime_type",None); filename=getattr(message.document,"file_name",None) or "file"
        ext=safe_extension_from_filename(filename)
        if (mime and ("audio" in mime or "video" in mime)) or ext in ALLOWED_EXTENSIONS:
            file_id=message.document.file_id; file_size=message.document.file_size
        else:
            bot_obj.send_message(message.chat.id,"Sorry, I can only transcribe audio or video files."); return
    if file_size and file_size>TELEGRAM_MAX_BYTES:
        max_display_mb=TELEGRAM_MAX_BYTES//(1024*1024)
        bot_obj.send_message(message.chat.id,f"Just Send me a file less than {max_display_mb}MB 😎",reply_to_message_id=message.message_id); return
    with memory_lock:
        if len(PENDING_QUEUE)>=MAX_PENDING_QUEUE:
            bot_obj.send_message(message.chat.id,"⚠️ Server busy. Try again later.",reply_to_message_id=message.message_id); return
        PENDING_QUEUE.append((message,bot_obj,bot_token,bot_index,file_id,file_size,filename))

def ask_gemini(text,instruction,timeout=REQUEST_TIMEOUT_GEMINI):
    if not GEMINI_API_KEY: raise RuntimeError("GEMINI_API_KEY not set")
    url=f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload={"contents":[{"parts":[{"text":instruction},{"text":text}]}]}
    headers={"Content-Type":"application/json"}
    resp=requests.post(url,headers=headers,json=payload,timeout=timeout); resp.raise_for_status()
    result=resp.json()
    if "candidates" in result and isinstance(result["candidates"],list) and len(result["candidates"])>0:
        try: return result['candidates'][0]['content']['parts'][0]['text']
        except: return json.dumps(result['candidates'][0])
    return json.dumps(result)

def register_handlers(bot_obj,bot_token,bot_index):
    @bot_obj.message_handler(commands=['start'])
    def start_handler(message):
        try:
            update_user_activity(message.from_user.id)
            if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj):
                send_subscription_message(message.chat.id,bot_obj); return
            bot_obj.send_message(message.chat.id,"Choose your file language for transcription using the below buttons:",reply_markup=build_lang_keyboard("start_select_lang"))
        except: logging.exception("Error in start_handler")

    @bot_obj.callback_query_handler(func=lambda c:c.data and c.data.startswith("start_select_lang|"))
    def start_select_lang_callback(call):
        try:
            uid=str(call.from_user.id); _,lang_code=call.data.split("|",1)
            lang_label=CODE_TO_LABEL.get(lang_code,lang_code)
            set_stt_user_lang(uid,lang_code)
            try: bot_obj.delete_message(call.message.chat.id,call.message.message_id)
            except: pass
            welcome_text="👋 Salaam!    \n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free"
            bot_obj.send_message(call.message.chat.id,welcome_text)
            bot_obj.answer_callback_query(call.id,f"✅ Language set to {lang_label}")
        except:
            logging.exception("Error in start_select_lang_callback")
            try: bot_obj.answer_callback_query(call.id,"❌ Error setting language, try again.",show_alert=True)
            except: pass

    @bot_obj.message_handler(commands=['help'])
    def handle_help(message):
        try:
            update_user_activity(message.from_user.id)
            if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj):
                send_subscription_message(message.chat.id,bot_obj); return
            text="Commands supported:\n/start - Show welcome message\n/lang  - Change language\n/mode  - Change result delivery mode\n/help  - This help message\n\nSend a voice/audio/video (up to 20MB) and I will transcribe it Need help? Contact: @lakigithub"
            bot_obj.send_message(message.chat.id,text)
        except: logging.exception("Error in handle_help")

    @bot_obj.message_handler(commands=['lang'])
    def handle_lang(message):
        try:
            if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj):
                send_subscription_message(message.chat.id,bot_obj); return
            kb=build_lang_keyboard("stt_lang")
            bot_obj.send_message(message.chat.id,"Choose your file language for transcription using the below buttons:",reply_markup=kb)
        except: logging.exception("Error in handle_lang")

    @bot_obj.message_handler(commands=['mode'])
    def handle_mode(message):
        try:
            if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj):
                send_subscription_message(message.chat.id,bot_obj); return
            current_mode=get_user_send_mode(str(message.from_user.id))
            mode_text="📄 .txt file" if current_mode=="file" else "💬 Split messages"
            bot_obj.send_message(message.chat.id,f"Result delivery mode: {mode_text}. Change it below:",reply_markup=build_result_mode_keyboard())
        except: logging.exception("Error in handle_mode")

    @bot_obj.callback_query_handler(lambda c:c.data and c.data.startswith("stt_lang|"))
    def on_stt_language_select(call):
        try:
            uid=str(call.from_user.id); _,lang_code=call.data.split("|",1)
            lang_label=CODE_TO_LABEL.get(lang_code,lang_code)
            set_stt_user_lang(uid,lang_code)
            bot_obj.answer_callback_query(call.id,f"✅ Language set: {lang_label}")
            try: bot_obj.delete_message(call.message.chat.id,call.message.message_id)
            except: pass
        except:
            logging.exception("Error in on_stt_language_select")
            try: bot_obj.answer_callback_query(call.id,"❌ Error setting language, try again.",show_alert=True)
            except: pass

    @bot_obj.callback_query_handler(lambda c:c.data and c.data.startswith("result_mode|"))
    def on_result_mode_select(call):
        try:
            uid=str(call.from_user.id); _,mode=call.data.split("|",1)
            set_user_send_mode(uid,mode)
            mode_text="📄 .txt file" if mode=="file" else "💬 Split messages"
            try: bot_obj.delete_message(call.message.chat.id,call.message.message_id)
            except: pass
            bot_obj.answer_callback_query(call.id,f"✅ Result mode set: {mode_text}")
        except:
            logging.exception("Error in on_result_mode_select")
            try: bot_obj.answer_callback_query(call.id,"❌ Error setting result mode, try again.",show_alert=True)
            except: pass

    @bot_obj.message_handler(content_types=['new_chat_members'])
    def handle_new_chat_members(message):
        try:
            if message.new_chat_members[0].id==bot_obj.get_me().id:
                group_data={'_id':str(message.chat.id),'title':message.chat.title,'type':message.chat.type,'added_date':datetime.now()}
                groups_collection.update_one({'_id':group_data['_id']},{'$set':group_data},upsert=True)
                bot_obj.send_message(message.chat.id,"Thanks for adding me! I'm ready to transcribe your media files.")
        except: logging.exception("Error in handle_new_chat_members")

    @bot_obj.message_handler(content_types=['left_chat_member'])
    def handle_left_chat_member(message):
        try:
            if message.left_chat_member.id==bot_obj.get_me().id:
                groups_collection.delete_one({'_id':str(message.chat.id)})
        except: logging.exception("Error in handle_left_chat_member")

    @bot_obj.message_handler(content_types=['voice','audio','video','document'])
    def handle_media_types(message):
        try: handle_media_common(message,bot_obj,bot_token,bot_index)
        except: logging.exception("Error in handle_media_types")

    @bot_obj.message_handler(content_types=['text'])
    def handle_text_messages(message):
        try:
            if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj):
                send_subscription_message(message.chat.id,bot_obj); return
            bot_obj.send_message(message.chat.id,"For Text to Audio Use: @TextToSpeechBBot")
        except: logging.exception("Error in handle_text_messages")

    @bot_obj.callback_query_handler(lambda c:c.data and c.data.startswith("get_key_points|"))
    def get_key_points_callback(call):
        try:
            parts=call.data.split("|")
            if len(parts)==3: _,chat_id_part,msg_id_part=parts
            elif len(parts)==2: _,msg_id_part=parts; chat_id_part=str(call.message.chat.id)
            else:
                bot_obj.answer_callback_query(call.id,"Invalid request",show_alert=True); return
            try:
                chat_id_val=int(chat_id_part); msg_id=int(msg_id_part)
            except:
                bot_obj.answer_callback_query(call.id,"Invalid message id",show_alert=True); return
            usage_key=f"{chat_id_val}|{msg_id}|get_key_points"; usage=action_usage.get(usage_key,0)
            if usage>=1:
                bot_obj.answer_callback_query(call.id,"Get Summarize unavailable (maybe expired)",show_alert=True); return
            action_usage[usage_key]=usage+1
            uid_key=str(chat_id_val); stored=user_transcriptions.get(uid_key,{}).get(msg_id)
            if not stored:
                bot_obj.answer_callback_query(call.id,"Get Summarize unavailable (maybe expired)",show_alert=True); return
            bot_obj.answer_callback_query(call.id,"Generating...")
            status_msg=bot_obj.send_message(call.message.chat.id,"🔄 Processing...",reply_to_message_id=call.message.message_id)
            stop={"stop":False}
            animation_thread=threading.Thread(target=animate_processing_message,args=(bot_obj,call.message.chat.id,status_msg.message_id,lambda:stop["stop"]))
            animation_thread.start()
            try:
                lang=get_stt_user_lang(str(chat_id_val)) or "en"
                instruction=f"What is this report and what is it about? Please summarize them for me into (lang={lang}) without adding any introductions, notes, or extra phrases."
                try: summary=ask_gemini(stored,instruction)
                except: summary=extract_key_points_offline(stored,max_points=6)
            except: summary=""
            stop["stop"]=True; animation_thread.join()
            if not summary:
                try: bot_obj.edit_message_text("No Summary returned.",chat_id=call.message.chat.id,message_id=status_msg.message_id)
                except: pass
            else:
                try: bot_obj.edit_message_text(f"{summary}",chat_id=call.message.chat.id,message_id=status_msg.message_id)
                except: pass
        except: logging.exception("Error in get_key_points_callback")

    @bot_obj.callback_query_handler(lambda c:c.data and c.data.startswith("clean_up|"))
    def clean_up_callback(call):
        try:
            parts=call.data.split("|")
            if len(parts)==3: _,chat_id_part,msg_id_part=parts
            elif len(parts)==2: _,msg_id_part=parts; chat_id_part=str(call.message.chat.id)
            else:
                bot_obj.answer_callback_query(call.id,"Invalid request",show_alert=True); return
            try:
                chat_id_val=int(chat_id_part); msg_id=int(msg_id_part)
            except:
                bot_obj.answer_callback_query(call.id,"Invalid message id",show_alert=True); return
            usage_key=f"{chat_id_val}|{msg_id}|clean_up"; usage=action_usage.get(usage_key,0)
            if usage>=1:
                bot_obj.answer_callback_query(call.id,"Clean up unavailable (maybe expired)",show_alert=True); return
            action_usage[usage_key]=usage+1
            uid_key=str(chat_id_val); stored=user_transcriptions.get(uid_key,{}).get(msg_id)
            if not stored:
                bot_obj.answer_callback_query(call.id,"Clean up unavailable (maybe expired)",show_alert=True); return
            bot_obj.answer_callback_query(call.id,"Cleaning up...")
            status_msg=bot_obj.send_message(call.message.chat.id,"🔄 Processing...",reply_to_message_id=call.message.message_id)
            stop={"stop":False}
            animation_thread=threading.Thread(target=animate_processing_message,args=(bot_obj,call.message.chat.id,status_msg.message_id,lambda:stop["stop"]))
            animation_thread.start()
            try:
                lang=get_stt_user_lang(str(chat_id_val)) or "en"
                instruction=f"Clean and normalize this transcription (lang={lang}). Remove ASR artifacts like [inaudible], repeated words, filler noises, timestamps, and incorrect punctuation. Produce a clean, well-punctuated, readable text in the same language. Do not add introductions or explanations."
                try: cleaned=ask_gemini(stored,instruction)
                except: cleaned=normalize_text_offline(stored)
            except: cleaned=""
            stop["stop"]=True; animation_thread.join()
            if not cleaned:
                try: bot_obj.edit_message_text("No cleaned text returned.",chat_id=call.message.chat.id,message_id=status_msg.message_id)
                except: pass
                return
            uid_key=str(chat_id_val); user_mode=get_user_send_mode(uid_key)
            if len(cleaned)>4000:
                if user_mode=="file":
                    f=io.BytesIO(cleaned.encode("utf-8")); f.name="cleaned.txt"
                    try: bot_obj.delete_message(call.message.chat.id,status_msg.message_id)
                    except: pass
                    sent=bot_obj.send_document(call.message.chat.id,f,reply_to_message_id=call.message.message_id)
                    try:
                        user_transcriptions.setdefault(uid_key,{})[sent.message_id]=cleaned
                        threading.Thread(target=delete_transcription_later,args=(uid_key,sent.message_id),daemon=True).start()
                    except: pass
                    try:
                        action_usage[f"{call.message.chat.id}|{sent.message_id}|clean_up"]=0
                        action_usage[f"{call.message.chat.id}|{sent.message_id}|get_key_points"]=0
                    except: pass
                else:
                    try: bot_obj.delete_message(call.message.chat.id,status_msg.message_id)
                    except: pass
                    chunks=split_text_into_chunks(cleaned,limit=4096); last_sent=None
                    for idx,chunk in enumerate(chunks):
                        if idx==0: last_sent=bot_obj.send_message(call.message.chat.id,chunk,reply_to_message_id=call.message.message_id)
                        else: last_sent=bot_obj.send_message(call.message.chat.id,chunk)
                    try:
                        user_transcriptions.setdefault(uid_key,{})[last_sent.message_id]=cleaned
                        threading.Thread(target=delete_transcription_later,args=(uid_key,last_sent.message_id),daemon=True).start()
                    except: pass
                    try:
                        action_usage[f"{call.message.chat.id}|{last_sent.message_id}|clean_up"]=0
                        action_usage[f"{call.message.chat.id}|{last_sent.message_id}|get_key_points"]=0
                    except: pass
            else:
                try:
                    bot_obj.edit_message_text(f"{cleaned}",chat_id=call.message.chat.id,message_id=status_msg.message_id)
                    uid_key=str(chat_id_val)
                    user_transcriptions.setdefault(uid_key,{})[status_msg.message_id]=cleaned
                    threading.Thread(target=delete_transcription_later,args=(uid_key,status_msg.message_id),daemon=True).start()
                    action_usage[f"{call.message.chat.id}|{status_msg.message_id}|clean_up"]=0
                    action_usage[f"{call.message.chat.id}|{status_msg.message_id}|get_key_points"]=0
                except: pass
        except: logging.exception("Error in clean_up_callback")

for idx,bot_obj in enumerate(bots): register_handlers(bot_obj,BOT_TOKENS[idx],idx)

@app.route("/",methods=["GET","POST","HEAD"])
def webhook_root():
    if request.method in ("GET","HEAD"):
        bot_index=request.args.get("bot_index")
        try: bot_index_val=int(bot_index) if bot_index is not None else 0
        except: bot_index_val=0
        now_iso=datetime.utcnow().isoformat()+"Z"
        return jsonify({"status":"ok","time":now_iso,"bot_index":bot_index_val}),200
    if request.method=="POST":
        content_type=request.headers.get("Content-Type","")
        if content_type and content_type.startswith("application/json"):
            raw=request.get_data().decode("utf-8")
            try: payload=json.loads(raw)
            except: payload=None
            bot_index=request.args.get("bot_index")
            if not bot_index and isinstance(payload,dict): bot_index=payload.get("bot_index")
            header_idx=request.headers.get("X-Bot-Index")
            if header_idx: bot_index=header_idx
            try: bot_index_val=int(bot_index) if bot_index is not None else 0
            except: bot_index_val=0
            if bot_index_val<0 or bot_index_val>=len(bots): return abort(404)
            try:
                update=telebot.types.Update.de_json(payload)
                bots[bot_index_val].process_new_updates([update])
            except: logging.exception("Error processing incoming webhook update")
            return "",200
    return abort(403)

@app.route("/set_webhook",methods=["GET","POST"])
def set_webhook_route():
    results=[]
    for idx,bot_obj in enumerate(bots):
        try:
            url=WEBHOOK_BASE.rstrip("/")+f"/?bot_index={idx}"
            bot_obj.delete_webhook(); time.sleep(0.2)
            bot_obj.set_webhook(url=url)
            results.append({"index":idx,"url":url,"status":"ok"})
        except Exception as e:
            logging.error(f"Failed to set webhook for bot {idx}: {e}")
            results.append({"index":idx,"error":str(e)})
    return jsonify({"results":results}),200

@app.route("/delete_webhook",methods=["GET","POST"])
def delete_webhook_route():
    results=[]
    for idx,bot_obj in enumerate(bots):
        try: bot_obj.delete_webhook(); results.append({"index":idx,"status":"deleted"})
        except Exception as e:
            logging.error(f"Failed to delete webhook for bot {idx}: {e}"); results.append({"index":idx,"error":str(e)})
    return jsonify({"results":results}),200

def set_webhook_on_startup():
    for idx,bot_obj in enumerate(bots):
        try:
            bot_obj.delete_webhook(); time.sleep(0.2)
            url=WEBHOOK_BASE.rstrip("/")+f"/?bot_index={idx}"
            bot_obj.set_webhook(url=url)
            logging.info(f"Main bot webhook set successfully to {url}")
        except Exception as e:
            logging.error(f"Failed to set main bot webhook on startup: {e}")

def set_bot_info_and_startup():
    set_webhook_on_startup()

if __name__=="__main__":
    try:
        set_bot_info_and_startup()
        try:
            client.admin.command('ping'); logging.info("Successfully connected to MongoDB!")
        except Exception as e:
            logging.error("Could not connect to MongoDB: %s",e)
    except:
        logging.exception("Failed during startup")
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8080)))
