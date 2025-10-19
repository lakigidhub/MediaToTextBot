#!/usr/bin/env python3
from dotenv import load_dotenv
load_dotenv()

import os
import sys
import logging
import time

os.environ['WEBHOOK_BASE'] = ''

logging.basicConfig(level=logging.INFO)

try:
    import app
    bots = app.bots
except:
    print("Failed to import app.py")
    sys.exit(1)

if not bots:
    print("No bots found")
    sys.exit(1)

print(f"Starting polling for {len(bots)} bot(s)")

for bot in bots:
    try:
        bot.delete_webhook()
        time.sleep(0.3)
    except:
        pass

print("Polling started. Press Ctrl+C to stop\n")

try:
    if len(bots) == 1:
        # Single bot - run directly
        bots[0].infinity_polling(timeout=60, long_polling_timeout=60)
    else:
        # Multiple bots - run in threads
        import threading
        threads = []
        for idx, bot in enumerate(bots):
            t = threading.Thread(target=lambda b=bot, i=idx: b.infinity_polling(timeout=60, long_polling_timeout=60))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
except KeyboardInterrupt:
    print("\nStopped")

