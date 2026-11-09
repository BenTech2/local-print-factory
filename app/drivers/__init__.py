"""Printer drivers, one per family. KINDS drives the 'Add printer' form."""
from .base import Driver, PrinterError, norm_model  # noqa: F401
from .moonraker import MoonrakerDriver
from .sdcp import SdcpDriver
from .ultimaker import UltimakerDriver

DRIVERS = {d.kind: d for d in (UltimakerDriver, MoonrakerDriver, SdcpDriver)}

# What the user picks when adding a printer -> (driver kind, model name)
KINDS = [
    {"id": "ultimaker", "label": "Ultimaker (UM3, S3, S5, S7...)", "kind": "ultimaker", "model": None},
    {"id": "neptune4", "label": "Elegoo Neptune 4", "kind": "moonraker", "model": "Elegoo Neptune 4"},
    {"id": "neptune4pro", "label": "Elegoo Neptune 4 Pro", "kind": "moonraker", "model": "Elegoo Neptune 4 Pro"},
    {"id": "neptune4plus", "label": "Elegoo Neptune 4 Plus", "kind": "moonraker", "model": "Elegoo Neptune 4 Plus"},
    {"id": "neptune4max", "label": "Elegoo Neptune 4 Max", "kind": "moonraker", "model": "Elegoo Neptune 4 Max"},
    {"id": "centauri", "label": "Elegoo Centauri Carbon", "kind": "sdcp", "model": "Elegoo Centauri Carbon"},
    {"id": "klipper", "label": "Other Klipper / Moonraker printer", "kind": "moonraker", "model": None},
]


def make_driver(p: dict) -> Driver:
    return DRIVERS.get(p.get("kind") or "ultimaker", UltimakerDriver)(p)
