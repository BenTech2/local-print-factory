"""Client for the Ultimaker printer's built-in local HTTP API (http://<printer>/api/v1).

GET requests are open. PUT/POST/DELETE need HTTP Digest credentials, which the
printer issues after someone approves the request on the printer's touchscreen.
Applies to Ultimaker 3 / 3 Extended, S3, S5, S7 and later S-series firmware.
"""
import httpx

from .errors import PrinterError

TIMEOUT = httpx.Timeout(6.0, connect=4.0)
UPLOAD_TIMEOUT = httpx.Timeout(600.0, connect=6.0)


class UltimakerClient:
    def __init__(self, host, auth_id=None, auth_key=None):
        self.host = host.strip().rstrip("/")
        if self.host.startswith("http://") or self.host.startswith("https://"):
            self.root = self.host
        else:
            self.root = f"http://{self.host}"
        self.base = f"{self.root}/api/v1"
        self.auth = httpx.DigestAuth(auth_id, auth_key) if auth_id and auth_key else None
        self._http = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)

    @property
    def paired(self):
        return self.auth is not None

    async def close(self):
        await self._http.aclose()

    async def _req(self, method, path, *, auth=False, **kw):
        if auth and not self.auth:
            raise PrinterError("Printer is not paired - pair it first to control it.")
        try:
            r = await self._http.request(method, self.base + path,
                                         auth=self.auth if auth else None, **kw)
        except httpx.HTTPError as e:
            raise PrinterError(f"Cannot reach printer: {e.__class__.__name__}") from e
        if r.status_code == 401:
            raise PrinterError("Printer rejected our credentials (401) - re-pair the printer.")
        return r

    async def _json(self, path):
        r = await self._req("GET", path)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise PrinterError(f"GET {path} -> HTTP {r.status_code}")
        return r.json()

    # ---- read-only -------------------------------------------------------
    async def system(self):
        return await self._json("/system")

    async def printer(self):
        return await self._json("/printer")

    async def print_job(self):
        """Current job, or None when the printer has no job."""
        return await self._json("/print_job")

    async def history(self, count=50):
        return await self._json(f"/history/print_jobs?count={count}") or []

    async def history_job(self, uuid):
        return await self._json(f"/history/print_jobs/{uuid}")

    async def material_xml(self, guid):
        r = await self._req("GET", f"/materials/{guid}")
        return r.text if r.status_code == 200 else None

    async def snapshot(self):
        r = await self._req("GET", "/camera/0/snapshot")
        if r.status_code != 200:
            raise PrinterError(f"No camera snapshot (HTTP {r.status_code})")
        return r.content, r.headers.get("content-type", "image/jpeg")

    def stream_request(self):
        """Returns an async context manager streaming the MJPEG feed."""
        return self._http.stream("GET", self.base + "/camera/0/stream",
                                 timeout=httpx.Timeout(None, connect=4.0))

    # ---- pairing ---------------------------------------------------------
    async def request_auth(self, application, user):
        r = await self._req("POST", "/auth/request", data={"application": application, "user": user})
        if r.status_code >= 400:
            raise PrinterError(f"Pairing request failed (HTTP {r.status_code})")
        return r.json()  # {"id": ..., "key": ...}

    async def check_auth(self, auth_id):
        d = await self._json(f"/auth/check/{auth_id}")
        return (d or {}).get("message", "unknown")  # authorized | unauthorized | unknown

    async def verify(self):
        r = await self._req("GET", "/auth/verify", auth=True)
        return r.status_code == 200

    # ---- control (digest auth) ------------------------------------------
    async def set_job_state(self, target):
        if target not in ("print", "pause", "abort"):
            raise ValueError(target)
        r = await self._req("PUT", "/print_job/state", auth=True, json=target)
        if r.status_code >= 400:
            raise PrinterError(f"Printer refused '{target}' (HTTP {r.status_code}): {r.text[:200]}")

    async def set_led(self, hue=None, saturation=None, brightness=None):
        body = {k: v for k, v in (("hue", hue), ("saturation", saturation), ("brightness", brightness))
                if v is not None}
        r = await self._req("PUT", "/printer/led", auth=True, json=body)
        if r.status_code >= 400:
            raise PrinterError(f"LED change refused (HTTP {r.status_code})")

    async def blink(self, frequency=2.0, count=6):
        r = await self._req("POST", "/printer/led/blink", auth=True,
                            json={"frequency": frequency, "count": count})
        if r.status_code >= 400:
            raise PrinterError(f"Blink refused (HTTP {r.status_code})")

    async def display_message(self, message, button_caption="OK"):
        r = await self._req("PUT", "/system/display_message", auth=True,
                            json={"message": message, "button_caption": button_caption})
        if r.status_code >= 400:
            raise PrinterError(f"Message refused (HTTP {r.status_code}) - printer may be busy")

    async def upload(self, jobname, filename, data: bytes):
        """Send a .ufp / .gcode / .gcode.gz file and start printing it."""
        if not self.auth:
            raise PrinterError("Printer is not paired.")
        # Prime the digest nonce with a cheap request so the large body is sent once.
        await self.verify()
        try:
            r = await self._http.post(
                self.base + "/print_job", auth=self.auth, timeout=UPLOAD_TIMEOUT,
                data={"jobname": jobname},
                files={"file": (filename, data, "application/octet-stream")})
        except httpx.HTTPError as e:
            raise PrinterError(f"Upload failed: {e.__class__.__name__}") from e
        if r.status_code == 405:
            raise PrinterError("Printer is busy (405) - it already has a job or needs the build plate cleared.")
        if r.status_code >= 400:
            raise PrinterError(f"Printer refused the file (HTTP {r.status_code}): {r.text[:200]}")
        try:
            return r.json()
        except ValueError:
            return {}
