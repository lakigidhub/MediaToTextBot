# MediaToTextBot

**A powerful Telegram bot that converts audio and video files to text using advanced speech recognition technology.**

---

## 🚀 Features

### 🔊 Multi-format Support
Handles:
- Voice messages  
- Audio files: `MP3`, `WAV`, `M4A`, `OGG`, etc.  
- Video files: `MP4`, `MKV`, `AVI`, etc.

### 🌍 Multi-language Transcription
Supports **50+ languages**, including:
> English, Arabic, Spanish, French, Russian, and more!

### 🧩 Smart Chunk Processing
- Splits long audio into manageable chunks  
- Adds overlap to avoid missing words

### 📝 Dual Output Modes
- 📄 **Text file** download for long transcripts  
- 💬 **Split messages** for quick reading

### 🧹 Post-processing Options
- ⭐ **Clean transcript** (removes artifacts, fixes punctuation)  
- 📋 **Summarized key points**

### 🤖 Multi-bot Support
- Run **multiple bot instances** with load balancing

### 👥 User Management
- Track user activity & preferences

### 💎 Subscription System
- Optional **channel subscription** before usage

---

## ⚙️ Setup Instructions

### 🔧 Prerequisites
1. Python **3.7+** and **pip**  
2. **FFmpeg** installed and accessible in PATH  
3. Telegram Bot Token(s) from **@BotFather**  
4. Optional: **MongoDB** for persistent user data

---

## 🧾 Environment Variables

Create a `.env` file with the following:

```env
# Required
BOT_TOKENS=your_bot_token_1,your_bot_token_2
SECRET_KEY=your_secret_key_here

# Optional
MONGO_URI=mongodb://localhost:27017/
DB_NAME=telegram_bot_db
WEBHOOK_BASE=https://your-domain.com
GEMINI_API_KEY=your_gemini_api_key
REQUIRED_CHANNEL=@your_channel

# Audio Processing
CHUNK_DURATION_SEC=40
CHUNK_OVERLAP_SEC=1.0
AUDIO_SAMPLE_RATE=8000
PREPEND_SILENCE_SEC=10
AMBIENT_CALIB_SEC=0.5
# Performance
TRANSCRIBE_MAX_WORKERS=6
MAX_PENDING_QUEUE=3
MAX_CONCURRENT_TRANSCRIPTS=2
```

---

## 🧰 Installation

```bash
git clone <repository-url>
cd MediaToTextBot
pip install -r requirements.txt
```

### Install FFmpeg
- Ubuntu/Debian → `sudo apt install ffmpeg`  
- Windows → Download from [ffmpeg.org](https://ffmpeg.org)  
- macOS → `brew install ffmpeg`

### Run the Bot
```bash
python bot.py
```

---

## ☁️ Deployment Options

### 🔹 Option 1: Local Development
```bash
python bot.py
```

### 🔹 Option 2: Production (Webhooks)
```bash
curl -X POST https://your-domain.com/set_webhook
gunicorn -w 4 -b 0.0.0.0:8080 bot:app
```

### 🔹 Option 3: Docker
```dockerfile
FROM python:3.9-slim
RUN apt-get update && apt-get install -y ffmpeg
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
CMD ["python", "bot.py"]
```

---

## 💬 Usage

### Basic Commands
- `/start` — Initialize bot  
- `/lang` — Change language  
- `/mode` — Switch output mode  
- `/help` — Show help info

### Supported File Types
**Audio:** MP3, WAV, M4A, OGG, FLAC, OPUS, etc.  
**Video:** MP4, MKV, AVI, MOV, FLV, 3GP, etc.

### Supported Languages
🇬🇧 English • 🇸🇦 العربية • 🇪🇸 Español • 🇫🇷 Français  
🇷🇺 Русский • 🇩🇪 Deutsch • 🇮🇳 हिन्दी • 🇮🇷 فارسی  
🇯🇵 日本語 • 🇰🇷 한국어 • 🇨🇳 中文 • + many more...

---

## 🧠 Technical Architecture

1. **Audio Processing Pipeline**
   - Converts to WAV  
   - Splits audio with overlap  
   - Adds silence for clarity  
2. **Speech Recognition**
   - Uses Google Speech API  
   - Retry & parallel processing  
3. **Text Processing**
   - Merges chunks, cleans text  
   - Optional AI-powered summaries  
4. **Queue Management**
   - Concurrency control  
   - Thread pool workers

---

## ⚡ Performance Optimization
- Chunk-based long audio handling  
- Parallel transcription  
- Memory-efficient file management  
- API connection pooling

---

## 🌐 API Endpoints

| Method | Endpoint | Description |
|:--|:--|:--|
| `GET` | `/` | Health check |
| `POST` | `/` | Telegram webhook updates |
| `POST` | `/set_webhook` | Configure webhooks |
| `POST` | `/delete_webhook` | Remove webhooks |

---

## 📊 Monitoring & Logging
- User activity tracking  
- Processing time metrics  
- Error reports & analytics  
- MongoDB integration

---

## 🧩 Troubleshooting

| Issue | Solution |
|:--|:--|
| **FFmpeg not found** | Add to PATH or set `FFMPEG_BINARY` |
| **Large files** | Default limit: 20 MB → adjust `TELEGRAM_MAX_BYTES` |
| **Recognition errors** | Improve audio quality / check language |

---

## ⚙️ Performance Tips
- Tune `CHUNK_DURATION_SEC` for optimal accuracy  
- Increase `TRANSCRIBE_MAX_WORKERS` for faster runs  
- Use `CHUNK_BATCH_PAUSE_SEC` to avoid API rate limits

---

## 🤝 Contributing
1. Fork the repo  
2. Create a feature branch  
3. Submit a PR with tests  

---

## 💬 Support
- Open an issue on GitHub  
- Telegram: [@lakigithub](https://t.me/lakigithub)

---

> 🎧 **MediaToTextBot** — Making audio content accessible through accurate transcription technology.
