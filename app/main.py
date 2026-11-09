"""Local Print Factory - self-hosted fleet manager for Ultimaker and Elegoo printers."""
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db, fleet
from .config import APP_NAME, APP_PASSWORD, FILES_DIR, THUMBS_DIR, VERSION
from .discovery import discover
from .files import allowed
from .drivers import DRIVERS, KINDS, PrinterError
from .drivers.moonraker import probe as moonraker_probe
from .drivers.sdcp import WS_PORT as SDCP_WS_PORT, udp_probe
from .printer_client import UltimakerClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"
PAIRING: dict[int, dict] = {}


@contextlib.asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(fleet.run_forever())
    yield
    task.cancel()


app = FastAPI(title="Local Print Factory", version=VERSION, lifespan=lifespan)


# ------------------------------------------------------------------ auth
def _token():
    return hmac.new(APP_PASSWORD.encode(), b"lf-session-v1", hashlib.sha256).hexdigest()


@app.middleware("http")
async def require_login(request: Request, call_next):
    # The UI shell is public; every /api call (except login) needs the session or X-Api-Key.
    path = request.url.path
    if APP_PASSWORD and path.startswith("/api/") and path != "/api/login":
        ok = (hmac.compare_digest(request.cookies.get("lf_session", ""), _token()) or
              hmac.compare_digest(request.headers.get("x-api-key", ""), APP_PASSWORD))
        if not ok:
            return JSONResponse({"detail": "Login required"}, status_code=401)
    response = await call_next(request)
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # revalidate so upgrades show up immediately
    return response


@app.post("/api/login")
async def login(password: str = Form(...)):
    if not APP_PASSWORD or not hmac.compare_digest(password, APP_PASSWORD):
        await asyncio.sleep(1)
        raise HTTPException(401, "Wrong password")
    r = JSONResponse({"ok": True})
    r.set_cookie("lf_session", _token(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90)
    return r


@app.post("/api/logout")
async def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie("lf_session")
    return r


# ------------------------------------------------------------------ helpers
def _printer(pid):
    p = db.one("SELECT * FROM printers WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "Printer not found")
    return p


def _public_printer(p):
    st = {k: v for k, v in fleet.STATE.get(p["id"], {}).items() if not k.startswith("_")}
    drv = DRIVERS.get(p["kind"], DRIVERS["ultimaker"])
    return {"id": p["id"], "name": p["name"], "host": p["host"], "accept_jobs": bool(p["accept_jobs"]),
            "kind": drv.kind, "kind_label": drv.label, "model": p["model"], "caps": sorted(drv.caps),
            "has_api_key": bool(p["auth_key"]) and drv.kind == "moonraker",
            "paired": bool(p["auth_id"]) if drv.needs_pairing else True,
            "pairing": p["id"] in PAIRING,
            "state": st or {"online": None, "status": "connecting"}}


async def _call(coro):
    try:
        return await coro
    except PrinterError as e:
        raise HTTPException(502, str(e))


def _driver(pid, cap=None):
    p = _printer(pid)
    d = fleet.driver_for(p)
    if cap and cap not in d.caps:
        raise HTTPException(400, f"{d.label} printers don't support that")
    return p, d


# ------------------------------------------------------------------ state
@app.get("/api/state")
def state():
    printers = [_public_printer(p) for p in db.q("SELECT * FROM printers ORDER BY name COLLATE NOCASE")]
    jobs = db.q("SELECT j.*, COALESCE(f.has_thumb, 0) AS has_thumb, json_extract(f.meta, '$.print_time') AS est_time "
                f"FROM jobs j LEFT JOIN files f ON f.id = j.file_id "
                f"WHERE j.status IN ('queued',{','.join('?' * len(db.ACTIVE_STATES))}) "
                "ORDER BY CASE j.status WHEN 'queued' THEN 1 ELSE 0 END, j.position", db.ACTIVE_STATES)
    return {"version": VERSION, "time": time.time(), "printers": printers, "jobs": jobs,
            "login": bool(APP_PASSWORD)}


@app.get("/api/kinds")
def kinds():
    return KINDS


# ------------------------------------------------------------------ printers
@app.get("/api/discover")
async def api_discover():
    um, elegoo = await asyncio.gather(asyncio.to_thread(discover), udp_probe(wait=3.0), return_exceptions=True)
    found = []
    for f in um if isinstance(um, list) else []:
        found.append({**f, "kind_id": "ultimaker", "type": "Ultimaker"})
    for f in elegoo if isinstance(elegoo, list) else []:
        found.append({"name": f["name"], "host": f["host"], "firmware": f["firmware"],
                      "kind_id": "centauri", "type": f.get("model") or "Elegoo (SDCP)"})
    known = {p["host"].split(":")[0] for p in db.q("SELECT host FROM printers")}
    for f in found:
        f["added"] = f["host"].split(":")[0] in known
    return sorted(found, key=lambda f: f["name"])


@app.post("/api/printers")
async def add_printer(body: dict = Body(...)):
    host = (body.get("host") or "").strip().rstrip("/")
    for prefix in ("http://", "https://"):
        host = host.removeprefix(prefix)
    if not host:
        raise HTTPException(400, "Host / IP address is required")
    kind = next((k for k in KINDS if k["id"] == (body.get("kind_id") or "ultimaker")), None)
    if not kind:
        raise HTTPException(400, "Unknown printer type")
    model = (body.get("model") or "").strip() or kind["model"]
    name = (body.get("name") or "").strip()
    auth_id = None
    api_key = (body.get("api_key") or "").strip() or None

    if kind["kind"] == "ultimaker":
        c = UltimakerClient(host)
        found, reachable = None, False
        try:
            found = await asyncio.wait_for(c._json("/system/name"), 5)
            reachable = True
        except (PrinterError, asyncio.TimeoutError, ValueError):
            pass                       # offline right now - allow adding it anyway
        finally:
            await c.close()
        if reachable and found is None:
            # Something answered, but not the Ultimaker API. Say what it is instead of adding a dead card.
            try:
                await moonraker_probe(host)
                other = "a Klipper/Moonraker printer (e.g. an Elegoo Neptune 4)"
            except PrinterError:
                other = None
            raise HTTPException(400, f"{host} is not an Ultimaker" + (f" - it looks like {other}. Choose that as the "
                                "printer type." if other else " (no /api/v1 found). Check the IP and printer type."))
        name = name or found or host
    elif kind["kind"] == "moonraker":
        try:
            host, info = await moonraker_probe(host)
        except PrinterError as e:
            raise HTTPException(400, str(e))
        name = name or info.get("hostname") or model or host
    elif kind["kind"] == "sdcp":
        found = await udp_probe(host.split(":")[0], 2.0)
        if found:
            auth_id = found[0]["mainboard"]   # cached so we don't have to ask again
            name = name or found[0]["name"]
        else:
            ip = host.split(":")[0]
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, SDCP_WS_PORT), 4)
                w.close()
            except (OSError, asyncio.TimeoutError):
                raise HTTPException(400, f"No Elegoo printer answered at {ip} (UDP 3000 / TCP {SDCP_WS_PORT})")
        name = name or model or host

    if db.one("SELECT id FROM printers WHERE host=?", (host,)):
        raise HTTPException(409, "That printer is already added")
    # Printers that don't check their own build plate start held until someone confirms the bed is empty:
    # whatever was printed before Local Print Factory knew about the printer may still be on it.
    pending = fleet.UNKNOWN_PLATE if DRIVERS[kind["kind"]].manual_clear else None
    pid = db.ex("INSERT INTO printers(name, host, kind, model, auth_id, auth_key, clear_pending, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (name, host, kind["kind"], model, auth_id, api_key, pending, time.time()))
    return _public_printer(_printer(pid))


@app.patch("/api/printers/{pid}")
def edit_printer(pid: int, body: dict = Body(...)):
    p = _printer(pid)
    name = (body.get("name") or p["name"]).strip() or p["name"]
    host = (body.get("host") or p["host"]).strip() or p["host"]
    accept = int(bool(body.get("accept_jobs", p["accept_jobs"])))
    model = (body["model"].strip() or None) if "model" in body else p["model"]
    if host != p["host"] and p["kind"] == "ultimaker":
        db.ex("UPDATE printers SET auth_id=NULL, auth_key=NULL WHERE id=?", (pid,))
    if host != p["host"] and p["kind"] == "sdcp":
        db.ex("UPDATE printers SET auth_id=NULL WHERE id=?", (pid,))
    if host != p["host"] and DRIVERS[p["kind"]].manual_clear:
        db.ex("UPDATE printers SET clear_pending=?, cleared_key=NULL WHERE id=?", (fleet.UNKNOWN_PLATE, pid))
    if p["kind"] == "moonraker" and "api_key" in body:
        db.ex("UPDATE printers SET auth_key=? WHERE id=?", ((body["api_key"] or "").strip() or None, pid))
    db.ex("UPDATE printers SET name=?, host=?, accept_jobs=?, model=? WHERE id=?", (name, host, accept, model, pid))
    return _public_printer(_printer(pid))


@app.delete("/api/printers/{pid}")
async def delete_printer(pid: int):
    _printer(pid)
    db.ex("UPDATE jobs SET status='queued', printer_id=NULL, message='Printer was removed' "
          "WHERE printer_id=? AND status='sending'", (pid,))
    db.ex("DELETE FROM printers WHERE id=?", (pid,))
    fleet.forget_printer(pid)
    return {"ok": True}


@app.post("/api/printers/{pid}/pair")
async def start_pair(pid: int):
    p, _ = _driver(pid, "pair")
    c = UltimakerClient(p["host"])
    try:
        cred = await _call(c.request_auth(APP_NAME, "local-factory"))
    finally:
        await c.close()
    PAIRING[pid] = {"id": cred["id"], "key": cred["key"], "started": time.time()}
    return {"status": "waiting", "message": "Approve the request on the printer's touchscreen."}


@app.get("/api/printers/{pid}/pair")
async def pair_status(pid: int):
    p = _printer(pid)
    pend = PAIRING.get(pid)
    if not pend:
        return {"status": "paired" if p["auth_id"] else "none"}
    c = UltimakerClient(p["host"], pend["id"], pend["key"])
    try:
        status = await _call(c.check_auth(pend["id"]))
        if status == "authorized" and await _call(c.verify()):
            db.ex("UPDATE printers SET auth_id=?, auth_key=? WHERE id=?", (pend["id"], pend["key"], pid))
            PAIRING.pop(pid, None)
            return {"status": "paired"}
    finally:
        await c.close()
    if status == "unauthorized":
        PAIRING.pop(pid, None)
        return {"status": "denied", "message": "The request was declined on the printer."}
    if time.time() - pend["started"] > 300:
        PAIRING.pop(pid, None)
        return {"status": "expired", "message": "Nobody approved it within 5 minutes - try again."}
    return {"status": "waiting"}


@app.delete("/api/printers/{pid}/pair")
def cancel_pair(pid: int):
    PAIRING.pop(pid, None)
    return {"ok": True}


@app.post("/api/printers/{pid}/action")
async def printer_action(pid: int, body: dict = Body(...)):
    action = body.get("action")
    if action not in ("pause", "resume", "abort"):
        raise HTTPException(400, "action must be pause, resume or abort")
    _, d = _driver(pid, action)
    await _call(getattr(d, action)())
    db.log_event(pid, "control", f"{action} requested from web UI")
    return {"ok": True}


@app.post("/api/printers/{pid}/clear")
async def plate_clear(pid: int):
    p, _ = _driver(pid, "clear")
    fleet.mark_plate_clear(p)
    db.log_event(pid, "plate_cleared", "Build plate marked clear from web UI")
    return {"ok": True}


@app.post("/api/printers/{pid}/identify")
async def identify(pid: int):
    _, d = _driver(pid, "identify")
    await _call(d.identify())
    return {"ok": True}


@app.post("/api/printers/{pid}/led")
async def led(pid: int, body: dict = Body(...)):
    _, d = _driver(pid, "led")
    await _call(d.set_led(body.get("hue"), body.get("saturation"), body.get("brightness")))
    return {"ok": True}


@app.post("/api/printers/{pid}/message")
async def message(pid: int, body: dict = Body(...)):
    text = (body.get("message") or "").strip()
    if not text:
        raise HTTPException(400, "Message is empty")
    _, d = _driver(pid, "message")
    await _call(d.display_message(text[:200]))
    return {"ok": True}


@app.get("/api/printers/{pid}/snapshot")
async def snapshot(pid: int):
    _, d = _driver(pid, "camera")
    data, ctype = await _call(d.snapshot())
    return Response(data, media_type=ctype, headers={"Cache-Control": "no-store"})


@app.get("/api/printers/{pid}/history")
async def printer_history(pid: int):
    _, d = _driver(pid, "history")
    return await _call(d.history())


# ------------------------------------------------------------------ files
@app.get("/api/files")
def list_files():
    rows = db.q("SELECT * FROM files ORDER BY uploaded_at DESC")
    for r in rows:
        r["meta"] = json.loads(r["meta"] or "{}")
        for ex in (r["meta"].get("extruders") or {}).values():
            ex["material"] = fleet.material_info(ex.get("material_guid"))
    return rows


@app.post("/api/files")
async def upload_files(files: list[UploadFile] = File(...), queue: int = Form(0),
                       copies: int = Form(1), printer_id: int | None = Form(None)):
    out = []
    for up in files:
        if not allowed(up.filename or ""):
            raise HTTPException(400, f"{up.filename}: only .ufp, .gcode and .gcode.gz files are accepted")
        data = await up.read()
        fid = await asyncio.to_thread(fleet.add_file, Path(up.filename).name, data)
        if queue:
            fleet.queue_file(fid, copies, printer_id or None)
        out.append(db.file_row(fid))
    return out


def _file(fid):
    f = db.file_row(fid)
    if not f:
        raise HTTPException(404, "File not found")
    return f


@app.get("/api/files/{fid}/download")
def download(fid: int):
    f = _file(fid)
    return FileResponse(FILES_DIR / f["stored_name"], filename=f["filename"])


@app.get("/api/files/{fid}/thumb")
def thumb(fid: int):
    p = THUMBS_DIR / f"{fid}.png"
    if not p.exists():
        raise HTTPException(404)
    jpeg = p.read_bytes()[:2] == b"\xff\xd8"
    return FileResponse(p, media_type="image/jpeg" if jpeg else "image/png")


@app.delete("/api/files/{fid}")
def delete_file(fid: int):
    f = _file(fid)
    if db.one("SELECT id FROM jobs WHERE file_id=? AND status IN ('queued','sending')", (fid,)):
        raise HTTPException(409, "This file is still in the queue - cancel those jobs first")
    (FILES_DIR / f["stored_name"]).unlink(missing_ok=True)
    (THUMBS_DIR / f"{fid}.png").unlink(missing_ok=True)
    db.ex("DELETE FROM files WHERE id=?", (fid,))
    return {"ok": True}


@app.get("/api/files/{fid}/check")
async def check_file(fid: int):
    f = _file(fid)
    strict = db.get_settings().get("strict_materials") == "1"
    out = []
    for p in db.q("SELECT * FROM printers ORDER BY name COLLATE NOCASE"):
        st = fleet.STATE.get(p["id"], {})
        hard, warn = fleet.compatibility(f, st, fleet.driver_for(p).extensions, strict)
        out.append({"printer_id": p["id"], "name": p["name"], "blocking": hard, "warnings": warn,
                    "online": bool(st.get("online"))})
    return out


@app.post("/api/files/{fid}/queue")
def queue(fid: int, body: dict = Body(default={})):
    _file(fid)
    ids = fleet.queue_file(fid, int(body.get("copies") or 1), body.get("printer_id") or None, body.get("name"))
    return {"job_ids": ids}


# ------------------------------------------------------------------ jobs
@app.get("/api/jobs")
def jobs(done: int = 0, limit: int = 200):
    if done:
        return db.q("SELECT j.*, COALESCE(f.has_thumb, 0) AS has_thumb FROM jobs j LEFT JOIN files f ON f.id = j.file_id "
                    f"WHERE j.status IN ({','.join('?' * len(db.DONE_STATES))}) "
                    "ORDER BY COALESCE(j.finished_at, j.created_at) DESC LIMIT ?", (*db.DONE_STATES, limit))
    return state()["jobs"]


def _job(jid):
    j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not j:
        raise HTTPException(404, "Job not found")
    return j


@app.patch("/api/jobs/{jid}")
def edit_job(jid: int, body: dict = Body(...)):
    j = _job(jid)
    if j["status"] != db.QUEUED:
        raise HTTPException(409, "Only queued jobs can be changed")
    if "target_printer_id" in body:
        db.ex("UPDATE jobs SET target_printer_id=?, message=NULL WHERE id=?", (body["target_printer_id"] or None, jid))
    if body.get("name"):
        db.ex("UPDATE jobs SET name=? WHERE id=?", (body["name"].strip(), jid))
    return _job(jid)


@app.post("/api/jobs/{jid}/move")
def move_job(jid: int, body: dict = Body(...)):
    j = _job(jid)
    queued = db.q("SELECT id, position FROM jobs WHERE status='queued' ORDER BY position")
    ids = [r["id"] for r in queued]
    if jid not in ids:
        raise HTTPException(409, "Only queued jobs can be reordered")
    i = ids.index(jid)
    d = body.get("direction")
    if d == "top":
        db.ex("UPDATE jobs SET position=? WHERE id=?", (queued[0]["position"] - 1, jid))
    elif d in ("up", "down"):
        k = i - 1 if d == "up" else i + 1
        if 0 <= k < len(queued):
            a, b = queued[i], queued[k]
            db.ex("UPDATE jobs SET position=? WHERE id=?", (b["position"], a["id"]))
            db.ex("UPDATE jobs SET position=? WHERE id=?", (a["position"], b["id"]))
    return {"ok": True, "job": j["id"]}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: int):
    j = _job(jid)
    if j["status"] != db.QUEUED:
        raise HTTPException(409, "Only queued jobs can be cancelled here - use Abort on the printer for running prints")
    db.ex("UPDATE jobs SET status=?, finished_at=? WHERE id=?", (db.CANCELLED, time.time(), jid))
    return {"ok": True}


@app.post("/api/jobs/{jid}/reprint")
def reprint(jid: int):
    j = _job(jid)
    if not j["file_id"] or not db.file_row(j["file_id"]):
        raise HTTPException(409, "The file for this job has been deleted")
    base = j["name"].split(" (")[0]
    return {"job_ids": fleet.queue_file(j["file_id"], 1, j["target_printer_id"], base)}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: int):
    j = _job(jid)
    if j["status"] not in db.DONE_STATES:
        raise HTTPException(409, "Only finished jobs can be removed from history")
    db.ex("DELETE FROM jobs WHERE id=?", (jid,))
    return {"ok": True}


# ------------------------------------------------------------------ settings / events
@app.get("/api/settings")
def get_settings():
    return db.get_settings()


@app.put("/api/settings")
def put_settings(body: dict = Body(...)):
    for k, v in body.items():
        if k in db.DEFAULT_SETTINGS:
            db.set_setting(k, v)
    return db.get_settings()


@app.post("/api/settings/test-webhook")
async def test_webhook():
    await fleet.notify(None, "test", "Test notification from Local Print Factory")
    return {"ok": True}


@app.get("/api/events")
def events(limit: int = 200):
    return db.q("SELECT e.*, p.name AS printer_name FROM events e LEFT JOIN printers p ON p.id=e.printer_id "
                "ORDER BY e.id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------------ static UI
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/favicon.svg")
def favicon():
    return FileResponse(STATIC / "favicon.svg")


@app.get("/healthz", include_in_schema=False)
def healthz():
    """For Docker / Coolify health checks. No login needed and reveals nothing."""
    return {"ok": True, "version": VERSION}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
