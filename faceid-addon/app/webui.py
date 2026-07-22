"""Review-UI + JSON-API: Personen verwalten, Unknown-Cluster zuordnen, letzte Erkennungen."""
import base64
import json
import logging
import secrets
import threading
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import FaceEngine, crop_face

log = logging.getLogger("faceid.web")


class AssignBody(BaseModel):
    ids: list[str]
    person: str  # Slug einer bestehenden ODER Name einer neuen Person


class NameBody(BaseModel):
    name: str


def build_app(cfg, engine, gallery, processor, data_dir: Path, static_dir: Path) -> FastAPI:
    app = FastAPI(title="FaceID")

    # Optionales HTTP Basic Auth (config: faceid.auth.user/password). Als Middleware,
    # damit auch der /data-Static-Mount (Gesichtsbilder!) geschützt ist.
    auth = cfg["faceid"].get("auth") or {}
    if auth.get("user") and auth.get("password"):
        expected = base64.b64encode(f"{auth['user']}:{auth['password']}".encode()).decode()
        log.info("HTTP Basic Auth aktiv (User %s)", auth["user"])

        @app.middleware("http")
        async def basic_auth(request, call_next):
            header = request.headers.get("authorization", "")
            if header.startswith("Basic ") and secrets.compare_digest(header[6:], expected):
                return await call_next(request)
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="FaceID"'})

    app.mount("/data", StaticFiles(directory=data_dir), name="data")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/api/persons")
    def persons():
        return gallery.persons()

    @app.post("/api/persons")
    def create_person(body: NameBody):
        return {"slug": gallery.create_person(body.name)}

    @app.delete("/api/persons/{slug}")
    def delete_person(slug: str):
        gallery.delete_person(slug)
        return {"ok": True}

    @app.delete("/api/persons/{slug}/faces/{fname}")
    def delete_face(slug: str, fname: str):
        gallery.delete_face(slug, fname)
        return {"ok": True}

    @app.post("/api/persons/{slug}/faces/{fname}/unassign")
    def unassign_face(slug: str, fname: str):
        ok = gallery.unassign_face(slug, fname)
        if ok:
            gallery.refresh_guesses()
        return {"ok": ok}

    @app.post("/api/persons/{slug}/photos")
    async def upload_photos(slug: str, files: list[UploadFile]):
        """Fotos (z. B. aus der Foto-Library) hochladen: Gesicht extrahieren + einlernen."""
        if slug not in gallery.persons():
            raise HTTPException(404, "Unknown person")
        added, skipped = 0, []
        for uf in files:
            raw = await uf.read()
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                skipped.append(f"{uf.filename}: not an image")
                continue
            if max(img.shape[:2]) > 2000:  # Foto-Library-Bilder einkürzen, Detection reicht so
                s = 2000 / max(img.shape[:2])
                img = cv2.resize(img, None, fx=s, fy=s)
            face = FaceEngine.best_face(engine.faces(img), min_px=60)
            if face is None:
                skipped.append(f"{uf.filename}: no face found")
                continue
            gallery.add_face(slug, crop_face(img, face.bbox), face.normed_embedding)
            added += 1
        return {"added": added, "skipped": skipped}

    @app.get("/api/unknowns")
    def unknowns():
        clusters = gallery.unknown_clusters(eps=float(cfg["faceid"].get("cluster_eps", 0.45)))
        frigate_url = cfg["frigate"]["url"].rstrip("/")
        for c in clusters:
            for u in c:
                if u.pop("has_full", False):
                    u["full_url"] = f"data/unknowns/{u['id']}_full.jpg"
                elif u.get("event_id"):
                    # Backfill-Bestand: Vollbild live aus Frigate (solange Event-Retention reicht)
                    u["full_url"] = f"{frigate_url}/api/events/{u['event_id']}/snapshot.jpg"
        return JSONResponse(clusters)

    @app.post("/api/unknowns/assign")
    def assign(body: AssignBody):
        persons_now = gallery.persons()
        slug = body.person if body.person in persons_now else gallery.create_person(body.person)
        name = gallery.persons()[slug]["name"]
        n = 0
        for uid in body.ids:
            jf = gallery.unknown_dir / f"{uid}.json"
            meta = {}
            if jf.exists():
                try:
                    meta = json.loads(jf.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            if gallery.assign_unknown(uid, slug):
                n += 1
                # Zuordnung ans Original-Event zurückspielen (Mensch bestätigt -> Score 1.0)
                if meta.get("event_id"):
                    processor.frigate.set_sub_label(meta["event_id"], name, 1.0)
        gallery.refresh_guesses()
        return {"assigned": n, "slug": slug}

    @app.post("/api/unknowns/auto_assign")
    def auto_assign():
        """Alle Unknowns mit Galerie-Match >= match_threshold der vorgeschlagenen Person zuordnen."""
        thr = float(cfg["faceid"].get("match_threshold", 0.5))
        assigned: dict[str, int] = {}
        for it in gallery.unknowns():
            slug, name, score = gallery.match(it["embedding"])
            if slug and score >= thr and gallery.assign_unknown(it["id"], slug):
                assigned[name] = assigned.get(name, 0) + 1
                if it.get("event_id"):
                    processor.frigate.set_sub_label(it["event_id"], name, score)
        gallery.refresh_guesses()
        return {"assigned": assigned, "total": sum(assigned.values())}

    @app.post("/api/unknowns/discard")
    def discard(body: AssignBody):
        for uid in body.ids:
            gallery.discard_unknown(uid)
        return {"ok": True}

    backfill_state = {"running": False, "processed": 0, "total": 0, "result": None, "days": 0}

    class BackfillBody(BaseModel):
        days: int = 14

    @app.post("/api/backfill")
    def start_backfill(body: BackfillBody):
        if backfill_state["running"]:
            raise HTTPException(409, "History scan already running")
        days = max(1, min(int(body.days), 60))
        backfill_state.update(running=True, processed=0, total=0, result=None, days=days)

        def progress(i, total):
            backfill_state.update(processed=i, total=total)

        def worker():
            from .backfill import run_backfill
            try:
                stats = run_backfill(
                    engine, gallery, processor.frigate, cfg["frigate"]["url"], days=days,
                    tag=bool(cfg["faceid"].get("set_sub_label", True)),
                    match_thr=float(cfg["faceid"].get("match_threshold", 0.5)),
                    progress=progress)
                backfill_state["result"] = stats
            except Exception as e:
                log.exception("Verlaufs-Scan fehlgeschlagen")
                backfill_state["result"] = {"error": str(e)}
            finally:
                backfill_state["running"] = False

        threading.Thread(target=worker, daemon=True, name="faceid-backfill").start()
        return {"started": True, "days": days}

    @app.get("/api/backfill")
    def backfill_status():
        return backfill_state

    @app.get("/api/recent")
    def recent():
        return list(processor.recent)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "persons": len(gallery.persons()),
                "queue": processor.queue.qsize(), "open_events": len(processor.events)}

    return app
