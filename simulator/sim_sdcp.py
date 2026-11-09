"""Fake Elegoo Centauri Carbon (SDCP v3) for testing.

Real printers use ws :3030, upload :80, camera :3031, discovery UDP :3000. Here everything
HTTP/WebSocket is on one --port, and UDP on --udp-port, so point Local Print Factory at it with:

    LF_SDCP_WS_PORT=8301 LF_SDCP_HTTP_PORT=8301 LF_SDCP_CAM_PORT=8301 LF_SDCP_UDP_PORT=8300

    python -m simulator.sim_sdcp --port 8301 --udp-port 8300
"""
import argparse
import asyncio
import hashlib
import json
import re
import time

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from simulator.sim_printer import _png

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8301)
ap.add_argument("--udp-port", type=int, default=8300)
ap.add_argument("--name", default="Centauri Carbon")
ap.add_argument("--speed", type=float, default=60.0)
ap.add_argument("--idle-after", type=float, default=20.0, help="real seconds after finishing before Status returns to 0")
ap.add_argument("--require-prefix", action="store_true", help="only accept '/local/<file>' in start print")
args, _ = ap.parse_known_args()

MB = "a1b2c3d4e5f60718"
app = FastAPI()
S = {"code": 0, "file": "", "task": "", "acc": 0.0, "since": 0.0, "total": 0, "ended": 0.0,
     "files": {}, "uploads": {}, "clients": 0, "starts": []}


def _elapsed():
    if S["code"] in (13, 16):
        return min(S["total"], S["acc"] + (time.time() - S["since"]) * args.speed)
    return S["acc"]


def _tick():
    now = time.time()
    if S["code"] == 16 and now - S["since"] > 1:
        S["code"] = 13
    if S["code"] == 13 and _elapsed() >= S["total"]:
        S.update(acc=S["total"], code=9, ended=now)
    if S["code"] == 5 and now - S["since"] > 1:
        S["code"] = 6
    if S["code"] == 7 and now - S["since"] > 1:
        S.update(code=8, ended=now)
    if S["code"] in (8, 9) and args.idle_after and now - S["ended"] > args.idle_after:
        S["code"] = 0


def status_msg():
    _tick()
    el = _elapsed()
    hot = S["code"] in (13, 16, 5, 6)
    return {"Status": {
        "CurrentStatus": [1 if S["code"] else 0],
        "TempOfNozzle": 219.6 if hot else 26.0, "TempTargetNozzle": 220 if hot else 0,
        "TempOfHotbed": 59.9 if hot else 25.0, "TempTargetHotbed": 60 if hot else 0,
        "TempOfBox": 31.0, "TempTargetBox": 0,
        "PrintInfo": {"Status": S["code"], "CurrentLayer": int(el / 10), "TotalLayer": int(S["total"] / 10),
                      "CurrentTicks": int(el), "TotalTicks": int(S["total"]), "Filename": S["file"],
                      "TaskId": S["task"], "PrintSpeedPct": 100,
                      "Progress": int(100 * el / S["total"]) if S["total"] else 0},
    }, "MainboardID": MB, "TimeStamp": int(time.time()), "Topic": f"sdcp/status/{MB}"}


ATTRS = {"Attributes": {"Name": args.name, "MachineName": "Centauri Carbon", "FirmwareVersion": "V1.1.25-sim",
                        "MainboardID": MB}, "MainboardID": MB, "Topic": f"sdcp/attributes/{MB}"}


def handle(cmd, data):
    _tick()
    now = time.time()
    if cmd == 128:
        fn = data.get("Filename", "")
        S["starts"].append(fn)
        if args.require_prefix and not fn.startswith("/local/"):
            return 2
        name = fn.removeprefix("/local/")
        if name not in S["files"]:
            return 2
        if S["code"] in (13, 16, 5, 6, 7):
            return 1
        S.update(code=16, file=name, task=hashlib.md5(f"{name}{now}".encode()).hexdigest()[:16],
                 acc=0.0, since=now, total=S["files"][name])
    elif cmd == 129:
        if S["code"] != 13:
            return 1
        S.update(acc=_elapsed(), code=5, since=now)
    elif cmd == 131:
        if S["code"] != 6:
            return 1
        S.update(code=13, since=now)
    elif cmd == 130:
        if S["code"] not in (13, 16, 5, 6):
            return 1
        S.update(acc=_elapsed(), code=7, since=now)
    elif cmd == 386:
        pass
    return 0


@app.websocket("/websocket")
async def ws(sock: WebSocket):
    if S["clients"] >= 5:
        await sock.close(code=1013)
        return
    await sock.accept()
    S["clients"] += 1
    try:
        while True:
            msg = json.loads(await sock.receive_text())
            d = msg.get("Data", {})
            if d.get("MainboardID") != MB:
                continue  # real printers ignore requests for another mainboard
            cmd = d.get("Cmd")
            ack = handle(cmd, d.get("Data") or {}) if cmd not in (0, 1) else 0
            await sock.send_text(json.dumps({"Id": msg.get("Id"), "Data": {
                "Cmd": cmd, "Data": {"Ack": ack}, "RequestID": d.get("RequestID"), "MainboardID": MB,
                "TimeStamp": int(time.time())}, "Topic": f"sdcp/response/{MB}"}))
            if cmd == 0:
                await sock.send_text(json.dumps(status_msg()))
            elif cmd == 1:
                await sock.send_text(json.dumps(ATTRS))
    except WebSocketDisconnect:
        pass
    finally:
        S["clients"] -= 1


@app.post("/uploadFile/upload")
async def upload(request: Request):
    form = await request.form()
    up = S["uploads"].setdefault(form["Uuid"], bytearray())
    off = int(form["Offset"])
    if off != len(up):
        return JSONResponse({"code": "111111", "messages": [{"field": "Offset", "message": "bad offset"}]})
    up += await form["File"].read()
    if len(up) >= int(form["TotalSize"]):
        data = bytes(S["uploads"].pop(form["Uuid"]))
        if hashlib.md5(data).hexdigest() != form["S-File-MD5"]:
            return JSONResponse({"code": "222222", "messages": [{"message": "md5 mismatch"}]})
        m = re.search(rb"total estimated time:\s*(?:(\d+)h\s*)?(\d+)m", data[:65536])
        total = (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60) if m else 1200
        S["files"][form["File"].filename] = total
    return {"code": "000000", "messages": None, "data": None, "success": True}


@app.get("/video")
async def video():
    async def gen():
        for _ in range(600):
            # PNG standing in for JPEG frames; wrap so the driver's JPEG frame finder sees SOI/EOI markers.
            img = b"\xff\xd8" + _png(320, 240, (_elapsed() / S["total"]) if S["total"] else 0) + b"\xff\xd9"
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + img + b"\r\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/sim/state")
def sim_state():
    m = status_msg()["Status"]["PrintInfo"]
    return {**m, "files": list(S["files"]), "starts": S["starts"], "clients": S["clients"]}


class Udp(asyncio.DatagramProtocol):
    def connection_made(self, t):
        self.t = t

    def datagram_received(self, data, addr):
        if data.strip() == b"M99999":
            self.t.sendto(json.dumps({"Id": MB, "Data": {"Name": args.name, "MachineName": "Centauri Carbon",
                                                         "BrandName": "ELEGOO", "MainboardIP": "127.0.0.1",
                                                         "MainboardID": MB, "ProtocolVersion": "V3.0.0",
                                                         "FirmwareVersion": "V1.1.25-sim"}}).encode(), addr)


@app.on_event("startup")
async def start_udp():
    await asyncio.get_running_loop().create_datagram_endpoint(Udp, local_addr=("127.0.0.1", args.udp_port))


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
