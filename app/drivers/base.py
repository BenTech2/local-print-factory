"""Common interface every printer family implements.

A driver's poll() returns a normalised state dict:
    status:   idle | printing | error | maintenance | booting
    variant:  printer model ("Ultimaker S5", "Elegoo Neptune 4 Pro", "Centauri Carbon")
    firmware, machine_name
    extruders: [{index, hotend, temp, target, material_guid, material, remaining_mm}]
    bed:      {temp, target}
    job:      None or {name, uuid, state, progress, time_total, time_elapsed, remaining,
                       result, pause_source, source}
              state is one of: pre_print | printing | pausing | paused | resuming |
                               wait_user_action | post_print | wait_cleanup
              result (only once ended): Finished | Aborted | Failed
"""
import re

import httpx

from ..errors import PrinterError  # noqa: F401  (re-exported)


def norm_model(name: str | None) -> str:
    """'ELEGOO NEPTUNE 4 PRO' / 'Elegoo Neptune 4 Pro' -> 'neptune4pro'; 'Ultimaker S5' -> 's5'."""
    if not name:
        return ""
    s = name.lower()
    for brand in ("ultimaker", "elegoo"):
        s = s.replace(brand, "")
    return re.sub(r"[^a-z0-9]", "", s)


class Driver:
    kind = "base"
    label = "Printer"
    # Which actions the UI may offer.
    caps: frozenset = frozenset()
    # File types this printer can print.
    extensions: tuple = (".gcode",)
    # True when the printer itself does not make someone confirm the bed is clear
    # after a print - Local Print Factory then holds the printer until "Plate is clear" is clicked.
    manual_clear = False
    needs_pairing = False

    def __init__(self, p: dict):
        self.p = p
        self.host = p["host"].strip().rstrip("/")

    @property
    def paired(self):
        return True

    async def close(self):
        pass

    async def poll(self) -> dict:
        raise NotImplementedError

    async def pause(self):
        raise PrinterError("Not supported on this printer")

    async def resume(self):
        raise PrinterError("Not supported on this printer")

    async def abort(self):
        raise PrinterError("Not supported on this printer")

    async def upload_and_print(self, jobname: str, filename: str, data: bytes) -> str | None:
        """Send the file, start it, return an id used to recognise the job later."""
        raise NotImplementedError

    async def job_result(self, remote_id: str) -> str | None:
        """Final result (Finished/Aborted/Failed) of a past job, if the printer remembers it."""
        return None

    async def history(self) -> list:
        return []

    async def snapshot(self) -> tuple[bytes, str]:
        raise PrinterError("No camera on this printer")


async def grab_mjpeg_frame(url: str, timeout=8.0) -> bytes:
    """Read one JPEG frame from an MJPEG stream (or return the body if it is a plain image)."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=4.0), follow_redirects=True) as h:
        try:
            async with h.stream("GET", url) as r:
                if r.status_code != 200:
                    raise PrinterError(f"Camera returned HTTP {r.status_code}")
                ctype = r.headers.get("content-type", "")
                buf = b""
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if "multipart" not in ctype:
                        continue
                    start = buf.find(b"\xff\xd8")
                    end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                    if start >= 0 and end > 0:
                        return buf[start:end + 2]
                    if len(buf) > 8_000_000:
                        break
                if "multipart" not in ctype and buf:
                    return buf
        except httpx.HTTPError as e:
            raise PrinterError(f"Camera not reachable: {e.__class__.__name__}") from e
    raise PrinterError("No image received from camera")


def safe_remote_name(jobname: str, ext=".gcode") -> str:
    base = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", jobname).strip() or "job"
    return base[:80] + ext
