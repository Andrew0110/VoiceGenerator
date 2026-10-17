"""Open the studio only after its local server is ready."""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
URL = "http://127.0.0.1:8765"


def studio_available():
    try:
        with urllib.request.urlopen(URL + "/api/status", timeout=.5) as response:
            return json.load(response).get("model") == "OmniVoice"
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    if studio_available():
        webbrowser.open(URL)
        print("Студія вже запущена. Відкриваю її у браузері.")
    else:
        def open_when_ready():
            for _ in range(75):
                if studio_available():
                    webbrowser.open(URL)
                    return
                time.sleep(.2)

        threading.Thread(target=open_when_ready, daemon=True).start()
        import uvicorn
        uvicorn.run("voice_studio.app:create_app", factory=True, host="127.0.0.1", port=8765)
