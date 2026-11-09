"""Klipper printers through the Moonraker API - Elegoo Neptune 4 / 4 Pro / 4 Plus / 4 Max.

On stock Neptune 4 firmware Moonraker is reached through Fluidd's web server on port 80
(http://<ip>/printer/...). Some setups expose it directly on :7125 instead; both work.
"""
import time
from urllib.parse import quote, urlsplit

import httpx

from .base import Driver, PrinterError, grab_mjpeg_frame, safe_remote_name

TIMEOUT = httpx.Timeout(6.0, connect=4.0)
QUERY = "/printer/objects/query?webhooks&print_stats&virtual_sdcard&display_status&extruder&heater_bed"
STATE_MAP = {"printing": "printing", "paused": "paused"}
RESULT_MAP = {"complete": "Finished", "cancelled": "Aborted", "error": "Failed"}
HISTORY_RESULT = {"completed": "Finished", "cancelled": "Aborted", "error": "Failed",
                  "klippy_shutdown": "Failed", "klippy_disconnect": "Failed", "server_exit": "Failed"}


class MoonrakerDriver(Driver):
    kind = "moonraker"
    label = "Klipper / Moonraker"
    caps = frozenset({"pause", "resume", "abort", "camera", "history", "clear"})
    extensions = (".gcode",)
    manual_clear = True

    def __init__(self, p):
        super().__init__(p)
        self.base = self.host if self.host.startswith("http") else f"http://{self.host}"
        headers = {"X-Api-Key": p["auth_key"]} if p.get("auth_key") else {}
        self.h = httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=True)
        self._info = None
        self._tick = 0
        self._estimates: dict[str, float] = {}
        self._finished_key = None   # unique id of the print that just ended (Moonraker history job_id)

    async def close(self):
        await self.h.aclose()

    async def _req(self, method, path, **kw):
        try:
            r = await self.h.request(method, self.base + path, **kw)
        except httpx.HTTPError as e:
            raise PrinterError(f"Cannot reach printer: {e.__class__.__name__}") from e
        if r.status_code in (401, 403):
            raise PrinterError("Moonraker refused access - add this server to trusted_clients or set an API key.")
        return r

    async def _get(self, path):
        r = await self._req("GET", path)
        if r.status_code >= 400:
            raise PrinterError(f"GET {path.split('?')[0]} -> HTTP {r.status_code}")
        return r.json().get("result", {})

    async def _post(self, path):
        r = await self._req("POST", path)
        if r.status_code >= 400:
            raise PrinterError(f"Printer refused ({r.status_code}): {_err(r)}")

    async def _estimate(self, filename):
        if filename in self._estimates:
            return self._estimates[filename]
        est = 0
        try:
            meta = await self._get(f"/server/files/metadata?filename={quote(filename)}")
            est = float(meta.get("estimated_time") or 0)
        except (PrinterError, ValueError):
            pass
        self._estimates[filename] = est
        return est

    async def poll(self):
        if self._info is None or self._tick % 20 == 0:
            self._info = await self._get("/printer/info")
        self._tick += 1
        klippy = self._info.get("state")
        try:
            st = (await self._get(QUERY)).get("status", {})
        except PrinterError:
            if klippy != "ready":
                st = {}
            else:
                raise
        wh = st.get("webhooks") or {}
        klippy = wh.get("state", klippy)
        ps = st.get("print_stats") or {}
        ext = st.get("extruder") or {}
        bed = st.get("heater_bed") or {}

        job = None
        state = ps.get("state") or "standby"
        fname = ps.get("filename") or ""
        if state not in RESULT_MAP:
            self._finished_key = None
        elif self._finished_key is None:
            self._finished_key = await self._latest_job_key(fname, ps)
        if fname and (state in STATE_MAP or state in RESULT_MAP):
            progress = (st.get("display_status") or {}).get("progress") or \
                (st.get("virtual_sdcard") or {}).get("progress") or 0
            elapsed = ps.get("print_duration") or 0
            total = await self._estimate(fname)
            if not total and progress > 0.03:
                total = elapsed / progress
            if state in RESULT_MAP:
                total = max(total, elapsed)
            job = {"name": fname.rsplit("/", 1)[-1].removesuffix(".gcode"), "uuid": fname,
                   "state": STATE_MAP.get(state, "wait_cleanup"), "progress": progress,
                   "time_total": total, "time_elapsed": elapsed, "remaining": max(0, total - elapsed),
                   "result": RESULT_MAP.get(state), "source": "moonraker",
                   "pause_source": "printer" if state == "paused" else None,
                   "clear_key": self._finished_key}
        if klippy in ("shutdown", "error"):
            status = "error"
        elif klippy == "startup":
            status = "booting"
        else:
            status = "printing" if job and job["state"] in STATE_MAP.values() else "idle"
        return {
            "status": status, "variant": self.p.get("model") or "Klipper printer",
            "match_model": self.p.get("model"),
            "firmware": self._info.get("software_version", "").split("-")[0] or None,
            "machine_name": self._info.get("hostname"),
            "error_detail": wh.get("state_message") if status == "error" else None,
            "extruders": [{"index": 0, "hotend": "Nozzle", "temp": ext.get("temperature"),
                           "target": ext.get("target"), "material_guid": "", "material": None,
                           "remaining_mm": None}],
            "bed": {"temp": bed.get("temperature"), "target": bed.get("target")},
            "job": job,
        }

    async def _latest_job_key(self, fname, ps):
        """Same file printed twice looks identical in print_stats, so key finished prints on history job_id."""
        try:
            jobs = (await self._get("/server/history/list?limit=1&order=desc")).get("jobs", [])
            if jobs and jobs[0].get("filename") == fname:
                return f"job:{jobs[0].get('job_id')}"
        except PrinterError:
            pass
        return f"{fname}|{ps.get('total_duration')}"

    async def pause(self):
        await self._post("/printer/print/pause")

    async def resume(self):
        await self._post("/printer/print/resume")

    async def abort(self):
        await self._post("/printer/print/cancel")

    async def upload_and_print(self, jobname, filename, data):
        if not filename.lower().endswith(".gcode"):
            raise PrinterError("Klipper printers need a plain .gcode file")
        remote = safe_remote_name(jobname)
        try:
            r = await self.h.post(self.base + "/server/files/upload", timeout=httpx.Timeout(600, connect=6),
                                  data={"root": "gcodes", "print": "true"},
                                  files={"file": (remote, data, "application/octet-stream")})
        except httpx.HTTPError as e:
            raise PrinterError(f"Upload failed: {e.__class__.__name__}") from e
        if r.status_code >= 400:
            raise PrinterError(f"Printer refused the file ({r.status_code}): {_err(r)}")
        self._estimates.pop(remote, None)
        return remote

    async def job_result(self, remote_id):
        jobs = (await self._get("/server/history/list?limit=30&order=desc")).get("jobs", [])
        for j in jobs:
            if j.get("filename") == remote_id:
                return HISTORY_RESULT.get(j.get("status"))
        return None

    async def history(self):
        jobs = (await self._get("/server/history/list?limit=50&order=desc")).get("jobs", [])
        return [{"name": j.get("filename"), "result": HISTORY_RESULT.get(j.get("status"), j.get("status")),
                 "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(j.get("start_time") or 0)),
                 "duration": j.get("print_duration")} for j in jobs]

    async def snapshot(self):
        root = urlsplit(self.base)
        web_root = f"{root.scheme}://{root.hostname}"   # webcam lives behind port 80, not :7125
        urls = []
        try:
            cams = (await self._get("/server/webcams/list")).get("webcams", [])
            for c in cams:
                u = c.get("snapshot_url") or c.get("stream_url")
                if u:
                    urls.append(u if u.startswith("http") else web_root + ("" if u.startswith("/") else "/") + u)
        except PrinterError:
            pass
        urls += [f"{web_root}/webcam/?action=snapshot", f"{web_root}:8080/?action=snapshot"]
        last = None
        for u in urls:
            try:
                return await grab_mjpeg_frame(u, timeout=5), "image/jpeg"
            except PrinterError as e:
                last = e
        raise last or PrinterError("No camera found")


def _err(r):
    try:
        return str((r.json().get("error") or {}).get("message"))[:200]
    except ValueError:
        return r.text[:200]


async def probe(host: str):
    """Find where Moonraker answers for this host. Returns (host_to_store, info) or raises."""
    h = host.strip().rstrip("/")
    candidates = [h] if ":" in h.split("//")[-1] else [h, f"{h}:7125"]
    async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=3)) as c:
        for cand in candidates:
            base = cand if cand.startswith("http") else f"http://{cand}"
            try:
                r = await c.get(base + "/server/info")
                if r.status_code == 200 and "result" in r.json():
                    return cand, r.json()["result"]
            except (httpx.HTTPError, ValueError):
                continue
    raise PrinterError(f"No Moonraker/Klipper API found at {host} (tried port 80 and 7125)")
