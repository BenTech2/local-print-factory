"""Fake Ultimaker printer for testing Local Print Factory without hardware.

Implements the parts of the printer's /api/v1 that Local Print Factory uses, including
HTTP Digest auth and the pair-on-touchscreen flow (auto-approved after a few seconds).

    python -m simulator.sim_printer --port 8101 --name "Sim S5 A" --variant "Ultimaker S5"

Prints run at --speed x real time (default 60x). After a print the printer sits in
'wait_cleanup' until you "clear the build plate":  POST http://127.0.0.1:8101/sim/cleanup
(or pass --auto-cleanup N to do it automatically after N seconds).
"""
import argparse
import hashlib
import io
import re
import secrets
import struct
import time
import uuid
import zipfile
import zlib

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8101)
ap.add_argument("--name", default="Sim S5")
ap.add_argument("--variant", default="Ultimaker S5")
ap.add_argument("--speed", type=float, default=60.0)
ap.add_argument("--auto-cleanup", type=float, default=0)
ap.add_argument("--approve-after", type=float, default=3.0)
ap.add_argument("--material", default="2f9d2279-9b0e-4765-bf9b-d1e1e13f3c49")  # sim PLA
args, _ = ap.parse_known_args()

REALM = "Jedi-API"
app = FastAPI()
S = {
    "status": "idle", "job": None, "creds": {}, "pending": {}, "history": [],
    "led": {"hue": 0, "saturation": 0, "brightness": 100}, "message": None,
    "materials": [args.material, args.material],
}
MATERIAL_XML = {
    "2f9d2279-9b0e-4765-bf9b-d1e1e13f3c49": ("Ultimaker", "PLA", "Silver Metallic", "#a1a1a0"),
}


# ------------------------------------------------------------ digest auth
def _check_digest(request: Request):
    h = request.headers.get("authorization", "")
    if not h.startswith("Digest "):
        return False
    parts = dict(re.findall(r'(\w+)="?([^",]+)"?', h[7:]))
    user = parts.get("username")
    key = S["creds"].get(user)
    if not key:
        return False
    ha1 = hashlib.md5(f"{user}:{REALM}:{key}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{request.method}:{parts.get('uri')}".encode()).hexdigest()
    if parts.get("qop"):
        expect = hashlib.md5(f"{ha1}:{parts['nonce']}:{parts['nc']}:{parts['cnonce']}:{parts['qop']}:{ha2}".encode()).hexdigest()
    else:
        expect = hashlib.md5(f"{ha1}:{parts['nonce']}:{ha2}".encode()).hexdigest()
    return secrets.compare_digest(expect, parts.get("response", ""))


@app.middleware("http")
async def digest(request: Request, call_next):
    protected = request.method in ("PUT", "POST", "DELETE") or request.url.path == "/api/v1/auth/verify"
    if request.url.path.startswith("/api/v1") and protected and request.url.path != "/api/v1/auth/request":
        if not _check_digest(request):
            nonce = secrets.token_hex(16)
            return Response(status_code=401, headers={
                "WWW-Authenticate": f'Digest realm="{REALM}", nonce="{nonce}", qop="auth", algorithm=MD5'})
    return await call_next(request)


# ------------------------------------------------------------ simulation
def _tick():
    j = S["job"]
    if not j:
        return
    now = time.time()
    if j["state"] in ("printing", "pre_print"):
        j["time_elapsed"] = min(j["time_total"], j["_acc"] + (now - j["_since"]) * args.speed)
        j["progress"] = j["time_elapsed"] / j["time_total"] if j["time_total"] else 1
        if j["time_elapsed"] > 30:
            j["state"] = "printing"
        if j["time_elapsed"] >= j["time_total"]:
            _end("Finished")
    if j and j["state"] == "pausing" and now - j["_since"] > 2:
        j["state"] = "paused"
    if j and j["state"] == "resuming" and now - j["_since"] > 2:
        j["state"], j["_since"] = "printing", now
    if j and j["state"] == "wait_cleanup" and args.auto_cleanup and now - j["_ended"] > args.auto_cleanup:
        _cleanup()


def _end(result):
    j = S["job"]
    j.update(state="wait_cleanup", result=result, datetime_finished=time.strftime("%Y-%m-%dT%H:%M:%S"), _ended=time.time())


def _cleanup():
    j = S["job"]
    if j:
        S["history"].insert(0, {k: v for k, v in j.items() if not k.startswith("_")} | {
            "datetime_cleaned": time.strftime("%Y-%m-%dT%H:%M:%S")})
    S["job"], S["status"] = None, "idle"


def _public_job():
    _tick()
    j = S["job"]
    return {k: v for k, v in j.items() if not k.startswith("_")} if j else None


# ------------------------------------------------------------ API
@app.get("/api/v1/system")
def system():
    return {"name": args.name, "variant": args.variant, "type": "3D printer", "platform": "sim",
            "hostname": args.name.lower().replace(" ", "-"), "guid": str(uuid.uuid5(uuid.NAMESPACE_DNS, args.name)),
            "firmware": "8.9.9-sim"}


@app.get("/api/v1/system/name")
def system_name():
    return args.name


@app.get("/api/v1/system/firmware")
def firmware():
    return "8.9.9-sim"


@app.get("/api/v1/printer")
def printer():
    _tick()
    hot = S["job"] and S["job"]["state"] in ("printing", "pre_print")
    return {
        "status": "printing" if S["job"] else S["status"],
        "bed": {"temperature": {"current": 60.1 if hot else 24.0, "target": 60 if hot else 0}, "type": "glass"},
        "led": S["led"],
        "heads": [{"extruders": [
            {"hotend": {"id": "AA 0.4", "temperature": {"current": 210.2 if hot else 25.3, "target": 210 if hot else 0}},
             "active_material": {"guid": S["materials"][0], "length_remaining": 182000}},
            {"hotend": {"id": "BB 0.4", "temperature": {"current": 25.1, "target": 0}},
             "active_material": {"guid": S["materials"][1], "length_remaining": -1}},
        ]}],
    }


@app.get("/api/v1/print_job")
def print_job():
    j = _public_job()
    return j if j else JSONResponse({"message": "Not found"}, status_code=404)


@app.post("/api/v1/print_job")
async def post_print_job(request: Request):
    if S["job"] or S["status"] != "idle":
        return JSONResponse({"message": "Printer is busy"}, status_code=405)
    form = await request.form()
    up = form.get("file")
    if up is None:
        return JSONResponse({"message": "No file"}, status_code=400)
    data = await up.read()
    text = data[:65536]
    if up.filename.lower().endswith(".ufp"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            g = [n for n in z.namelist() if n.endswith(".gcode")]
            text = z.read(g[0])[:65536] if g else b""
    m = re.search(rb";PRINT\.TIME:(\d+)", text)
    total = int(m.group(1)) if m else 1800
    uid = str(uuid.uuid4())
    S["job"] = {"name": form.get("jobname") or up.filename, "uuid": uid, "state": "pre_print", "progress": 0,
                "time_total": total, "time_elapsed": 0, "source": "WEB_API", "result": "",
                "pause_source": "", "datetime_started": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "datetime_finished": "", "_acc": 0, "_since": time.time()}
    S["status"] = "printing"
    return JSONResponse({"message": "Job created", "uuid": uid}, status_code=201)


@app.put("/api/v1/print_job/state")
async def job_state(request: Request):
    target = await request.json()
    if isinstance(target, dict):
        target = target.get("target")
    j = S["job"]
    _tick()
    if not j:
        return JSONResponse({"message": "No job"}, status_code=405)
    now = time.time()
    if target == "pause" and j["state"] in ("printing", "pre_print"):
        j["_acc"] = j["time_elapsed"]
        j.update(state="pausing", pause_source="api", _since=now)
    elif target == "print" and j["state"] == "paused":
        j.update(state="resuming", _since=now)
    elif target == "abort" and j["state"] not in ("wait_cleanup",):
        _end("Aborted")
    else:
        return JSONResponse({"message": f"Cannot {target} now"}, status_code=405)
    return Response(status_code=204)


@app.get("/api/v1/history/print_jobs")
def history(count: int = 50):
    return S["history"][:count]


@app.get("/api/v1/history/print_jobs/{uid}")
def history_job(uid: str):
    for h in S["history"]:
        if h["uuid"] == uid:
            return h
    return JSONResponse({"message": "Not found"}, status_code=404)


@app.get("/api/v1/materials/{guid}")
def material(guid: str):
    b, m, c, cc = MATERIAL_XML.get(guid, ("Generic", "PLA", "White", "#ffffff"))
    xml = f"""<?xml version="1.0"?><fdmmaterial xmlns="http://www.ultimaker.com/material"><metadata>
<name><brand>{b}</brand><material>{m}</material><color>{c}</color></name><GUID>{guid}</GUID>
<color_code>{cc}</color_code></metadata></fdmmaterial>"""
    return Response(xml, media_type="application/xml")


@app.post("/api/v1/auth/request")
async def auth_request(request: Request):
    form = await request.form()
    aid, key = secrets.token_hex(16), secrets.token_hex(32)
    S["pending"][aid] = {"key": key, "t": time.time(), "app": form.get("application")}
    return {"id": aid, "key": key}


@app.get("/api/v1/auth/check/{aid}")
def auth_check(aid: str):
    if aid in S["creds"]:
        return {"message": "authorized"}
    p = S["pending"].get(aid)
    if not p:
        return {"message": "unknown"}
    if time.time() - p["t"] > args.approve_after:
        S["creds"][aid] = p["key"]
        S["pending"].pop(aid)
        return {"message": "authorized"}
    return {"message": "unknown"}


@app.get("/api/v1/auth/verify")
def auth_verify():
    return {"message": "ok"}


@app.put("/api/v1/printer/led")
async def put_led(request: Request):
    S["led"].update(await request.json())
    return Response(status_code=204)


@app.post("/api/v1/printer/led/blink")
def blink():
    return Response(status_code=204)


@app.put("/api/v1/system/display_message")
async def display_message(request: Request):
    S["message"] = (await request.json()).get("message")
    return Response(status_code=200)


def _png(w, h, pct):
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            if y > h - 14 and x < w * pct:
                row += bytes((31, 111, 235))
            elif (x // 20 + y // 20) % 2:
                row += bytes((40, 44, 52))
            else:
                row += bytes((30, 33, 40))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


@app.get("/api/v1/camera/0/snapshot")
def snapshot():
    j = _public_job()
    return Response(_png(320, 240, (j or {}).get("progress", 0)), media_type="image/png")


@app.get("/api/v1/camera/0/stream")
async def stream():
    import asyncio

    async def gen():
        while True:
            j = _public_job()
            img = _png(320, 240, (j or {}).get("progress", 0))
            yield b"--frame\r\nContent-Type: image/png\r\nContent-Length: " + str(len(img)).encode() + b"\r\n\r\n" + img + b"\r\n"
            await asyncio.sleep(1)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


# ------------------------------------------------------------ sim controls
@app.post("/sim/cleanup")
def sim_cleanup():
    if S["job"] and S["job"]["state"] == "wait_cleanup":
        _cleanup()
        return {"ok": True}
    return {"ok": False, "reason": "not waiting for cleanup"}


@app.post("/sim/material/{idx}/{guid}")
def sim_material(idx: int, guid: str):
    S["materials"][idx] = guid
    return {"ok": True}


@app.get("/sim/state")
def sim_state():
    return {"status": S["status"], "job": _public_job(), "message": S["message"], "led": S["led"],
            "paired_apps": len(S["creds"])}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
