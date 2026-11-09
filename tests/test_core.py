import gzip

from app.drivers import norm_model
from app.drivers.sdcp import SdcpDriver
from app.files import extract_metadata
from app.fleet import compatibility
from tests.make_sample import make, make_cura_marlin, make_orca

GUID = "2f9d2279-9b0e-4765-bf9b-d1e1e13f3c49"
UM_EXT = (".ufp", ".gcode", ".gcode.gz")


def frow(name, data):
    meta, _ = extract_metadata(name, data)
    return {"filename": name, "meta": meta}


def test_ufp_metadata_and_thumbnail():
    meta, thumb = extract_metadata("part.ufp", make("Ultimaker S5", 1234))
    assert meta["machine"] == "Ultimaker S5" and meta["print_time"] == 1234
    assert meta["extruders"]["0"]["material_guid"] == GUID
    assert thumb.startswith(b"\x89PNG")


def test_gcode_gz_metadata():
    g = gzip.compress(b";FLAVOR:Griffin\n;TARGET_MACHINE.NAME:Ultimaker S3\n;PRINT.TIME:60\n;END_OF_HEADER\nG28\n")
    meta, thumb = extract_metadata("x.gcode.gz", g)
    assert meta["machine"] == "Ultimaker S3" and thumb is None


def test_cura_marlin_for_neptune():
    meta, _ = extract_metadata("b.gcode", make_cura_marlin("ELEGOO NEPTUNE 4 PRO", 900))
    assert meta["machine"] == "ELEGOO NEPTUNE 4 PRO" and meta["print_time"] == 900


def test_orca_elegoo_slicer_tail_config_and_thumbnail():
    meta, thumb = extract_metadata("v.gcode", make_orca("Elegoo Centauri Carbon", 25))
    assert meta["machine"] == "Elegoo Centauri Carbon"   # read from the config block at the END of the file
    assert meta["print_time"] == 25 * 60
    assert thumb.startswith(b"\x89PNG")


def test_unknown_gcode_warns():
    meta, _ = extract_metadata("x.gcode", b"G28\nG1 X1\n")
    assert "warning" in meta and "machine" not in meta


def test_model_names_normalise():
    assert norm_model("ELEGOO NEPTUNE 4 PRO") == norm_model("Elegoo Neptune 4 Pro") == "neptune4pro"
    assert norm_model("Elegoo Neptune 4") != norm_model("Elegoo Neptune 4 Max")
    assert norm_model("Centauri Carbon") == norm_model("Elegoo Centauri Carbon")


def st(model="Ultimaker S5", guid=GUID, core="AA 0.4"):
    return {"match_model": model, "extruders": [{"index": 0, "hotend": core, "material_guid": guid}]}


def test_compatibility_ultimaker_rules():
    f = frow("p.ufp", make("Ultimaker S5"))
    assert compatibility(f, st(), UM_EXT, False) == ([], [])
    assert compatibility(f, st(model="Ultimaker S3"), UM_EXT, False)[0]          # wrong machine blocks
    hard, warn = compatibility(f, st(guid="other"), UM_EXT, False)
    assert not hard and warn                                                     # material only warns...
    assert compatibility(f, st(guid="other"), UM_EXT, True)[0]                    # ...unless strict
    assert compatibility(f, st(core="AA 0.8"), UM_EXT, False)[1]


def test_compatibility_mixed_fleet():
    n4p = frow("b.gcode", make_cura_marlin("ELEGOO NEPTUNE 4 PRO"))
    cc = frow("v.gcode", make_orca("Elegoo Centauri Carbon"))
    ufp = frow("p.ufp", make("Ultimaker S5"))
    elegoo = {"match_model": "Elegoo Neptune 4 Pro", "extruders": [{"index": 0, "hotend": "Nozzle", "material_guid": ""}]}
    centauri = {"match_model": "Centauri Carbon", "extruders": [{"index": 0, "hotend": "Nozzle", "material_guid": ""}]}
    assert compatibility(n4p, elegoo, (".gcode",), False) == ([], [])
    assert compatibility(cc, centauri, (".gcode",), False) == ([], [])
    assert compatibility(n4p, centauri, (".gcode",), False)[0]       # Neptune file never goes to the Centauri
    assert compatibility(cc, elegoo, (".gcode",), False)[0]
    assert compatibility(ufp, elegoo, (".gcode",), False)[0]         # .ufp is Ultimaker-only
    assert compatibility(n4p, st(), UM_EXT, False)[0]                 # Neptune gcode never goes to an Ultimaker


def test_sdcp_status_parsing():
    d = SdcpDriver({"host": "10.0.0.5", "model": None})
    d.attrs = {"MachineName": "Centauri Carbon", "FirmwareVersion": "V1.1.25"}
    out = d._normalise({"TempOfNozzle": [220, 219.5], "TempTargetNozzle": 220, "TempOfHotbed": 60,
                        "PrintInfo": {"Status": 13, "Filename": "/local/vase.gcode", "TaskId": "t1",
                                      "CurrentTicks": 600, "TotalTicks": 1200, "Progress": 50,
                                      "CurrentLayer": 50, "TotalLayer": 100}})
    assert out["status"] == "printing" and out["job"]["uuid"] == "vase.gcode"
    assert out["job"]["progress"] == 0.5 and out["job"]["remaining"] == 600
    assert out["extruders"][0]["temp"] == 219.5
    done = d._normalise({"PrintInfo": {"Status": 9, "Filename": "vase.gcode", "TaskId": "t1"}})
    assert done["status"] == "idle" and done["job"]["state"] == "wait_cleanup" and done["job"]["result"] == "Finished"
    assert d._normalise({"PrintInfo": {"Status": 0}})["job"] is None


def test_new_elegoo_printer_waits_for_bed_check():
    """A printer that doesn't check its own bed must not get a job until someone confirms the bed is empty -
    even when it reports nothing finished (e.g. Klipper restarted since the last print)."""
    import time
    from app import db, fleet
    pid = db.ex("INSERT INTO printers(name, host, kind, model, clear_pending, created_at) VALUES(?,?,?,?,?,?)",
                ("N4", f"t-{time.time()}", "moonraker", "Elegoo Neptune 4 Max", fleet.UNKNOWN_PLATE, time.time()))
    p = db.one("SELECT * FROM printers WHERE id=?", (pid,))
    st = {"online": True, "paired": True, "status": "idle", "job": None}
    fleet._apply_plate_clear(p, st)
    assert st["needs_clear"] and not fleet.printer_ready(p, st)

    fleet.STATE[pid] = st
    fleet.mark_plate_clear(p)
    p = db.one("SELECT * FROM printers WHERE id=?", (pid,))
    st = {"online": True, "paired": True, "status": "idle", "job": None}
    fleet._apply_plate_clear(p, st)
    assert not st.get("needs_clear") and fleet.printer_ready(p, st)
