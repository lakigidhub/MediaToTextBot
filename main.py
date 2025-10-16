import os,logging,requests,telebot,json,threading,time,io,re
from flask import Flask,request,abort,jsonify
from datetime import datetime
from telebot.types import InlineKeyboardMarkup,InlineKeyboardButton
from pymongo import MongoClient
from collections import Counter,deque

logging.basicConfig(level=logging.INFO,format='%(asctime)s - %(levelname)s - %(message)s')

env=os.environ
TRANSCRIBE_MAX_WORKERS=int(env.get("TRANSCRIBE_MAX_WORKERS","4"))
TELEGRAM_MAX_BYTES=int(env.get("TELEGRAM_MAX_BYTES",str(20*1024*1024)))
REQUEST_TIMEOUT_TELEGRAM=int(env.get("REQUEST_TIMEOUT_TELEGRAM","300"))
REQUEST_TIMEOUT_GEMINI=int(env.get("REQUEST_TIMEOUT_GEMINI","300"))
MAX_CONCURRENT_TRANSCRIPTS=int(env.get("MAX_CONCURRENT_TRANSCRIPTS","2"))
MAX_PENDING_QUEUE=int(env.get("MAX_PENDING_QUEUE","2"))
GEMINI_API_KEY=env.get("GEMINI_API_KEY","")
WEBHOOK_BASE=env.get("WEBHOOK_BASE","")
MONGO_URI=env.get("MONGO_URI","")
DB_NAME=env.get("DB_NAME","telegram_bot_db")
REQUIRED_CHANNEL=env.get("REQUIRED_CHANNEL","")
BOT_TOKENS=[t.strip() for t in env.get("BOT_TOKENS","").split(",") if t.strip()]
ASSEMBLYAI_API_KEY=env.get("ASSEMBLYAI_API_KEY","26293b7d8dbf43d883ce8a43d3c06f63")
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
user_transcriptions={}
action_usage={}
memory_lock=threading.Lock()
ALLOWED_EXTENSIONS=set(["mp3","wav","m4a","ogg","webm","flac","mp4","mkv","avi","mov","hevc","aac","aiff","amr","wma","opus","m4v","ts","flv","3gp"])
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
        if bot_obj.get_chat(chat_id).type!='private': return
        m=InlineKeyboardMarkup(); m.add(InlineKeyboardButton("Click here to join the Group ",url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"))
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

def delete_transcription_later(user_id,message_id):
    time.sleep(86400)
    with memory_lock:
        user_transcriptions.get(user_id,{}).pop(message_id,None)

def is_transcoding_like_error(msg):
    return any(ch in msg.lower() for ch in ["transcoding failed","file does not appear to contain audio","text/html","html document","unsupported media type","could not decode","unsupported audio format","invalid media type"]) if msg else False

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
    if not sentences: return "\n".join(f"- {s}" for s in sentences[:max_points])
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
    return f"https://api.telegram.org/file/bot{bot_token}/{file_path}"

def transcribe_via_assemblyai(audio_url, language_code):
    if not ASSEMBLYAI_API_KEY: raise RuntimeError("ASSEMBLYAI_API_KEY not set")
    headers = {"authorization": ASSEMBLYAI_API_KEY,"content-type": "application/json"}
    data = {"audio_url": audio_url,"language_code": language_code,"speaker_diarization": True,"punctuate": True,"format_text": True,"auto_chapters": False,"speech_model": "best"}
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
        elif status in ('queued', 'processing'):
            time.sleep(3)
        else:
            raise RuntimeError(f"AssemblyAI returned unexpected status: {status}")

def transcribe_via_selected_service(input_file_url, lang_code):
    try:
        text=transcribe_via_assemblyai(input_file_url, lang_code)
        if text is None: raise RuntimeError("AssemblyAI returned no text")
        return text,"assemblyai"
    except Exception as e:
        logging.exception("AssemblyAI failed")
        raise RuntimeError("AssemblyAI failed: "+str(e))

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
        action_usage[f"{chat_id}|{message_id}|clean_up"]=0
        action_usage[f"{chat_id}|{message_id}|get_key_points"]=0
    except: pass

def send_transcribed_result(bot_obj,message,corrected_text):
    uid_key=str(message.chat.id); user_mode=get_user_send_mode(uid_key); last_sent=None
    if len(corrected_text)>4000:
        if user_mode=="file":
            f=io.BytesIO(corrected_text.encode("utf-8")); f.name="Transcript.txt"
            last_sent=bot_obj.send_document(message.chat.id,f,reply_to_message_id=message.message_id)
        else:
            chunks=split_text_into_chunks(corrected_text,limit=4096)
            for idx,chunk in enumerate(chunks):
                if idx==0: last_sent=bot_obj.send_message(message.chat.id,chunk,reply_to_message_id=message.message_id)
                else: last_sent=bot_obj.send_message(message.chat.id,chunk)
    else:
        last_sent=bot_obj.send_message(message.chat.id,corrected_text or "⚠️ Warning Make sure the voice is clear or speaking in the language you Choosed.",reply_to_message_id=message.message_id)
    if last_sent:
        attach_action_buttons(bot_obj,message.chat.id,last_sent.message_id,corrected_text)
        user_transcriptions.setdefault(uid_key,{})[last_sent.message_id]=corrected_text
        threading.Thread(target=delete_transcription_later,args=(uid_key,last_sent.message_id),daemon=True).start()

def process_media_file(message,bot_obj,bot_token,bot_index,file_id,file_size,filename):
    uid=str(message.from_user.id); lang=get_stt_user_lang(uid)
    processing_msg=bot_obj.send_message(message.chat.id,"🔄 Processing...",reply_to_message_id=message.message_id)
    processing_msg_id=processing_msg.message_id; stop={"stop":False}
    animation_thread=threading.Thread(target=animate_processing_message,args=(bot_obj,message.chat.id,processing_msg_id,lambda:stop["stop"]))
    animation_thread.start()
    
    try:
        file_url=telegram_file_info_and_url(bot_token,file_id)
        text,_=transcribe_via_selected_service(file_url,lang)
        corrected_text=normalize_text_offline(text)
        send_transcribed_result(bot_obj,message,corrected_text)
        increment_processing_count(uid,"stt")
    except Exception as e:
        error_msg=str(e); logging.exception("Error during transcription")
        if "authorization" in error_msg.lower():
            bot_obj.send_message(message.chat.id,"⚠️ AssemblyAI Error: API Key Authorization Failed. Please check your key.",reply_to_message_id=message.message_id)
        elif is_transcoding_like_error(error_msg):
            bot_obj.send_message(message.chat.id,"⚠️ Transcription error: The file is not audible or the format is unsupported by AssemblyAI. Please send a different file.",reply_to_message_id=message.message_id)
        else:
            bot_obj.send_message(message.chat.id,f"Error during transcription: {error_msg}",reply_to_message_id=message.message_id)
    finally:
        stop["stop"]=True; animation_thread.join()
        try: bot_obj.delete_message(message.chat.id,processing_msg_id)
        except: pass

def worker_thread():
    while True:
        item=None
        try:
            transcript_semaphore.acquire()
            with memory_lock:
                if PENDING_QUEUE: item=PENDING_QUEUE.popleft()
            if item:
                logging.info(f"Starting processing for user {item[0].from_user.id} (Chat {item[0].chat.id}) from queue. Current queue size: {len(PENDING_QUEUE)}")
                process_media_file(*item)
            else:
                transcript_semaphore.release()
        except:
            logging.exception("Error in worker thread")
        finally:
            if item: transcript_semaphore.release()
            time.sleep(0.5)

def start_worker_threads():
    for _ in range(MAX_CONCURRENT_TRANSCRIPTS):
        threading.Thread(target=worker_thread,daemon=True).start()

start_worker_threads()

def handle_media_common(message,bot_obj,bot_token,bot_index=0):
    update_user_activity(message.from_user.id)
    if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj):
        send_subscription_message(message.chat.id,bot_obj); return
    file_info=None
    if message.voice: file_info=(message.voice.file_id,message.voice.file_size,"voice.ogg")
    elif message.audio: file_info=(message.audio.file_id,message.audio.file_size,getattr(message.audio,"file_name","audio"))
    elif message.video: file_info=(message.video.file_id,message.video.file_size,getattr(message.video,"file_name","video.mp4"))
    elif message.document:
        mime=getattr(message.document,"mime_type",None); filename=getattr(message.document,"file_name",None) or "file"
        ext=safe_extension_from_filename(filename)
        if (mime and ("audio" in mime or "video" in mime)) or ext in ALLOWED_EXTENSIONS:
            file_info=(message.document.file_id,message.document.file_size,filename)
        else:
            bot_obj.send_message(message.chat.id,"Sorry, I can only transcribe audio or video files."); return
    if file_info and file_info[1]>TELEGRAM_MAX_BYTES:
        max_display_mb=TELEGRAM_MAX_BYTES//(1024*1024)
        bot_obj.send_message(message.chat.id,f"Just Send me a file less than {max_display_mb}MB 😎",reply_to_message_id=message.message_id); return
    if file_info:
        if len(PENDING_QUEUE)>=MAX_PENDING_QUEUE:
            bot_obj.send_message(message.chat.id,"⚠️ Server busy. Try again later.",reply_to_message_id=message.message_id); return
        PENDING_QUEUE.append((message,bot_obj,bot_token,bot_index,*file_info))

def ask_gemini(text,instruction,timeout=REQUEST_TIMEOUT_GEMINI):
    if not GEMINI_API_KEY: raise RuntimeError("GEMINI_API_KEY not set")
    url=f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload={"contents":[{"parts":[{"text":instruction},{"text":text}]}]}
    headers={"Content-Type":"application/json"}
    resp=requests.post(url,headers=headers,json=payload,timeout=timeout); resp.raise_for_status()
    result=resp.json()
    if "candidates" in result and len(result["candidates"])>0 and "text" in result['candidates'][0]['content']['parts'][0]:
        return result['candidates'][0]['content']['parts'][0]['text']
    return json.dumps(result)

def handle_action_callback(call,bot_obj,action):
    parts=call.data.split("|"); chat_id_part=parts[1] if len(parts)==3 else str(call.message.chat.id); msg_id_part=parts[-1]
    try: chat_id_val=int(chat_id_part); msg_id=int(msg_id_part)
    except: bot_obj.answer_callback_query(call.id,"Invalid message id",show_alert=True); return
    usage_key=f"{chat_id_val}|{msg_id}|{action}"; usage=action_usage.get(usage_key,0)
    if usage>=1: bot_obj.answer_callback_query(call.id,f"{action} unavailable (maybe expired)",show_alert=True); return
    action_usage[usage_key]=usage+1
    uid_key=str(chat_id_val); stored=user_transcriptions.get(uid_key,{}).get(msg_id)
    if not stored: bot_obj.answer_callback_query(call.id,f"{action} unavailable (maybe expired)",show_alert=True); return
    bot_obj.answer_callback_query(call.id,"Generating...")
    status_msg=bot_obj.send_message(call.message.chat.id,"🔄 Processing...",reply_to_message_id=call.message.message_id)
    stop={"stop":False}
    animation_thread=threading.Thread(target=animate_processing_message,args=(bot_obj,call.message.chat.id,status_msg.message_id,lambda:stop["stop"]))
    animation_thread.start()
    try:
        lang=get_stt_user_lang(str(chat_id_val)) or "en"
        if action=="get_key_points":
            instruction=f"What is this report and what is it about? Please summarize them for me into (lang={lang}) without adding any introductions, notes, or extra phrases."
            result_text=ask_gemini(stored,instruction) if GEMINI_API_KEY else extract_key_points_offline(stored,max_points=6)
        else:
            instruction=f"Clean and normalize this transcription (lang={lang}). Remove ASR artifacts like [inaudible], repeated words, filler noises, timestamps, and incorrect punctuation. Produce a clean, well-punctuated, readable text in the same language. Do not add introductions or explanations."
            result_text=ask_gemini(stored,instruction) if GEMINI_API_KEY else normalize_text_offline(stored)
    except: result_text=""
    stop["stop"]=True; animation_thread.join()
    try: bot_obj.delete_message(call.message.chat.id,status_msg.message_id)
    except: pass
    if not result_text: bot_obj.send_message(call.message.chat.id,f"No {action} returned.",reply_to_message_id=call.message.message_id); return
    if action=="clean_up":
        send_transcribed_result(bot_obj,call.message,result_text)
    else:
        bot_obj.send_message(call.message.chat.id,f"{result_text}",reply_to_message_id=call.message.message_id)

def register_handlers(bot_obj,bot_token,bot_index):
    @bot_obj.message_handler(commands=['start'])
    def start_handler(message):
        update_user_activity(message.from_user.id)
        if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj): send_subscription_message(message.chat.id,bot_obj); return
        bot_obj.send_message(message.chat.id,"Choose your file language for transcription using the below buttons:",reply_markup=build_lang_keyboard("start_select_lang"))

    @bot_obj.callback_query_handler(func=lambda c:c.data and c.data.startswith("start_select_lang|"))
    def start_select_lang_callback(call):
        try:
            uid=str(call.from_user.id); _,lang_code=call.data.split("|",1); lang_label=CODE_TO_LABEL.get(lang_code,lang_code)
            set_stt_user_lang(uid,lang_code)
            try: bot_obj.delete_message(call.message.chat.id,call.message.message_id)
            except: pass
            bot_obj.send_message(call.message.chat.id,"👋 Salaam!    \n• Send me\n• voice message\n• audio file\n• video\n• to transcribe for free")
            bot_obj.answer_callback_query(call.id,f"✅ Language set to {lang_label}")
        except: bot_obj.answer_callback_query(call.id,"❌ Error setting language, try again.",show_alert=True)

    @bot_obj.message_handler(commands=['help'])
    def handle_help(message):
        update_user_activity(message.from_user.id)
        if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj): send_subscription_message(message.chat.id,bot_obj); return
        bot_obj.send_message(message.chat.id,"Commands supported:\n/start - Show welcome message\n/lang  - Change language\n/mode  - Change result delivery mode\n/help  - This help message\n\nSend a voice/audio/video (up to 20MB) and I will transcribe it Need help? Contact: @lakigithub")

    @bot_obj.message_handler(commands=['lang'])
    def handle_lang(message):
        if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj): send_subscription_message(message.chat.id,bot_obj); return
        bot_obj.send_message(message.chat.id,"Choose your file language for transcription using the below buttons:",reply_markup=build_lang_keyboard("stt_lang"))

    @bot_obj.message_handler(commands=['mode'])
    def handle_mode(message):
        if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj): send_subscription_message(message.chat.id,bot_obj); return
        current_mode=get_user_send_mode(str(message.from_user.id))
        mode_text="📄 .txt file" if current_mode=="file" else "💬 Split messages"
        bot_obj.send_message(message.chat.id,f"Result delivery mode: {mode_text}. Change it below:",reply_markup=build_result_mode_keyboard())

    @bot_obj.callback_query_handler(lambda c:c.data and c.data.startswith("stt_lang|"))
    def on_stt_language_select(call):
        try:
            uid=str(call.from_user.id); _,lang_code=call.data.split("|",1); lang_label=CODE_TO_LABEL.get(lang_code,lang_code)
            set_stt_user_lang(uid,lang_code)
            bot_obj.answer_callback_query(call.id,f"✅ Language set: {lang_label}")
            try: bot_obj.delete_message(call.message.chat.id,call.message.message_id)
            except: pass
        except: bot_obj.answer_callback_query(call.id,"❌ Error setting language, try again.",show_alert=True)

    @bot_obj.callback_query_handler(lambda c:c.data and c.data.startswith("result_mode|"))
    def on_result_mode_select(call):
        try:
            uid=str(call.from_user.id); _,mode=call.data.split("|",1)
            set_user_send_mode(uid,mode)
            mode_text="📄 .txt file" if mode=="file" else "💬 Split messages"
            try: bot_obj.delete_message(call.message.chat.id,call.message.message_id)
            except: pass
            bot_obj.answer_callback_query(call.id,f"✅ Result mode set: {mode_text}")
        except: bot_obj.answer_callback_query(call.id,"❌ Error setting result mode, try again.",show_alert=True)

    @bot_obj.message_handler(content_types=['new_chat_members'])
    def handle_new_chat_members(message):
        if message.new_chat_members[0].id==bot_obj.get_me().id:
            group_data={'_id':str(message.chat.id),'title':message.chat.title,'type':message.chat.type,'added_date':datetime.now()}
            groups_collection.update_one({'_id':group_data['_id']},{'$set':group_data},upsert=True)
            bot_obj.send_message(message.chat.id,"Thanks for adding me! I'm ready to transcribe your media files.")

    @bot_obj.message_handler(content_types=['left_chat_member'])
    def handle_left_chat_member(message):
        if message.left_chat_member.id==bot_obj.get_me().id:
            groups_collection.delete_one({'_id':str(message.chat.id)})

    @bot_obj.message_handler(content_types=['voice','audio','video','document'])
    def handle_media_types(message):
        handle_media_common(message,bot_obj,bot_token,bot_index)

    @bot_obj.message_handler(content_types=['text'])
    def handle_text_messages(message):
        if message.chat.type=='private' and not check_subscription(message.from_user.id,bot_obj): send_subscription_message(message.chat.id,bot_obj); return
        bot_obj.send_message(message.chat.id,"For Text to Audio Use: @TextToSpeechBBot")

    @bot_obj.callback_query_handler(lambda c:c.data and c.data.startswith("get_key_points|"))
    def get_key_points_callback(call):
        handle_action_callback(call,bot_obj,"get_key_points")

    @bot_obj.callback_query_handler(lambda c:c.data and c.data.startswith("clean_up|"))
    def clean_up_callback(call):
        handle_action_callback(call,bot_obj,"clean_up")

for idx,bot_obj in enumerate(bots): register_handlers(bot_obj,BOT_TOKENS[idx],idx)

@app.route("/",methods=["GET","POST","HEAD"])
def webhook_root():
    if request.method in ("GET","HEAD"):
        bot_index=request.args.get("bot_index")
        try: bot_index_val=int(bot_index) if bot_index is not None else 0
        except: bot_index_val=0
        return jsonify({"status":"ok","time":datetime.utcnow().isoformat()+"Z","bot_index":bot_index_val}),200
    if request.method=="POST":
        content_type=request.headers.get("Content-Type","")
        if not content_type or not content_type.startswith("application/json"): return abort(403)
        try: payload=json.loads(request.get_data().decode("utf-8"))
        except: payload=None
        bot_index=request.args.get("bot_index")
        if not bot_index and isinstance(payload,dict): bot_index=payload.get("bot_index")
        header_idx=request.headers.get("X-Bot-Index")
        if header_idx: bot_index=header_idx
        try: bot_index_val=int(bot_index) if bot_index is not None else 0
        except: bot_index_val=0
        if bot_index_val<0 or bot_index_val>=len(bots): return abort(404)
        try:
            bots[bot_index_val].process_new_updates([telebot.types.Update.de_json(payload)])
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
            results.append({"index":idx,"error":str(e)})
    return jsonify({"results":results}),200

@app.route("/delete_webhook",methods=["GET","POST"])
def delete_webhook_route():
    results=[]
    for idx,bot_obj in enumerate(bots):
        try: bot_obj.delete_webhook(); results.append({"index":idx,"status":"deleted"})
        except Exception as e: results.append({"index":idx,"error":str(e)})
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

if __name__=="__main__":
    try:
        set_webhook_on_startup()
        try:
            client.admin.command('ping'); logging.info("Successfully connected to MongoDB!")
        except:
            logging.error("Could not connect to MongoDB!")
    except:
        logging.exception("Failed during startup")
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8080)))
