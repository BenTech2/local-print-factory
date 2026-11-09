"""The fleet engine: polls every printer, tracks queued jobs, dispatches work, fires notifications."""
import asyncio
import json
import logging
import shutil
import time
import uuid as uuidlib

import httpx

from . import db
from .config import FILES_DIR, HOTFOLDER_DIR, POLL_INTERVAL, THUMBS_DIR
from .drivers import Driver, PrinterError, make_driver, norm_model
from .drivers.ultimaker import material_info  # noqa: F401  (used by main)
from .files import allowed, extract_metadata

log = logging.getLogger("fleet")

# printer_id -> live state dict (in memory only; rebuilt by polling)
STATE: dict[int, dict] = {}
_drivers: dict[int, tuple[tuple, Driver]] = {}
_sending: set[int] = set()          # printer ids with an upload in flight


def driver_for(p: dict) -> Driver:
    key = (p["host"], p.get("kind"), p.get("model"), p.get("auth_id"), p.get("auth_key"))
    cached = _drivers.get(p["id"])
    if cached and cached[0] == key:
        return cached[1]
    if cached:
        asyncio.get_running_loop().create_task(cached[1].close())
    d = make_driver(p)
    _drivers[p["id"]] = (key, d)
    return d


def reset():
    """Drop all cached printer connections and state, e.g. after restoring a backup."""
    STATE.clear()
    _sending.clear()
    for _, d in list(_drivers.values()):
        asyncio.get_running_loop().create_task(d.close())
    _drivers.clear()


def forget_printer(pid: int):
    STATE.pop(pid, None)
    cached = _drivers.pop(pid, None)
    if cached:
        asyncio.get_running_loop().create_task(cached[1].close())


# ---------------------------------------------------------------- notifications
async def notify(printer_id, kind, message):
    db.log_event(printer_id, kind, message)
    url = db.get_settings().get("webhook_url", "").strip()
    if not url:
        return
    payload = {"event": kind, "message": message, "text": message, "content": message,
               "printer_id": printer_id, "time": time.time()}
    try:
        async with httpx.AsyncClient(timeout=8) as h:
            if "ntfy" in url:
                await h.post(url, content=message.encode(), headers={"Title": f"Printer: {kind}"})
            else:
                await h.post(url, json=payload)
    except httpx.HTTPError as e:
        log.warning("webhook failed: %s", e)


# ---------------------------------------------------------------- polling
async def poll_printer(p):
    prev = STATE.get(p["id"], {})
    d = driver_for(p)
    try:
        raw = await d.poll()
        st = {**raw, "online": True, "last_seen": time.time(), "error": None}
    except (PrinterError, ValueError) as e:
        st = dict(prev)
        st.update({"online": False, "error": str(e), "status": "offline"})
        if prev.get("online"):
            await notify(p["id"], "offline", f"{p['name']} went offline: {e}")
    st.update(paired=d.paired, kind=d.kind, caps=sorted(d.caps), manual_clear=d.manual_clear)
    if st.get("online") and d.manual_clear:
        was_printing = (prev.get("job") or {}).get("state") not in (None, "wait_cleanup")
        _apply_plate_clear(p, st, was_printing)
    STATE[p["id"]] = st

    if st.get("online"):
        await _events_from_transition(p, prev, st)
        await _track_jobs(p, d, st)


def _apply_plate_clear(p, st, was_printing=False):
    """Printers that don't ask about the build plate themselves are held after every print
    until someone clicks 'Plate is clear', so the queue can never print onto a finished part."""
    job = st.get("job")
    pending = json.loads(p["clear_pending"]) if p.get("clear_pending") else None
    if job and job["state"] != "wait_cleanup":
        if pending:  # a new print started (e.g. from the printer's screen) - plate was evidently cleared
            db.ex("UPDATE printers SET clear_pending=NULL WHERE id=?", (p["id"],))
        return
    if job:  # finished / stopped / failed
        key = job.get("clear_key") or job.get("uuid")
        # Seen ending right now -> always hold. Otherwise (e.g. after a restart) hold unless already confirmed.
        if (was_printing or key != p.get("cleared_key")) and (not pending or pending["key"] != key):
            pending = {"key": key, **{k: job.get(k) for k in
                                      ("name", "uuid", "result", "progress", "time_total", "time_elapsed")}}
            db.ex("UPDATE printers SET clear_pending=? WHERE id=?", (json.dumps(pending), p["id"]))
    if pending and pending.get("unknown"):
        # Just added (or moved to a new address): we can't see what's on the bed, so a person has to look first.
        st["job"] = None
        st["needs_clear"] = True
        st["clear_reason"] = "unknown"
    elif pending:
        st["job"] = {**{k: v for k, v in pending.items() if k != "key"}, "state": "wait_cleanup",
                     "remaining": 0, "clear_key": pending["key"]}
        st["status"] = "printing"
        st["needs_clear"] = True
    else:
        st["job"] = None


# Stored on printers that don't check their own bed when they are added, so the first job waits for a person.
UNKNOWN_PLATE = json.dumps({"key": "__unknown__", "unknown": True})


def mark_plate_clear(p):
    pending = json.loads(p["clear_pending"]) if p.get("clear_pending") else None
    job = (STATE.get(p["id"]) or {}).get("job") or {}
    key = (pending or {}).get("key") or job.get("clear_key")
    db.ex("UPDATE printers SET clear_pending=NULL, cleared_key=? WHERE id=?", (key, p["id"]))
    st = STATE.get(p["id"])
    if st and st.get("needs_clear"):
        st.update(job=None, status="idle", needs_clear=False)


async def _events_from_transition(p, prev, st):
    if not prev.get("online"):
        if "online" in prev:
            await notify(p["id"], "online", f"{p['name']} is back online")
        return
    if st["status"] == "error" and prev.get("status") != "error":
        await notify(p["id"], "error", f"{p['name']} reports an error - check the printer")
    pj, nj = prev.get("job") or {}, st.get("job") or {}
    if nj.get("state") in ("post_print", "wait_cleanup") and pj.get("state") not in ("post_print", "wait_cleanup"):
        result = nj.get("result") or "Finished"
        await notify(p["id"], "print_" + result.lower(),
                     f"{p['name']}: '{nj.get('name')}' {result.lower()} - clear the build plate to continue")
    if nj.get("state") == "wait_user_action" and pj.get("state") != "wait_user_action":
        await notify(p["id"], "action_needed", f"{p['name']} needs attention on its screen ('{nj.get('name')}')")
    if nj.get("state") == "paused" and pj.get("state") != "paused":
        await notify(p["id"], "paused", f"{p['name']} paused ({nj.get('pause_source') or 'unknown reason'})")


RESULT_MAP = {"finished": db.FINISHED, "aborted": db.ABORTED, "failed": db.FAILED}


async def _track_jobs(p, d: Driver, st):
    """Follow our dispatched jobs on this printer through to a result."""
    active = db.q("SELECT * FROM jobs WHERE printer_id=? AND status IN (?,?,?)",
                  (p["id"], db.SENT, db.PRINTING, db.PAUSED))
    rj = st.get("job")
    for job in active:
        matches = rj and (rj["uuid"] == job["remote_uuid"] or
                          (not job["remote_uuid"] and rj["name"] == job["name"]))
        if matches:
            if not job["remote_uuid"]:
                db.ex("UPDATE jobs SET remote_uuid=? WHERE id=?", (rj["uuid"], job["id"]))
            state = rj["state"]
            if state in ("post_print", "wait_cleanup") and rj.get("result"):
                await _finish(job, rj["result"])
            elif state == "paused":
                db.ex("UPDATE jobs SET status=?, progress=?, message=? WHERE id=?",
                      (db.PAUSED, rj["progress"], f"Paused ({rj.get('pause_source') or '?'})", job["id"]))
            elif state not in ("post_print", "wait_cleanup"):
                msg = "Waiting for action on printer screen" if state == "wait_user_action" else None
                db.ex("UPDATE jobs SET status=?, progress=?, message=? WHERE id=?",
                      (db.PRINTING, rj["progress"], msg, job["id"]))
            continue
        # Our job is no longer the printer's current job.
        if job["status"] == db.SENT and time.time() - (job["sent_at"] or 0) < 90:
            continue  # printer may still be ingesting the file
        result = None
        if job["remote_uuid"]:
            try:
                result = await d.job_result(job["remote_uuid"])
            except PrinterError:
                return
        if result:
            await _finish(job, result)
        else:
            db.ex("UPDATE jobs SET status=?, message=?, finished_at=? WHERE id=?",
                  (db.ERROR, "Lost track of the job on the printer (no result in its history)",
                   time.time(), job["id"]))


async def _finish(job, result):
    status = RESULT_MAP.get(str(result).lower(), db.FINISHED)
    db.ex("UPDATE jobs SET status=?, progress=?, finished_at=?, message=? WHERE id=?",
          (status, 1.0 if status == db.FINISHED else job["progress"], time.time(),
           None if status == db.FINISHED else f"Printer reported: {result}", job["id"]))


# ---------------------------------------------------------------- dispatch
def compatibility(f: dict, st: dict, extensions, strict_materials: bool):
    """Can file row `f` go to a printer in state `st`? Returns (blocking problems, warnings)."""
    hard, warn = [], []
    meta = f.get("meta") or {}
    name = f.get("filename", "").lower()
    if extensions and not name.endswith(tuple(extensions)):
        kinds = ", ".join(extensions)
        hard.append(f"this printer only takes {kinds} files")
        return hard, warn
    machine = meta.get("machine")
    model = st.get("match_model")
    if machine and model and norm_model(machine) != norm_model(model):
        hard.append(f"sliced for {machine}, printer is {model}")
    elif not machine:
        warn.append("the file doesn't say which printer it was sliced for")
    exs = {str(e["index"]): e for e in st.get("extruders", [])}
    for idx, want in (meta.get("extruders") or {}).items():
        have = exs.get(idx)
        if not have:
            continue
        g = want.get("material_guid")
        if g and have.get("material_guid") and g != have["material_guid"]:
            msg = f"extruder {int(idx) + 1}: file wants a different material than is loaded"
            (hard if strict_materials else warn).append(msg)
        nz = want.get("nozzle")
        hot = have.get("hotend") or ""
        if nz and hot and hot != "Nozzle" and nz.replace(" ", "").lower() != hot.replace(" ", "").lower():
            warn.append(f"extruder {int(idx) + 1}: file wants {nz}, printer has {hot}")
    return hard, warn


def printer_ready(p, st):
    return (st.get("online") and st.get("paired") and p["accept_jobs"]
            and st.get("status") == "idle" and not st.get("needs_clear")
            and (not st.get("job") or st["job"].get("state") in (None, "none"))
            and p["id"] not in _sending)


async def dispatch():
    settings = db.get_settings()
    strict = settings.get("strict_materials") == "1"
    printers = db.q("SELECT * FROM printers ORDER BY id")
    busy = {r["printer_id"] for r in db.q(
        "SELECT printer_id FROM jobs WHERE status IN (?,?)", (db.SENDING, db.SENT))}
    queued = db.q("SELECT * FROM jobs WHERE status=? ORDER BY position", (db.QUEUED,))
    if not queued:
        return
    for p in printers:
        st = STATE.get(p["id"], {})
        if p["id"] in busy or not printer_ready(p, st):
            continue
        d = driver_for(p)
        for job in queued:
            if job["target_printer_id"] not in (None, p["id"]):
                continue
            f = db.file_row(job["file_id"]) if job["file_id"] else None
            if not f:
                db.ex("UPDATE jobs SET status=?, message=? WHERE id=?", (db.ERROR, "File was deleted", job["id"]))
                continue
            hard, _ = compatibility(f, st, d.extensions, strict)
            if not hard and not f["meta"].get("machine") and job["target_printer_id"] is None:
                db.ex("UPDATE jobs SET message=? WHERE id=?",
                      ("Waiting: file doesn't say which printer it is for - choose a printer for it", job["id"]))
                continue
            if hard:
                if job["target_printer_id"] == p["id"]:
                    db.ex("UPDATE jobs SET message=? WHERE id=?", ("Blocked: " + "; ".join(hard), job["id"]))
                continue
            queued.remove(job)
            _sending.add(p["id"])
            asyncio.create_task(_send(p, d, job, f))
            break
    _explain_stuck_jobs(queued, printers, strict)


def _explain_stuck_jobs(queued, printers, strict):
    """Tell the user when no printer in the fleet could ever take a queued file."""
    for job in queued:
        if job["target_printer_id"] or not job["file_id"]:
            continue
        f = db.file_row(job["file_id"])
        if not f or not f["meta"].get("machine"):
            continue
        fits = any(not compatibility(f, STATE.get(p["id"], {}), driver_for(p).extensions, strict)[0]
                   for p in printers)
        msg = None if fits else f"No printer in your fleet can take this file (sliced for {f['meta']['machine']})"
        if (job["message"] or None) != msg and (msg or (job["message"] or "").startswith("No printer in")):
            db.ex("UPDATE jobs SET message=? WHERE id=?", (msg, job["id"]))


async def _send(p, d: Driver, job, f):
    db.ex("UPDATE jobs SET status=?, printer_id=?, printer_name=?, message=?, sent_at=? WHERE id=?",
          (db.SENDING, p["id"], p["name"], "Uploading to printer...", time.time(), job["id"]))
    try:
        data = (FILES_DIR / f["stored_name"]).read_bytes()
        remote = await d.upload_and_print(job["name"], f["filename"], data)
        db.ex("UPDATE jobs SET status=?, remote_uuid=?, message=?, sent_at=? WHERE id=?",
              (db.SENT, remote, None, time.time(), job["id"]))
        await notify(p["id"], "job_started", f"Sent '{job['name']}' to {p['name']}")
    except (PrinterError, OSError) as e:
        db.ex("UPDATE jobs SET status=?, printer_id=NULL, printer_name=NULL, message=? WHERE id=?",
              (db.QUEUED, f"Last attempt on {p['name']} failed: {e}", job["id"]))
        log.warning("send to %s failed: %s", p["name"], e)
    finally:
        _sending.discard(p["id"])


# ---------------------------------------------------------------- library
def add_file(filename: str, data: bytes, folder_id: int | None = None) -> int:
    meta, thumb = extract_metadata(filename, data)
    stored = f"{uuidlib.uuid4().hex}_{filename}"
    (FILES_DIR / stored).write_bytes(data)
    fid = db.ex("INSERT INTO files(filename, stored_name, size, meta, has_thumb, folder_id, uploaded_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (filename, stored, len(data), json.dumps(meta), 1 if thumb else 0, folder_id, time.time()))
    if thumb:
        (THUMBS_DIR / f"{fid}.png").write_bytes(thumb)
    return fid


def queue_file(fid: int, copies=1, target_printer_id=None, name=None):
    f = db.file_row(fid)
    base = name or f["filename"].rsplit(".gcode", 1)[0].rsplit(".ufp", 1)[0]
    ids = []
    for i in range(max(1, min(int(copies), 100))):
        jname = base if copies == 1 else f"{base} ({i + 1}/{copies})"
        ids.append(db.ex("INSERT INTO jobs(file_id, name, target_printer_id, status, position, created_at) "
                         "VALUES(?,?,?,?,?,?)",
                         (fid, jname, target_printer_id, db.QUEUED, db.next_position(), time.time())))
    return ids


def folder_for_path(parts) -> int | None:
    """Find or create the Library folder for a path like ('Project A', 'Plates'). Names match case-insensitively."""
    parent = None
    for name in parts:
        name = name.strip()[:100]
        if not name:
            continue
        row = db.one("SELECT id FROM folders WHERE name=? COLLATE NOCASE AND parent_id IS ?", (name, parent))
        parent = row["id"] if row else db.ex("INSERT INTO folders(name, parent_id, created_at) VALUES(?,?,?)",
                                             (name, parent, time.time()))
    return parent


def scan_hotfolder():
    """Import print files saved into the hot folder. Sub-folders become Library folders."""
    imported = []
    for path in sorted(HOTFOLDER_DIR.rglob("*")):
        rel = path.relative_to(HOTFOLDER_DIR)
        if not path.is_file() or not allowed(path.name) or rel.parts[0] == "imported" \
                or any(part.startswith(".") for part in rel.parts):
            continue
        if time.time() - path.stat().st_mtime < 3:  # still being written
            continue
        fid = add_file(path.name, path.read_bytes(), folder_for_path(rel.parts[:-1]))
        done = HOTFOLDER_DIR / "imported"
        done.mkdir(exist_ok=True)
        shutil.move(str(path), str(done / f"{int(time.time())}_{path.name}"))
        if db.get_settings().get("hotfolder_autoqueue") == "1":
            queue_file(fid)
        imported.append(str(rel))
    return imported


# ---------------------------------------------------------------- loops
async def run_forever():
    tick = 0
    while True:
        started = time.monotonic()
        try:
            printers = db.q("SELECT * FROM printers")
            await asyncio.gather(*(poll_printer(p) for p in printers))
            await dispatch()
            if tick % 3 == 0:
                await asyncio.to_thread(scan_hotfolder)
        except Exception:  # never let the loop die
            log.exception("fleet loop error")
        tick += 1
        await asyncio.sleep(max(0.2, POLL_INTERVAL - (time.monotonic() - started)))
