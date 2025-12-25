# MediaToTextBot

A powerful Telegram bot that converts voice, audio and video files to text using Google Web Speech API and designed for easy deployment.


---

## 🚀 Features

### 🔀 Multi-format Support
Handles:
- Voice messages  
- Audio files: MP3, WAV, M4A, OGG, etc.  
- Video files: MP4, MKV, AVI, etc.

### 🌐 Multi-language Transcription
Supports 50+ languages including English, Arabic, Spanish, French, Russian and more. Language selection is available via an inline keyboard to boost recognition accuracy.

### 🧩 Smart Chunk Processing
- Splits long audio into manageable chunks  
- Adds overlap and silence padding to avoid missing words  
- Parallel processing with a configurable worker pool

### 📝 Dual Output Modes
- Split long transcripts into multiple Telegram messages  
- Or deliver a single downloadable `.txt` file for long outputs

### 🔁 Gemini Integration (Optional)
- Rotate multiple Gemini API keys for summarization and text processing  
- Summarize transcripts: short, detailed, or bulleted formats

---

## ✅ Quick Start

1. Clone the repo:
   ```bash
   git clone <your-repo-url>
   cd <repo-directory>

	2.	Create virtual environment and install:

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt


	3.	Ensure ffmpeg is installed and available in PATH.
	4.	Set environment variables:
	•	BOT_TOKEN — Telegram bot token (required)
	•	WEBHOOK_URL_BASE — Public HTTPS base URL for webhook (required)
	•	WEBHOOK_PATH — Webhook path (default: /webhook/)
	•	PORT — Port to run Flask app (default: 8080)
	•	MAX_UPLOAD_MB — Max upload size in MB (default: 20)
	•	DOWNLOADS_DIR — Temp download folder (default: ./downloads)
	•	GEMINI_KEY / GEMINI_KEYS — Gemini API key(s) for summarize features (optional)
	5.	Run locally (example using ngrok):

export BOT_TOKEN="xxx"
export WEBHOOK_URL_BASE="https://<your-ngrok>.ngrok.io"
export WEBHOOK_PATH="/webhook/"
python main.py


	6.	Production with gunicorn:

BOT_TOKEN="xxx" WEBHOOK_URL_BASE="https://your-domain.com" \
gunicorn "main:flask_app" --bind 0.0.0.0:8080 --workers 1



⸻

⚙ Configuration (common env vars)
	•	REQUEST_TIMEOUT — HTTP timeout in seconds (default 300)
	•	MAX_WORKERS — Workers for chunk transcription (default 3)
	•	CHUNK_SECONDS — Seconds per chunk (default 293)
	•	CHUNK_OVERLAP — Overlap seconds between chunks (default 1.0)
	•	SILENCE_PADDING — Extra silence appended to chunks (default 5)

⸻

🛠 Notes & Troubleshooting
	•	If you see Webhook URL not set, exiting. — set WEBHOOK_URL_BASE and WEBHOOK_PATH.
	•	Install ffmpeg (macOS: brew install ffmpeg, Ubuntu: sudo apt install ffmpeg).
	•	For blank transcriptions: confirm language selection or tweak SILENCE_PADDING / CHUNK_SECONDS.
	•	Do not commit BOT_TOKEN or API keys; use environment variables or secret manager.

⸻

🔒 Privacy

Audio files are stored temporarily in DOWNLOADS_DIR and removed after processing where possible. Do not log or expose tokens or user audio.

⸻

🤝 Contributing
	•	Open issues or PRs.
	•	Keep changes focused and avoid persistent storage unless necessary.

⸻





