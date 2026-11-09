"""Runtime configuration, all from environment variables."""
import os
from pathlib import Path

VERSION = "1.3.0"

DATA_DIR = Path(os.environ.get("LF_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
FILES_DIR = DATA_DIR / "files"
THUMBS_DIR = DATA_DIR / "thumbs"
DB_PATH = DATA_DIR / "localfactory.db"
HOTFOLDER_DIR = Path(os.environ.get("LF_HOTFOLDER", DATA_DIR / "hotfolder"))

# Seconds between status polls of each printer.
POLL_INTERVAL = float(os.environ.get("LF_POLL_INTERVAL", "3"))
# Optional password for the web UI / API. Empty = no login (trusted LAN only).
APP_PASSWORD = os.environ.get("LF_PASSWORD", "")
# Name shown on the printer's screen when pairing.
APP_NAME = os.environ.get("LF_APP_NAME", "Local Print Factory")

for d in (DATA_DIR, FILES_DIR, THUMBS_DIR, HOTFOLDER_DIR):
    d.mkdir(parents=True, exist_ok=True)
