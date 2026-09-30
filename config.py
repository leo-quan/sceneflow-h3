from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
COMFY_ROOT = BASE_DIR.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
VIDEO_DIR = BASE_DIR / "videos"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "app.db"

COMFY_HTTP = "http://127.0.0.1:8188"
COMFY_WS = "ws://127.0.0.1:8188/ws"
COMFY_INPUT_DIR = COMFY_ROOT / "input" / "webapp"
COMFY_OUTPUT_DIR = COMFY_ROOT / "output"

for directory in (DATA_DIR, UPLOAD_DIR, VIDEO_DIR, COMFY_INPUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)
