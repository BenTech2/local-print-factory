"""Runtime configuration, all from environment variables."""
import os
from pathlib import Path

VERSION = "1.3.1"

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

# Inside a container the data folder must be a mounted volume, or everything is lost on the next redeploy.
IN_CONTAINER = os.environ.get("LF_IN_CONTAINER") == "1" or Path("/.dockerenv").exists()
DATA_PERSISTENT = not IN_CONTAINER or os.path.ismount(DATA_DIR)
