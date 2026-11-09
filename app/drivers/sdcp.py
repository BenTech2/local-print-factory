"""Elegoo Centauri Carbon via SDCP v3 (Elegoo's Smart Device Control Protocol).

  control  ws://<ip>:3030/websocket   JSON envelopes, one persistent connection per printer
                                      (the printer only allows ~5 clients, so we never open more than one)
  upload   http://<ip>/uploadFile/upload   multipart, 1 MiB chunks, whole-file MD5 on each chunk
  camera   http://<ip>:3031/video     MJPEG; also one shared connection, kept open only while viewed
  discover UDP "M99999" -> <ip>:3000

Note: the Centauri Carbon 2 uses a different (MQTT) protocol and is not covered here.
"""
import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
import uuid

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect

from .base import Driver, PrinterError, safe_remote_name

log = logging.getLogger("sdcp")
WS_PORT = int(os.environ.get("LF_SDCP_WS_PORT", "3030"))
HTTP_PORT = int(os.environ.get("LF_SDCP_HTTP_PORT", "80"))
CAM_PORT = int(os.environ.get("LF_SDCP_CAM_PORT", "3031"))
UDP_PORT = int(os.environ.get("LF_SDCP_UDP_PORT", "3000"))
CHUNK = 1024 * 1024

CMD_STATUS, CMD_ATTRS, CMD_START, CMD_PAUSE, CMD_STOP, CMD_RESUME, CMD_VIDEO = 0, 1, 128, 129, 130, 131, 386
ACTIVE = {1: "pre_print", 15: "pre_print", 16: "pre_print", 18: "pre_print", 20: "pre_print",
          13: "printing", 5: "pausing", 6: "paused", 7: "printing"}
TERMINAL = {8: "Aborted", 9: "Finished", 14: "Failed"}
START_ACK = {1: "printer is busy", 2: "file not found on printer", 3: "MD5 check failed",
             4: "file could not be read", 5: "file resolution mismatch", 6: "unknown file format",
             7: "file was sliced for a different printer model"}


def _host_only(host):
    return host.split("//")[-1].split("/")[0].split(":")[0]


class _UdpProbe(asyncio.DatagramProtocol):
    def __init__(self):
        self.replies = []

    def datagram_received(self, data, addr):
        try:
            self.replies.append((addr[0], json.loads(data.decode(errors="replace"))))
        except ValueError:
            pass


async def udp_probe(target="255.255.255.255", wait=2.0):
    """Ask Elegoo SDCP printers to identify themselves. Returns [{host, name, model, mainboard, firmware}]."""
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        _UdpProbe, local_addr=("0.0.0.0", 0), allow_broadcast=True)
    try:
        for _ in range(3):
            transport.sendto(b"M99999", (target, UDP_PORT))
            await asyncio.sleep(wait / 3)
    finally:
        transport.close()
    out = {}
    for ip, msg in proto.replies:
        d = msg.get("Data") or {}
        d = d.get("Attributes", d)
        out[ip] = {"host": ip, "name": d.get("Name") or d.get("MachineName") or ip,
                   "model": d.get("MachineName"), "mainboard": d.get("MainboardID") or msg.get("Id"),
                   "firmware": d.get("FirmwareVersion")}
    return list(out.values())


class SdcpDriver(Driver):
    kind = "sdcp"
    label = "Elegoo (SDCP)"
    caps = frozenset({"pause", "resume", "abort", "camera", "clear"})
    extensions = (".gcode",)
    manual_clear = True

    def __init__(self, p):
        super().__init__(p)
        self.ip = _host_only(self.host)
        self.mainboard = p.get("auth_id") or None
        self.status = None
        self.attrs = None
        self.status_at = 0.0
        self.error = None
        self._ws = None
        self._task = None
        self._pending: dict[str, asyncio.Future] = {}
        self._cam_task = None
        self._cam_frame = None
        self._cam_wanted = 0.0

    # ---------------------------------------------------------------- connection
    async def close(self):
        for t in (self._task, self._cam_task):
            if t:
                t.cancel()

    def _ensure(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            try:
                if not self.mainboard:
                    found = await udp_probe(self.ip, 1.5)
                    if found:
                        self.mainboard = found[0]["mainboard"]
                async with ws_connect(f"ws://{self.ip}:{WS_PORT}/websocket", open_timeout=6,
                                      ping_interval=20, max_size=8_000_000) as ws:
                    self._ws, self.error = ws, None
                    if self.mainboard:
                        await self._send_raw(CMD_ATTRS, {})
                        await self._send_raw(CMD_STATUS, {})
                    async for raw in ws:
                        self._on_message(raw)
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                self.error = f"Cannot reach printer: {e.__class__.__name__}"
            except Exception as e:  # keep the connection loop alive whatever happens
                log.exception("sdcp loop")
                self.error = f"Connection error: {e}"
            self._ws = None
            for f in self._pending.values():
                if not f.done():
                    f.set_exception(PrinterError("Lost connection to printer"))
            self._pending.clear()
            await asyncio.sleep(3)

    def _on_message(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode(errors="replace")
        raw = raw[raw.find("{"):] if "{" in raw else raw
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        inner = msg.get("Data") if isinstance(msg.get("Data"), dict) else {}
        mb = msg.get("MainboardID") or inner.get("MainboardID")
        topic = msg.get("Topic", "")
        if "Attributes" in msg or "sdcp/attributes" in topic:
            self.attrs = msg.get("Attributes") or inner.get("Attributes") or inner
            mb = mb or (self.attrs or {}).get("MainboardID")
        if mb and not self.mainboard:
            self.mainboard = mb
            asyncio.create_task(self._send_raw(CMD_ATTRS, {}))
        if "Status" in msg or "sdcp/status" in topic:
            st = msg.get("Status") or inner.get("Status") or inner
            if isinstance(st, dict):
                self.status, self.status_at = st, time.time()
        elif "sdcp/response" in topic:
            rid = str(inner.get("RequestID", ""))
            fut = self._pending.pop(rid, None)
            if fut and not fut.done():
                fut.set_result(inner.get("Data") or {})

    async def _send_raw(self, cmd, data):
        if not self._ws or not self.mainboard:
            raise PrinterError(self.error or "Not connected to printer yet")
        rid = secrets.token_hex(8)
        env = {"Id": self.mainboard, "Topic": f"sdcp/request/{self.mainboard}",
               "Data": {"Cmd": cmd, "Data": data, "RequestID": rid, "MainboardID": self.mainboard,
                        "TimeStamp": int(time.time() * 1000), "From": 1}}
        await self._ws.send(json.dumps(env))
        return rid

    async def _command(self, cmd, data=None, timeout=10.0):
        self._ensure()
        rid = await self._send_raw(cmd, data or {})
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            res = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise PrinterError("Printer did not answer the command")
        return res

    # ---------------------------------------------------------------- status
    async def poll(self):
        self._ensure()
        if self._ws and self.mainboard:
            try:
                await self._send_raw(CMD_STATUS, {})
            except (PrinterError, websockets.WebSocketException):
                pass
        if not self.status:
            for _ in range(15):  # first contact: give it a moment
                await asyncio.sleep(0.2)
                if self.status:
                    break
        if not self._ws and time.time() - self.status_at > 10:
            raise PrinterError(self.error or "Cannot reach printer")
        if not self.status:
            raise PrinterError("Connected, but the printer has not reported its status yet")
        return self._normalise(self.status)

    def _normalise(self, s):
        pi = s.get("PrintInfo") or {}
        code = int(pi.get("Status") or 0)
        fname = (pi.get("Filename") or "").rsplit("/", 1)[-1]
        job = None
        if code in ACTIVE or (code in TERMINAL and fname):
            cur, tot = float(pi.get("CurrentTicks") or 0), float(pi.get("TotalTicks") or 0)
            if tot > 1_000_000:  # some firmware reports milliseconds
                cur, tot = cur / 1000, tot / 1000
            prog = pi.get("Progress")
            if prog is not None:
                prog = float(prog) / 100
            elif pi.get("TotalLayer"):
                prog = float(pi.get("CurrentLayer") or 0) / float(pi["TotalLayer"])
            else:
                prog = cur / tot if tot else 0
            if code == 9:
                prog = 1.0
            job = {"name": fname.removesuffix(".gcode"), "uuid": fname, "state": ACTIVE.get(code, "wait_cleanup"),
                   "progress": prog, "time_total": tot, "time_elapsed": cur, "remaining": max(0, tot - cur),
                   "result": TERMINAL.get(code), "source": "sdcp",
                   "pause_source": "printer" if code == 6 else None,
                   "clear_key": f"{fname}|{pi.get('TaskId') or ''}",
                   "layer": f"{pi.get('CurrentLayer', 0)}/{pi.get('TotalLayer', 0)}" if pi.get("TotalLayer") else None}
        a = self.attrs or {}
        return {
            "status": "printing" if code in ACTIVE else "idle",
            "variant": self.p.get("model") or a.get("MachineName") or "Centauri Carbon",
            "match_model": self.p.get("model") or a.get("MachineName"),
            "firmware": a.get("FirmwareVersion"), "machine_name": a.get("Name"),
            "extruders": [{"index": 0, "hotend": "Nozzle", "temp": _t(s.get("TempOfNozzle")),
                           "target": _t(s.get("TempTargetNozzle"), 0), "material_guid": "", "material": None,
                           "remaining_mm": None}],
            "bed": {"temp": _t(s.get("TempOfHotbed")), "target": _t(s.get("TempTargetHotbed"), 0)},
            "chamber": _t(s.get("TempOfBox")),
            "job": job,
        }

    # ---------------------------------------------------------------- control
    async def _ack(self, cmd, data=None, what="command"):
        res = await self._command(cmd, data)
        ack = res.get("Ack", 0)
        if ack:
            raise PrinterError(f"Printer refused {what} (code {ack}{': ' + START_ACK[ack] if cmd == CMD_START and ack in START_ACK else ''})")
        return res

    async def pause(self):
        await self._ack(CMD_PAUSE, what="pause")

    async def resume(self):
        await self._ack(CMD_RESUME, what="resume")

    async def abort(self):
        await self._ack(CMD_STOP, what="stop")

    async def upload_and_print(self, jobname, filename, data):
        if not filename.lower().endswith(".gcode"):
            raise PrinterError("The Centauri Carbon needs a plain .gcode file")
        remote = safe_remote_name(jobname)
        md5 = hashlib.md5(data).hexdigest()
        up_id = uuid.uuid4().hex
        url = f"http://{self.ip}{'' if HTTP_PORT == 80 else f':{HTTP_PORT}'}/uploadFile/upload"
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=6)) as h:
            for off in range(0, max(len(data), 1), CHUNK):
                try:
                    r = await h.post(url, data={"Check": "1", "S-File-MD5": md5, "Offset": str(off),
                                                "Uuid": up_id, "TotalSize": str(len(data))},
                                     files={"File": (remote, data[off:off + CHUNK], "application/octet-stream")})
                except httpx.HTTPError as e:
                    raise PrinterError(f"Upload failed: {e.__class__.__name__}") from e
                try:
                    ok = r.status_code == 200 and str(r.json().get("code")) == "000000"
                except ValueError:
                    ok = False
                if not ok:
                    raise PrinterError(f"Printer refused upload chunk at {off} (HTTP {r.status_code}): {r.text[:150]}")
        start = {"Filename": remote, "StartLayer": 0, "Calibration_switch": 0,
                 "PrintPlatformType": 0, "Tlp_Switch": 0, "slot_map": []}
        res = await self._command(CMD_START, start)
        if res.get("Ack") == 2:  # some firmware wants the storage prefix
            start["Filename"] = f"/local/{remote}"
            res = await self._command(CMD_START, start)
        if res.get("Ack"):
            ack = res["Ack"]
            raise PrinterError(f"Uploaded, but the printer would not start it: {START_ACK.get(ack, f'code {ack}')}")
        return remote

    # ---------------------------------------------------------------- camera
    async def snapshot(self):
        self._cam_wanted = time.time()
        if self._cam_task is None or self._cam_task.done():
            self._cam_frame = None
            self._cam_task = asyncio.create_task(self._camera_loop())
        for _ in range(75):           # the camera can take several seconds to wake up
            if self._cam_frame:
                return self._cam_frame, "image/jpeg"
            if self._cam_task.done():
                break
            await asyncio.sleep(0.2)
        raise PrinterError("Camera not available")

    async def _camera_loop(self):
        """One shared MJPEG connection, kept open only while someone is looking."""
        url = f"http://{self.ip}:{CAM_PORT}/video"
        enabled = False
        while time.time() - self._cam_wanted < 15:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=4)) as h:
                    async with h.stream("GET", url) as r:
                        if r.status_code != 200:
                            raise PrinterError(f"HTTP {r.status_code}")
                        buf = b""
                        async for chunk in r.aiter_bytes():
                            buf += chunk
                            while True:
                                s = buf.find(b"\xff\xd8")
                                e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                                if s < 0 or e < 0:
                                    break
                                self._cam_frame, buf = buf[s:e + 2], buf[e + 2:]
                            if len(buf) > 4_000_000:
                                buf = b""
                            if time.time() - self._cam_wanted > 15:
                                return
            except (httpx.HTTPError, PrinterError):
                if enabled:
                    return
                enabled = True
                try:  # camera stream may need switching on first
                    await self._command(CMD_VIDEO, {"Enable": 1}, timeout=5)
                except PrinterError:
                    return
                await asyncio.sleep(1)


def _t(v, i=-1):
    """Temperatures arrive as a number, or as [target, actual] on some firmware."""
    if isinstance(v, (list, tuple)):
        return v[i] if v else None
    return v
