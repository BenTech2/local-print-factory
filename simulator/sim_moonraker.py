"""Fake Klipper/Moonraker printer (e.g. Elegoo Neptune 4 Pro) for testing.

    python -m simulator.sim_moonraker --port 8201 --hostname neptune4pro

Unlike Ultimakers, Klipper printers go straight back to accepting jobs after a print
(print_stats.state stays 'complete') - Local Print Factory must hold them until the plate is cleared.
"""
import argparse
import re
import time

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from simulator.sim_printer import _png

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8201)
ap.add_argument("--hostname", default="neptune4pro")
ap.add_argument("--speed", type=float, default=60.0)
args, _ = ap.parse_known_args()

app = FastAPI()
S = {"state": "standby", "filename": "", "files": {}, "history": [], "acc": 0.0, "since": 0.0,
     "est": 0, "job_id": 0}


def R(x):
    return {"result": x}


def _tick():
    if S["state"] == "printing":
        el = S["acc"] + (time.time() - S["since"]) * args.speed
        if el >= S["est"]:
            S["acc"] = S["est"]
            _end("complete", "completed")


def _elapsed():
    if S["state"] == "printing":
        return min(S["est"], S["acc"] + (time.time() - S["since"]) * args.speed)
    return S["acc"]


def _end(state, hist):
    S["state"] = state
    S["history"].insert(0, {"job_id": str(S["job_id"]), "filename": S["filename"], "status": hist,
                            "start_time": time.time() - S["acc"] / args.speed, "print_duration": S["acc"]})


@app.get("/server/info")
def server_info():
    return R({"klippy_connected": True, "klippy_state": "ready", "moonraker_version": "v0.8-sim"})


@app.get("/printer/info")
def printer_info():
    return R({"state": "ready", "hostname": args.hostname, "software_version": "v0.12.0-sim"})


@app.get("/printer/objects/query")
def query():
    _tick()
    el = _elapsed()
    hot = S["state"] in ("printing", "paused")
    prog = el / S["est"] if S["est"] else 0
    return R({"eventtime": time.time(), "status": {
        "webhooks": {"state": "ready", "state_message": "Printer is ready"},
        "print_stats": {"state": S["state"], "filename": S["filename"], "print_duration": el,
                        "total_duration": el + 5, "message": ""},
        "virtual_sdcard": {"progress": prog},
        "display_status": {"progress": prog, "message": None},
        "extruder": {"temperature": 219.8 if hot else 24.5, "target": 220.0 if hot else 0},
        "heater_bed": {"temperature": 60.2 if hot else 23.9, "target": 60.0 if hot else 0},
    }})


@app.get("/server/files/metadata")
def metadata(filename: str):
    if filename not in S["files"]:
        return JSONResponse({"error": {"message": "not found"}}, status_code=404)
    return R({"filename": filename, "estimated_time": S["files"][filename]})


@app.post("/server/files/upload")
async def upload(request: Request):
    form = await request.form()
    f = form["file"]
    data = await f.read()
    m = re.search(rb";TIME:(\d+)", data[:65536]) or re.search(rb"estimated printing time.*?=\s*(\d+)m", data)
    est = int(m.group(1)) if m else 1200
    S["files"][f.filename] = est
    started = False
    if form.get("print") == "true":
        if S["state"] in ("printing", "paused"):
            return JSONResponse({"error": {"message": "Printer is busy"}}, status_code=400)
        S.update(state="printing", filename=f.filename, acc=0.0, since=time.time(), est=est, job_id=S["job_id"] + 1)
        started = True
    return JSONResponse({"result": {"item": {"path": f.filename, "root": "gcodes"}, "action": "create_file",
                                    "print_started": started}}, status_code=201)


@app.post("/printer/print/pause")
def pause():
    _tick()
    if S["state"] != "printing":
        return JSONResponse({"error": {"message": "Not printing"}}, status_code=400)
    S["acc"] = _elapsed()
    S["state"] = "paused"
    return R("ok")


@app.post("/printer/print/resume")
def resume():
    if S["state"] != "paused":
        return JSONResponse({"error": {"message": "Not paused"}}, status_code=400)
    S.update(state="printing", since=time.time())
    return R("ok")


@app.post("/printer/print/cancel")
def cancel():
    _tick()
    if S["state"] not in ("printing", "paused"):
        return JSONResponse({"error": {"message": "Not printing"}}, status_code=400)
    S["acc"] = _elapsed()
    _end("cancelled", "cancelled")
    return R("ok")


@app.get("/server/history/list")
def history(limit: int = 50, order: str = "desc"):
    return R({"count": len(S["history"]), "jobs": S["history"][:limit]})


@app.get("/server/webcams/list")
def webcams():
    return R({"webcams": [{"name": "default", "service": "mjpegstreamer",
                           "snapshot_url": f"http://127.0.0.1:{args.port}/webcam/snapshot",
                           "stream_url": f"http://127.0.0.1:{args.port}/webcam/stream"}]})


@app.get("/webcam/snapshot")
def snap():
    return Response(_png(320, 240, (_elapsed() / S["est"]) if S["est"] else 0), media_type="image/png")


@app.get("/sim/state")
def sim_state():
    _tick()
    return {**{k: v for k, v in S.items() if k != "files"}, "elapsed": _elapsed()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
