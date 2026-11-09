import io
import time
import zipfile

from fastapi.testclient import TestClient

from app.main import app
from tests.make_sample import make

c = TestClient(app)


def test_backup_and_restore_round_trip():
    folder = c.post("/api/folders", json={"name": f"Keep {time.time()}"}).json()
    f = c.post("/api/files", files={"files": ("keep.ufp", make("Ultimaker S5"))},
               data={"folder_id": str(folder["id"])}).json()[0]

    r = c.get("/api/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    backup = r.content
    names = zipfile.ZipFile(io.BytesIO(backup)).namelist()
    assert "localfactory.db" in names and any(n.startswith("files/") and n.endswith("keep.ufp") for n in names)

    # Lose the data...
    c.delete(f"/api/files/{f['id']}")
    c.delete(f"/api/folders/{folder['id']}")
    assert not any(x["id"] == f["id"] for x in c.get("/api/files").json())

    # ...and get it back.
    r = c.post("/api/restore", files={"file": ("b.zip", backup, "application/zip")})
    assert r.status_code == 200, r.text
    files = {x["id"]: x for x in c.get("/api/files").json()}
    assert files[f["id"]]["folder_id"] == folder["id"]
    assert c.get(f"/api/files/{f['id']}/download").content == make("Ultimaker S5")


def test_restore_rejects_other_files():
    assert c.post("/api/restore", files={"file": ("x.zip", b"not a zip")}).status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("localfactory.db", b"x")
        z.writestr("../../etc/evil", b"x")          # path tricks are refused
    assert c.post("/api/restore", files={"file": ("x.zip", buf.getvalue())}).status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("localfactory.db", b"definitely not sqlite")
    assert c.post("/api/restore", files={"file": ("x.zip", buf.getvalue())}).status_code == 400
    assert c.get("/api/files").status_code == 200    # still working afterwards
