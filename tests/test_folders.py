import os
import time

from fastapi.testclient import TestClient

from app import fleet
from app.config import HOTFOLDER_DIR
from app.main import app
from tests.make_sample import make

c = TestClient(app)   # no "with": the printer polling loop isn't started


def upload(name, folder_id=None):
    data = {"folder_id": str(folder_id)} if folder_id else {}
    r = c.post("/api/files", files={"files": (name, make("Ultimaker S5"))}, data=data)
    assert r.status_code == 200, r.text
    return r.json()[0]


def test_folders_nest_move_and_delete_without_losing_files():
    proj = c.post("/api/folders", json={"name": f"Project {time.time()}"}).json()
    plates = c.post("/api/folders", json={"name": "Plates", "parent_id": proj["id"]}).json()
    assert c.post("/api/folders", json={"name": "plates", "parent_id": proj["id"]}).status_code == 409  # duplicate
    assert c.post("/api/folders", json={"name": "  "}).status_code == 400

    f = upload("part.ufp", plates["id"])
    assert f["folder_id"] == plates["id"]
    assert c.patch(f"/api/folders/{proj['id']}", json={"parent_id": plates["id"]}).status_code == 400  # cycle

    moved = c.patch(f"/api/files/{f['id']}", json={"folder_id": proj["id"]}).json()
    assert moved["folder_id"] == proj["id"]
    assert c.patch(f"/api/files/{f['id']}", json={"filename": "renamed.txt"}).status_code == 400

    # Deleting a folder moves its files and sub-folders up a level - nothing is deleted.
    assert c.delete(f"/api/folders/{proj['id']}").status_code == 200
    files = {x["id"]: x for x in c.get("/api/files").json()}
    folders = {x["id"]: x for x in c.get("/api/folders").json()}
    assert files[f["id"]]["folder_id"] is None
    assert folders[plates["id"]]["parent_id"] is None


def test_hotfolder_subfolders_become_library_folders():
    name = f"Hot {time.time()}"
    d = HOTFOLDER_DIR / name / "Batch 1"
    d.mkdir(parents=True)
    p = d / "bracket.ufp"
    p.write_bytes(make("Ultimaker S5"))
    old = time.time() - 10
    os.utime(p, (old, old))
    assert any("bracket.ufp" in x for x in fleet.scan_hotfolder())
    folders = c.get("/api/folders").json()
    top = next(x for x in folders if x["name"] == name)
    batch = next(x for x in folders if x["name"] == "Batch 1" and x["parent_id"] == top["id"])
    assert any(x["filename"] == "bracket.ufp" and x["folder_id"] == batch["id"] for x in c.get("/api/files").json())
