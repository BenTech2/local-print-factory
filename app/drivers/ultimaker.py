"""Ultimaker 3 / S-series via the printer's local /api/v1 (digest auth after pairing)."""
import asyncio
import logging
import xml.etree.ElementTree as ET

from .. import db
from ..printer_client import UltimakerClient
from .base import Driver, PrinterError

log = logging.getLogger("ultimaker")
_material_lookups: set[str] = set()


def material_info(guid):
    if not guid:
        return None
    return db.one("SELECT * FROM materials WHERE guid=?", (guid,))


class UltimakerDriver(Driver):
    kind = "ultimaker"
    label = "Ultimaker"
    caps = frozenset({"pause", "resume", "abort", "camera", "identify", "led", "message", "history", "pair"})
    extensions = (".ufp", ".gcode", ".gcode.gz")
    manual_clear = False  # the printer asks on its own screen
    needs_pairing = True

    def __init__(self, p):
        super().__init__(p)
        self.c = UltimakerClient(self.host, p.get("auth_id"), p.get("auth_key"))
        self._system = None
        self._tick = 0

    @property
    def paired(self):
        return self.c.paired

    async def close(self):
        await self.c.close()

    async def poll(self):
        printer = await self.c.printer()
        if printer is None:
            raise PrinterError("No Ultimaker API at this address (/api/v1 not found). If this is an Elegoo or "
                               "Klipper printer, remove it and add it again with the right printer type.")
        job = await self.c.print_job()
        if self._system is None or self._tick % 20 == 0:
            system = await self.c.system()
            if system is not None:
                try:
                    fw = await self.c._json("/system/firmware")
                    system["firmware"] = fw if isinstance(fw, str) else None
                except PrinterError:
                    pass
                self._system = system
        self._tick += 1
        return self._summarise(printer or {}, job, self._system or {})

    def _summarise(self, printer, job, system):
        heads = printer.get("heads") or [{}]
        extruders = []
        for i, ex in enumerate(heads[0].get("extruders") or []):
            hot = ex.get("hotend") or {}
            mat = ex.get("active_material") or {}
            guid = mat.get("guid") or mat.get("GUID") or ""
            info = material_info(guid)
            if guid and not info:
                asyncio.create_task(self._lookup_material(guid))
            extruders.append({
                "index": i, "hotend": hot.get("id") or "",
                "temp": (hot.get("temperature") or {}).get("current"),
                "target": (hot.get("temperature") or {}).get("target"),
                "material_guid": guid, "material": info,
                "remaining_mm": mat.get("length_remaining"),
            })
        bed = (printer.get("bed") or {}).get("temperature") or {}
        j = None
        if job:
            total, elapsed = job.get("time_total") or 0, job.get("time_elapsed") or 0
            j = {"name": job.get("name"), "uuid": job.get("uuid"), "state": job.get("state"),
                 "progress": job.get("progress") or 0, "time_total": total, "time_elapsed": elapsed,
                 "remaining": max(0, total - elapsed), "source": job.get("source"),
                 "result": job.get("result"), "pause_source": job.get("pause_source")}
        return {"status": printer.get("status", "unknown"), "variant": system.get("variant"),
                "match_model": system.get("variant"),
                "firmware": system.get("firmware"), "machine_name": system.get("name"),
                "extruders": extruders,
                "bed": {"temp": bed.get("current"), "target": bed.get("target")},
                "led": printer.get("led"), "job": j}

    async def _lookup_material(self, guid):
        if guid in _material_lookups:
            return
        _material_lookups.add(guid)
        try:
            xml = await self.c.material_xml(guid)
            if not xml:
                return
            vals = {}
            for el in ET.fromstring(xml).iter():
                tag = el.tag.split("}")[-1]
                if tag in ("brand", "material", "color", "color_code") and tag not in vals and el.text:
                    vals[tag] = el.text.strip()
            db.ex("INSERT OR REPLACE INTO materials(guid, brand, material, color, color_code) VALUES(?,?,?,?,?)",
                  (guid, vals.get("brand"), vals.get("material"), vals.get("color"), vals.get("color_code")))
        except (PrinterError, ET.ParseError) as e:
            log.debug("material lookup %s failed: %s", guid, e)
            _material_lookups.discard(guid)

    async def pause(self):
        await self.c.set_job_state("pause")

    async def resume(self):
        await self.c.set_job_state("print")

    async def abort(self):
        await self.c.set_job_state("abort")

    async def upload_and_print(self, jobname, filename, data):
        res = await self.c.upload(jobname, filename, data)
        return (res or {}).get("uuid")

    async def job_result(self, remote_id):
        h = await self.c.history_job(remote_id)
        return (h or {}).get("result")

    async def history(self):
        rows = await self.c.history(50)
        return [{"name": r.get("name"), "result": r.get("result"),
                 "started": (r.get("datetime_started") or "").replace("T", " ")[:16],
                 "duration": r.get("time_elapsed")} for r in rows]

    async def snapshot(self):
        return await self.c.snapshot()

    async def identify(self):
        await self.c.blink(2, 8)

    async def set_led(self, hue, saturation, brightness):
        await self.c.set_led(hue, saturation, brightness)

    async def display_message(self, text):
        await self.c.display_message(text)
