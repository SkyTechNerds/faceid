"""Review-UI + JSON-API: Personen verwalten, Unknown-Cluster zuordnen, letzte Erkennungen."""
import base64
import binascii
import io
import json
import logging
import secrets
import tarfile
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import logbuffer
from .engine import FaceEngine, crop_face, find_face_padded
from .backup_util import build_backup_gz, write_backup_file, prune_backups
from pathlib import Path as _P

log = logging.getLogger("faceid.web")


class AssignBody(BaseModel):
    ids: list[str]
    person: str  # Slug einer bestehenden ODER Name einer neuen Person


class NameBody(BaseModel):
    name: str


def _frigate_url(cfg) -> str:
    """Basis-URL aus der Konfiguration — leerer String, wenn kein Frigate konfiguriert ist.

    ``url:`` ohne Wert ergibt in YAML ``None``, nicht den fehlenden Schluessel. Ein
    ``.get("url", "")`` faengt deshalb nur den zweiten Fall ab, und ``None.rstrip("/")``
    nimmt danach den ganzen Endpunkt mit. Beide Formen muessen hier leer werden.
    """
    return ((cfg.get("frigate") or {}).get("url") or "").rstrip("/")


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

    # ---------- Identify (read-only, multi-face) ----------
    # Pure recogniser: detect every face in the upload, score each one against the
    # gallery, and return every candidate with its score.  Nothing is written —
    # no enrollment, no unknowns queue, no history, no MQTT, no HA REST — so the
    # caller (or operator acceptance test) can hammer it without side effects.
    #
    # The caller decides what to do with the candidate list (gate against the
    # thresholds in ``thresholds.match``, log a probe, etc.).  We only return the
    # raw signal: every candidate per face, sorted by score desc, capped at
    # ``top_k`` (default = gallery.match_top_k from config).

    def _identify_aliases() -> dict:
        return dict(cfg["faceid"].get("ha_display_aliases") or {})

    def _decode_identify_image(raw: bytes):
        if not raw:
            return None
        arr = np.frombuffer(raw, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def _build_identify_payload(faces, top_k: int) -> dict:
        aliases = _identify_aliases()
        persons_now = gallery.persons()
        match_thr = float(cfg["faceid"].get("match_threshold", 0.5))
        unknown_thr = float(cfg["faceid"].get("unknown_threshold", 0.35))
        out_faces = []
        for i, face in enumerate(faces):
            bbox = [int(round(float(v))) for v in face.bbox]
            det_score = float(face.det_score)
            # Per-person mean of top-k similarities.  Same scoring rule the
            # event processor uses, so a probe's "best" matches the system of
            # record — only the threshold gating lives in the caller.
            scored = []
            emb = face.normed_embedding
            for slug, info in persons_now.items():
                ref = gallery.embeddings(slug)
                if ref is None or not len(ref):
                    continue
                sims = ref @ emb
                k = min(int(top_k), len(sims))
                if k <= 0:
                    continue
                score = float(np.sort(sims)[-k:].mean())
                scored.append((score, slug, info["name"],
                               info.get("person_uuid"), len(info["files"])))
            scored.sort(key=lambda r: r[0], reverse=True)
            top = scored[:max(1, int(top_k))]
            candidates = [
                {
                    "person": aliases.get(name, name),
                    "slug": slug,
                    "person_uuid": uuid,
                    "score": round(score, 4),
                    "ref_count": ref_count,
                }
                for score, slug, name, uuid, ref_count in top
            ]
            if top:
                best_score, best_slug, best_name, best_uuid, _ = top[0]
                best = {
                    "person": aliases.get(best_name, best_name),
                    "slug": best_slug,
                    "person_uuid": best_uuid,
                    "score": round(best_score, 4),
                }
                above = best_score >= match_thr
            else:
                best = None
                above = False
            out_faces.append({
                "index": i,
                "bbox": bbox,
                "det_score": round(det_score, 4),
                "candidates": candidates,
                "best": best,
                "above_match_threshold": above,
            })
        return {
            "success": True,
            "faces": out_faces,
            "face_count": len(out_faces),
            "thresholds": {"match": match_thr, "unknown": unknown_thr},
        }

    @app.post("/api/identify")
    async def identify(request: Request,
                       image: UploadFile | None = None,
                       top_k: int | None = None,
                       min_face_px: int | None = None):
        """Read-only face identification — no enrollment, no events.

        Accepts either multipart/form-data (field ``image``) or
        application/json (body ``{"image_base64": "..."}``).  Returns every
        detected face with every gallery candidate scored against it (capped
        by ``top_k``).  Zero faces is success, not an error.
        """
        cfg_top_k = int(cfg["faceid"].get("match_top_k", 3))
        try:
            k_eff = int(top_k) if top_k is not None else cfg_top_k
        except (TypeError, ValueError):
            raise HTTPException(400, "top_k must be an integer")
        if k_eff < 1:
            raise HTTPException(400, "top_k must be >= 1")
        max_possible = max(1, len(gallery.persons()))
        k_eff = min(k_eff, max_possible)

        try:
            cfg_min_px = int(cfg["faceid"].get("min_face_px", 48))
        except (TypeError, ValueError):
            cfg_min_px = 48
        try:
            min_px = int(min_face_px) if min_face_px is not None else cfg_min_px
        except (TypeError, ValueError):
            raise HTTPException(400, "min_face_px must be an integer")
        if min_px < 1:
            raise HTTPException(400, "min_face_px must be >= 1")

        raw = b""
        content_type = ""
        if image is not None and image.filename:
            raw = await image.read()
            content_type = (image.content_type or "").lower()
        else:
            body_bytes = await request.body()
            if body_bytes:
                ctype = (request.headers.get("content-type") or "").lower()
                content_type = ctype
                if "application/json" in ctype:
                    try:
                        body = json.loads(body_bytes.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        raise HTTPException(400, "invalid JSON body")
                    b64 = body.get("image_base64")
                    if not isinstance(b64, str) or not b64:
                        raise HTTPException(
                            400,
                            "expected JSON {image_base64: '...'} "
                            "or multipart field 'image'")
                    try:
                        raw = base64.b64decode(b64, validate=True)
                    except (binascii.Error, ValueError):
                        raise HTTPException(400, "image_base64 is not valid base64")
                elif "multipart/" in ctype:
                    raise HTTPException(
                        400, "multipart body must include the 'image' file field")
                else:
                    # Allow raw image bytes with no explicit content-type.
                    raw = body_bytes

        if not raw:
            raise HTTPException(
                400,
                "missing image: send multipart field 'image' "
                "or JSON {image_base64: '...'}")

        bgr = _decode_identify_image(raw)
        if bgr is None:
            raise HTTPException(400, "image could not be decoded as JPEG/PNG")

        # Cap extremely large uploads so a 50 MP probe can't pin the model
        # thread.  Single-face detection normally rescales nothing.
        if max(bgr.shape[:2]) > 4000:
            scale = 4000 / max(bgr.shape[:2])
            bgr = cv2.resize(bgr, None, fx=scale, fy=scale)

        # Detect once, filter by min_face_px here so the response matches what
        # the live pipeline would have considered.
        all_faces = engine.faces(bgr)
        kept = [f for f in all_faces
                if min((float(f.bbox[2]) - float(f.bbox[0])),
                       (float(f.bbox[3]) - float(f.bbox[1]))) >= min_px]

        payload = _build_identify_payload(kept, k_eff)
        payload["request"] = {
            "content_type": content_type,
            "bytes": len(raw),
            "top_k": k_eff,
            "min_face_px": min_px,
            "detected_before_filter": len(all_faces),
        }
        return payload

    class FavBody(BaseModel):
        favorite: bool

    @app.post("/api/gallery/quality-scan")
    @app.post("/api/gallery/cross-risk-scan")  # Name bis v0.14 — bleibt gueltig
    def quality_scan():
        """Referenzen pruefen: solche, die zwei Personen verwechselbar machen, und solche,
        die zur eigenen Person gar nicht passen."""
        thr = gallery.cross_risk_threshold
        if thr <= 0 and gallery.self_outlier_ratio <= 0:
            raise HTTPException(400, "reference check is disabled "
                                     "(cross_risk_margin < 0 and self_outlier_ratio <= 0)")
        removed = gallery.quality_scan(thr)
        n_out = sum(1 for r in removed if r["kind"] == "outlier")
        log.info("reference check: %d set aside — %d ambiguous (threshold %.2f), "
                 "%d not resembling their own person", len(removed), len(removed) - n_out,
                 thr, n_out)
        return {"ok": True, "threshold": round(thr, 3), "removed": removed}

    # ---------- Verlauf der Meldungen ----------

    @app.get("/api/history")
    def history_list(limit: int = 100):
        """Was wurde gemeldet — mit dem Ausschnitt, der dafuer benutzt wurde."""
        h = getattr(processor, "history", None)
        if h is None:
            return {"enabled": False, "items": []}
        return {"enabled": True,
                "items": h.items(limit, gallery=gallery,
                                 threshold=float(cfg["faceid"].get("match_threshold", 0.5)))}

    @app.post("/api/history/{hid}/wrong")
    def history_wrong(hid: str):
        """'War falsch' — ausrechnen, welches Referenzfoto den Treffer getragen hat.

        Beantwortet die Frage, die sonst Handarbeit ist: Zieht EIN Foto die fremde
        Person an, und was waere ohne dieses Foto passiert?
        """
        h = getattr(processor, "history", None)
        if h is None:
            raise HTTPException(400, "history is disabled (history_keep: 0)")
        try:
            res = h.blame(hid, gallery)
        except KeyError:
            raise HTTPException(404, "unknown history entry")
        log.info("history %s marked wrong: %s carried by %s", hid,
                 res.get("person"), res.get("top_photo") or "?")
        return res

    @app.delete("/api/history/{hid}")
    def history_delete(hid: str):
        h = getattr(processor, "history", None)
        if h is not None:
            h.delete(hid)
        return {"ok": True}

    @app.post("/api/history/clear")
    def history_clear():
        h = getattr(processor, "history", None)
        return {"ok": True, "removed": h.clear() if h is not None else 0}

    class AsideBody(BaseModel):
        reason: str = "reported as the cause of a wrong recognition"

    @app.post("/api/persons/{slug}/faces/{fname}/set-aside")
    def set_aside_face(slug: str, fname: str, body: AsideBody):
        """Das schuldige Referenzfoto beiseitelegen — reversibel, mit Grund."""
        if not gallery.set_aside(slug, fname, body.reason):
            raise HTTPException(404, "unknown person or photo")
        log.info("%s/%s set aside — %s", slug, fname, body.reason)
        return {"ok": True}

    @app.post("/api/persons/{slug}/rename")
    def rename_person(slug: str, body: dict):
        """Nur der Anzeigename — Fotos, Embeddings und Zuordnungen bleiben unberuehrt."""
        try:
            name = gallery.rename(slug, str(body.get("name", "")))
        except KeyError:
            raise HTTPException(404, "Unknown person")
        except ValueError as e:
            raise HTTPException(400, str(e))
        log.info("person %s renamed to %r", slug, name)
        return {"ok": True, "slug": slug, "name": name}

    @app.post("/api/persons/{slug}/favorite")
    def set_favorite(slug: str, body: FavBody):
        return {"ok": gallery.set_favorite(slug, body.favorite)}

    @app.post("/api/persons/{slug}/trimmed/{fname}/restore")
    def restore_trimmed(slug: str, fname: str):
        return {"ok": gallery.restore_trimmed(slug, fname)}

    @app.delete("/api/persons/{slug}/trimmed/{fname}")
    def delete_trimmed(slug: str, fname: str):
        gallery.delete_trimmed(slug, fname)
        return {"ok": True}

    @app.post("/api/persons/{slug}/trimmed/clear")
    def clear_trimmed(slug: str):
        return {"cleared": gallery.clear_trimmed(slug)}

    @app.post("/api/deduplicate")
    def deduplicate(body: dict = None):
        b = body or {}
        thr = float(b.get("threshold", cfg["faceid"].get("dedupe_threshold", 0.65)))
        dry = bool(b.get("dry_run", False))
        # zuerst echte Bild-Dubletten (identisches Foto), dann aehnliche Gesichter
        pix = gallery.deduplicate_pixels_all(dry_run=dry)
        emb = gallery.deduplicate_all(thr, dry_run=dry)
        key = "would_remove" if dry else "moved"
        return {key: pix + emb, "same_image": pix, "similar_face": emb, "threshold": thr}

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

    @app.post("/api/persons/{slug}/ignore")
    def ignore_person(slug: str):
        """Person komplett auf die Ignore-Liste setzen (alle Bilder werden Negativ-Anker)."""
        n = gallery.ignore_person(slug)
        if n:
            gallery.refresh_guesses()
        return {"ignored_faces": n}

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
            face, img = find_face_padded(engine, img, min_px=60)
            if face is None:
                skipped.append(f"{uf.filename}: no face found")
                continue
            # Mehrere Personen im Bild? find_face_padded liefert das GROESSTE Gesicht —
            # auf einem Familienfoto also womoeglich das der falschen Person, und die
            # landet dann still in dieser Galerie. Genau der Fall, der zwei Menschen
            # anschliessend verwechselbar macht.
            others = [f for f in engine.faces(img)
                      if (f.bbox[2] - f.bbox[0]) >= 60
                      and not np.array_equal(f.bbox, face.bbox)]
            if others:
                ref = gallery.embeddings(slug)
                if ref is None or not len(ref):
                    skipped.append(f"{uf.filename}: {len(others) + 1} faces in the photo and "
                                   f"no reference for this person yet — crop it to one face first")
                    continue
                # Das Gesicht nehmen, das am besten zu den vorhandenen Fotos passt,
                # nicht das groesste.
                cands = [face] + others
                sims = [float(np.max(ref @ f.normed_embedding)) for f in cands]
                best_i = int(np.argmax(sims))
                if sims[best_i] < 0.35:
                    skipped.append(f"{uf.filename}: {len(cands)} faces, none resembling this "
                                   f"person (best {sims[best_i]:.2f}) — crop it first")
                    continue
                face = cands[best_i]
            gallery.add_face(slug, crop_face(img, face.bbox), face.normed_embedding,
                             source={"camera": "upload"})
            added += 1
        return {"added": added, "skipped": skipped}

    @app.get("/api/unknowns")
    def unknowns():
        clusters = gallery.unknown_clusters(eps=float(cfg["faceid"].get("cluster_eps", 0.45)))
        frigate_url = _frigate_url(cfg)
        for c in clusters:
            for u in c:
                if u.pop("has_full", False):
                    u["full_url"] = f"data/unknowns/{u['id']}_full.jpg"
                elif frigate_url and u.get("event_id"):
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
                if getattr(processor.frigate, "enabled", True) and meta.get("event_id"):
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
                if getattr(processor.frigate, "enabled", True) and it.get("event_id"):
                    processor.frigate.set_sub_label(it["event_id"], name, score)
        gallery.refresh_guesses()
        return {"assigned": assigned, "total": sum(assigned.values())}

    @app.post("/api/unknowns/ignore")
    def ignore(body: AssignBody):
        """Gesichter auf die Ignore-Liste: nie mehr melden, zuordnen oder vorlegen.
        Alle Gesichter einer Aktion landen in derselben Gruppe."""
        import time as _t
        gid = f"g{int(_t.time() * 1000)}"
        n = sum(1 for uid in body.ids if gallery.ignore_unknown(uid, group=gid))
        return {"ignored": n}

    class MoveBody(BaseModel):
        ids: list[str]
        group: str

    @app.post("/api/ignored/move")
    def move_ignored(body: MoveBody):
        """Anker in eine andere Gruppe verschieben (auch: Gruppen zusammenlegen)."""
        return {"moved": gallery.set_ignored_group(body.ids, body.group)}

    @app.post("/api/ignored/assign")
    def assign_ignored(body: AssignBody):
        """Falsch ignorierte Gesichter direkt einer echten Person zuordnen."""
        persons_now = gallery.persons()
        slug = body.person if body.person in persons_now else gallery.create_person(body.person)
        n = gallery.assign_ignored(body.ids, slug)
        if n:
            gallery.refresh_guesses()
        return {"assigned": n, "slug": slug}

    @app.get("/api/ignored")
    def list_ignored():
        return JSONResponse(gallery.ignored_clusters(eps=float(cfg["faceid"].get("cluster_eps", 0.45))))

    @app.post("/api/ignored/restore")
    def restore_ignored(body: AssignBody):
        n = sum(1 for iid in body.ids if gallery.restore_ignored(iid))
        gallery.refresh_guesses()
        return {"restored": n}

    @app.post("/api/ignored/delete")
    def delete_ignored(body: AssignBody):
        for iid in body.ids:
            gallery.delete_ignored(iid)
        return {"ok": True}

    @app.post("/api/unknowns/discard")
    def discard(body: AssignBody):
        for uid in body.ids:
            gallery.discard_unknown(uid)
        return {"ok": True}

    # Pruefen und Setzen von "running" muss zusammen passieren. FastAPI fuehrt
    # synchrone Endpunkte in einem Threadpool aus, und zwischen der Abfrage und dem
    # Setzen liegt eine Zuweisung — dort kann Python den Thread wechseln. Zwei
    # gleichzeitige Starts kaemen sonst beide durch die 409-Sperre und wuerden in
    # denselben Zustand schreiben. Die Sperre haelt nur den Check-and-Set, nie den Lauf.
    job_lock = threading.Lock()

    folder_input = getattr(processor, "folder_ingest", None)
    folder_mode = bool(folder_input and folder_input.enabled)
    # Welcher Zweig laeuft, darf nicht allein an folder_mode haengen: wer Frigate
    # abschaltet, bevor der Ordner eingerichtet ist, landet sonst im Frigate-Zweig ohne
    # Frigate — und bekommt einen KeyError statt einer Auskunft.
    frigate_usable = bool(getattr(processor.frigate, "enabled", True) and _frigate_url(cfg))
    backfill_state = {"running": False, "processed": 0, "total": 0, "result": None,
                      "days": 0, "mode": "folder" if folder_mode else "frigate"}

    class BackfillBody(BaseModel):
        days: int = 14

    @app.post("/api/backfill")
    def start_backfill(body: BackfillBody):
        days = max(1, min(int(body.days), 60))
        if not folder_mode and not frigate_usable:
            raise HTTPException(400, "No input configured — enable Frigate with a URL, "
                                     "or switch on the recording folder")
        with job_lock:
            if backfill_state["running"]:
                raise HTTPException(409, "History scan already running")
            # ``days`` gilt nur fuer den Frigate-Zweig; der Ordner kennt kein Zeitfenster,
            # sondern nur „schon verarbeitet oder nicht". 0 statt der angefragten Zahl,
            # damit der Zustand nicht einen Zeitraum behauptet, nach dem niemand gesucht hat.
            backfill_state.update(running=True, processed=0, total=0, result=None,
                                  days=0 if folder_mode else days)

        def progress(i, total):
            backfill_state.update(processed=i, total=total)

        def worker():
            try:
                if folder_mode:
                    # Derselbe progress-Rueckruf wie im Frigate-Zweig: der Ordnerlauf
                    # meldet jetzt waehrenddessen, nicht erst danach.
                    stats = folder_input.scan_once(progress=progress)
                    backfill_state.update(processed=stats.get("processed", 0),
                                          total=stats.get("found", 0))
                else:
                    from .backfill import run_backfill
                    stats = run_backfill(
                        engine, gallery, processor.frigate, _frigate_url(cfg), days=days,
                        tag=bool(cfg["faceid"].get("set_sub_label", True)),
                        match_thr=float(cfg["faceid"].get("match_threshold", 0.5)),
                        progress=progress,
                        hires=bool(cfg["faceid"].get("hires_enroll", True)))
                backfill_state["result"] = stats
            except Exception as e:
                log.exception("history scan failed")
                backfill_state["result"] = {"error": str(e)}
            finally:
                backfill_state["running"] = False

        threading.Thread(target=worker, daemon=True, name="faceid-backfill").start()
        return {"started": True, "days": days}

    @app.get("/api/backfill")
    def backfill_status():
        return backfill_state

    # Live-editierbare Einstellungen (Settings-Tab). Overlay in data/settings.json.
    SETTINGS_SPEC = {
        "match_threshold": (0.2, 0.9),
        "unknown_threshold": (0.1, 0.8),
        "suggest_threshold": (0.1, 0.9),
        "cluster_eps": (0.3, 0.8),
        "ignore_threshold": (0.1, 0.9),
        "dedupe_threshold": (0.50, 0.95),
    }
    BACKUP_SPEC = {"hires_enroll": bool, "clip_fallback": bool, "clip_fallback_cameras": list, "live_hires_fallback": bool, "live_hires_fallback_cameras": list, "live_hires_mode": str, "backup_enabled": bool, "backup_hour": (0, 23), "backup_keep": (1, 90), "backup_dir": str}
    INT_SPEC = {"max_faces_per_person": (5, 100), "trimmed_keep": (0, 100),
                "match_top_k": (1, 10), "max_ignore_anchors": (0, 200),
                "min_face_px": (16, 200), "max_attempts": (1, 20),
                "folder_max_indexed_files": (0, 200_000)}
    settings_file = data_dir / "settings.json"

    def _apply_settings(updates: dict):
        deferred: list[str] = []
        f = cfg["faceid"]
        f.update(updates)
        # in processor/gallery gecachte Werte live nachziehen
        if "match_threshold" in updates: processor.match_thr = float(updates["match_threshold"])
        if "unknown_threshold" in updates: processor.unknown_thr = float(updates["unknown_threshold"])
        if "ignore_threshold" in updates: processor.ignore_thr = float(updates["ignore_threshold"])
        trimmed = 0
        if "max_faces_per_person" in updates:
            gallery.max_per_person = int(updates["max_faces_per_person"])
            trimmed = gallery.enforce_cap_all()
        if "trimmed_keep" in updates:
            gallery.trimmed_keep = int(updates["trimmed_keep"])
        if "match_top_k" in updates:
            gallery.top_k = max(1, int(updates["match_top_k"]))
        if "max_ignore_anchors" in updates:
            gallery.max_ignore_anchors = int(updates["max_ignore_anchors"])
        if "min_face_px" in updates:
            processor.min_face_px = int(updates["min_face_px"])
            if folder_input is not None:
                folder_input.min_face_px = int(updates["min_face_px"])
        if "max_attempts" in updates:
            processor.max_attempts = int(updates["max_attempts"])
        if "folder_max_indexed_files" in updates and folder_input is not None:
            # Ueber die gesperrte Methode statt an den privaten Feldern: dies laeuft im
            # HTTP-Thread, waehrend der Poller denselben Zustand schreiben kann.
            if not folder_input.set_index_cap(int(updates["folder_max_indexed_files"])):
                # Nicht verschlucken: bei einem selten laufenden Ordner kann es lange
                # dauern, bis der naechste Lauf die Grenze nachtraegt.
                deferred.append("index cap applies when the running folder scan finishes")
        if "dedupe_threshold" in updates:
            gallery.dedupe_threshold = float(updates["dedupe_threshold"])
        if "hires_enroll" in updates:
            processor.hires_enroll = bool(updates["hires_enroll"])
        if "clip_fallback" in updates:
            processor.clip_fallback = bool(updates["clip_fallback"])
        if "clip_fallback_cameras" in updates:
            processor.clip_fallback_cameras = set(updates["clip_fallback_cameras"])
        if "live_hires_fallback" in updates:
            processor.live_hires = bool(updates["live_hires_fallback"])
        if "live_hires_fallback_cameras" in updates:
            processor.live_hires_cameras = set(updates["live_hires_fallback_cameras"])
        if "live_hires_mode" in updates:
            m = str(updates["live_hires_mode"]).strip().lower()
            processor.live_hires_mode = m if m in ("fallback", "always") else "fallback"
        # settings.json (nur die editierbaren Keys) persistieren
        keys = set(SETTINGS_SPEC) | set(BACKUP_SPEC) | set(INT_SPEC)
        overlay = {}
        if settings_file.exists():
            try: overlay = json.loads(settings_file.read_text())
            except (json.JSONDecodeError, OSError): overlay = {}
        overlay.update({k: v for k, v in updates.items() if k in keys})
        settings_file.write_text(json.dumps(overlay, ensure_ascii=False, indent=1))
        return trimmed, deferred

    @app.get("/api/settings")
    def get_settings():
        f = cfg["faceid"]
        return {
            "input_mode": "folder" if folder_mode else "frigate",
            "folder": folder_input.status() if folder_mode else None,
            "thresholds": {k: float(f.get(k, {"match_threshold":0.5,"unknown_threshold":0.35,
                "suggest_threshold":0.40,"cluster_eps":0.55,"ignore_threshold":0.5,"dedupe_threshold":0.65}[k]))
                for k in SETTINGS_SPEC},
            "ranges": {k: v for k, v in SETTINGS_SPEC.items()},
            "backup": {"enabled": bool(f.get("backup_enabled", False)),
                       "hour": int(f.get("backup_hour", 3)),
                       "keep": int(f.get("backup_keep", 7)),
                       "dir": str(f.get("backup_dir") or "")},
            "max_faces_per_person": int(f.get("max_faces_per_person", 40)),
            "trimmed_keep": int(f.get("trimmed_keep", 10)),
            "match_top_k": int(f.get("match_top_k", 3)),
            "max_ignore_anchors": int(f.get("max_ignore_anchors", 0)),
            "min_face_px": int(f.get("min_face_px", 48)),
            "max_attempts": int(f.get("max_attempts", 6)),
            "folder_max_indexed_files": int(f.get("folder_max_indexed_files",
                                                  getattr(folder_input, "max_indexed_files", 5000))),
            "hires_enroll": bool(f.get("hires_enroll", True)),
            "clip_fallback": bool(f.get("clip_fallback", True)),
            "clip_fallback_cameras": list(f.get("clip_fallback_cameras") or []),
            "live_hires_fallback": bool(f.get("live_hires_fallback", False)),
            "live_hires_fallback_cameras": list(f.get("live_hires_fallback_cameras") or []),
            "live_hires_mode": str(f.get("live_hires_mode", "fallback")),
            "known_cameras": sorted(processor._announced or set(f.get("cameras") or [])),
        }

    @app.post("/api/settings")
    def post_settings(body: dict):
        updates = {}
        for k, (lo, hi) in SETTINGS_SPEC.items():
            if k in body:
                try: v = float(body[k])
                except (TypeError, ValueError): raise HTTPException(400, f"{k} not a number")
                updates[k] = min(max(v, lo), hi)
        for k, spec in BACKUP_SPEC.items():
            if k in body:
                if spec is bool: updates[k] = bool(body[k])
                elif spec is str: updates[k] = str(body[k] or "")
                elif spec is list:
                    # leere Liste = alle Kameras; Reihenfolge egal, Duplikate raus
                    updates[k] = sorted({str(c).strip() for c in (body[k] or []) if str(c).strip()})
                else:
                    lo, hi = spec
                    updates[k] = min(max(int(body[k]), lo), hi)
        for k, (lo, hi) in INT_SPEC.items():
            if k in body:
                try: updates[k] = min(max(int(body[k]), lo), hi)
                except (TypeError, ValueError): raise HTTPException(400, f"{k} not an int")
        trimmed, deferred = _apply_settings(updates)
        return {"ok": True, "applied": updates, "trimmed": trimmed, "deferred": deferred}

    @app.post("/api/backup/now")
    def backup_now():
        f = cfg["faceid"]
        bdir = _P(f.get("backup_dir") or (data_dir / "backups"))
        p = write_backup_file(data_dir, bdir)
        prune_backups(bdir, int(f.get("backup_keep", 7)))
        return {"ok": True, "file": str(p)}

    @app.get("/api/backup")
    def backup():
        """Komplette Galerie (persons + ignored) als tar.gz — die einzige unersetzliche
        Datenquelle. Unknown-Queue und Frigate-Vollbilder werden bewusst ausgelassen."""
        ts = time.strftime("%Y%m%d-%H%M%S")
        # octet-stream statt gzip: Safari (iOS) ignoriert Content-Disposition bei
        # application/gzip und zeigt den Inhalt als Zeichensalat im Browser an.
        return Response(build_backup_gz(data_dir), media_type="application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="faceid-backup-{ts}.tar.gz"'})

    analysis_state = {"running": False, "processed": 0, "total": 0, "result": None}

    class AnalysisBody(BaseModel):
        days: float = 3.0

    @app.post("/api/analysis")
    def start_analysis(body: AnalysisBody):
        """Kalibrierungs-Auswertung anstossen — dieselbe Rechnung wie die Skripte,
        aber ohne Shell, damit App-Nutzer sie ueberhaupt ausfuehren koennen."""
        days = max(0.0, min(float(body.days), 30.0))
        with job_lock:
            if analysis_state["running"]:
                raise HTTPException(409, "analysis already running")
            analysis_state.update(running=True, processed=0, total=0, result=None)

        def worker():
            try:
                from . import analysis
                analysis_state["result"] = analysis.run(
                    data_dir, engine, processor.frigate, cfg, days=days,
                    progress=lambda i, t: analysis_state.update(processed=i, total=t))
            except Exception as e:
                log.exception("analysis failed")
                analysis_state["result"] = {"error": str(e)}
            finally:
                analysis_state["running"] = False

        threading.Thread(target=worker, daemon=True, name="faceid-analysis").start()
        return {"started": True, "days": days}

    @app.get("/api/analysis")
    def analysis_status():
        return dict(analysis_state)

    # ---- Werkzeuge -------------------------------------------------------------
    # Dieselben Auswertungen wie in scripts/, nur ohne Shell. Noetig, weil scripts/
    # gar nicht im App-Image liegt (das Dockerfile kopiert app/ und static/) und die
    # Verzoegerungsmessung dort journalctl braeuchte, das es im Container nicht gibt.
    TOOL_SPECS = [
        {"id": "delay", "label": "How fast is a recognition?",
         "about": "Time from the start of the event to the published name, split by "
                  "attempt — attempt 1 is really Frigate's time to a usable snapshot. "
                  "Reads the history, so it needs no camera access and is instant.",
         "days": [0, 1, 3, 7, 14], "days_label": "period", "needs": "history"},
        {"id": "coverage", "label": "How well is each person covered?",
         "about": "Angles, cameras, day and night per person — and what photo is "
                  "concretely missing. Re-reads every reference photo, so it takes a "
                  "while on a large gallery.",
         "days": [], "needs": "gallery"},
        {"id": "why-no-face", "label": "Why do events yield no face?",
         "about": "Separates the three reasons that otherwise blur into 'not "
                  "recognised': no face in the picture at all, one too small, or one "
                  "the detector is unsure about. Downloads a snapshot per event.",
         "days": [1, 3, 7], "days_label": "look back", "needs": "frigate"},
    ]
    tool_state = {t["id"]: {"running": False, "processed": 0, "total": 0,
                            "result": None} for t in TOOL_SPECS}

    class ToolBody(BaseModel):
        days: float = 3.0

    @app.get("/api/tools")
    def tools_list():
        return {"tools": TOOL_SPECS, "state": tool_state}

    @app.post("/api/tools/{tid}")
    def start_tool(tid: str, body: ToolBody):
        st = tool_state.get(tid)
        if st is None:
            raise HTTPException(404, "unknown tool")
        days = max(0.0, min(float(body.days), 30.0))
        with job_lock:
            if st["running"]:
                raise HTTPException(409, "already running")
            st.update(running=True, processed=0, total=0, result=None)

        def worker():
            # Der Import gehoert INS try: schlaegt er fehl, stirbt der Thread sonst vor
            # dem finally, "running" bliebe fuer immer True und das Werkzeug waere bis
            # zum Neustart tot — mit "already running" als einziger Auskunft.
            try:
                from . import tools as t
                prog = lambda i, n: st.update(processed=i, total=n)
                if tid == "delay":
                    h = getattr(processor, "history", None)
                    st["result"] = ({"error": "history is disabled (history_keep: 0)"}
                                    if h is None else t.delay(h.dir, days=days))
                elif tid == "coverage":
                    st["result"] = t.coverage(data_dir, engine, processor.frigate, cfg,
                                              progress=prog)
                else:
                    st["result"] = t.why_no_face(engine, processor.frigate, cfg,
                                                 days=days, progress=prog)
            except Exception as e:
                log.exception("tool %s failed", tid)
                st["result"] = {"error": str(e)}
            finally:
                st["running"] = False

        threading.Thread(target=worker, daemon=True, name=f"faceid-tool-{tid}").start()
        return {"started": True, "days": days}

    @app.get("/api/tools/{tid}")
    def tool_status(tid: str):
        st = tool_state.get(tid)
        if st is None:
            raise HTTPException(404, "unknown tool")
        return dict(st)

    @app.get("/api/backups")
    def backups():
        """Gespeicherte Backups auflisten — wer FaceID als App betreibt, kommt sonst gar
        nicht an sie heran und sieht auch nicht, ob das Auto-Backup laeuft."""
        bdir = _P(cfg["faceid"].get("backup_dir") or (data_dir / "backups"))
        out = []
        if bdir.is_dir():
            for f in sorted(bdir.glob("faceid-backup-*.tar.gz"), reverse=True):
                try:
                    st = f.stat()
                except OSError:
                    continue
                out.append({"name": f.name, "size": st.st_size, "ts": st.st_mtime})
        return {"backups": out, "dir": str(bdir)}

    @app.get("/api/backups/{name}")
    def backup_file(name: str):
        """Ein bestimmtes gespeichertes Backup ausliefern."""
        bdir = _P(cfg["faceid"].get("backup_dir") or (data_dir / "backups"))
        # Kein Verzeichniswechsel ueber den Namen — nur Dateien aus genau diesem Ordner.
        target = (bdir / Path(name).name)
        if not target.is_file() or not target.name.startswith("faceid-backup-"):
            raise HTTPException(404, "no such backup")
        return FileResponse(target, media_type="application/octet-stream",
                            filename=target.name)

    @app.post("/api/restore")
    async def restore(file: UploadFile, merge: bool = False):
        """Backup einspielen. merge=false (Default) ersetzt persons+ignored komplett;
        merge=true fügt nur fehlende Personen/Anker hinzu (bestehende bleiben)."""
        raw = await file.read()
        try:
            tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        except tarfile.TarError:
            raise HTTPException(400, "Not a valid .tar.gz backup")
        members = tar.getmembers()
        # Sicherheit: keine absoluten Pfade / path traversal, nur persons/ und ignored/
        for m in members:
            norm = Path(m.name)
            if m.name.startswith("/") or ".." in norm.parts or (norm.parts and norm.parts[0] not in ("persons", "ignored")):
                raise HTTPException(400, f"Refusing unsafe path in archive: {m.name}")
        if not merge:
            for sub in ("persons", "ignored"):
                d = data_dir / sub
                if d.exists():
                    for f in d.rglob("*"):
                        if f.is_file():
                            f.unlink()
                    for f in sorted(d.rglob("*"), reverse=True):
                        if f.is_dir():
                            f.rmdir()
        added = 0
        for m in members:
            if not m.isfile():
                continue
            target = data_dir / m.name
            if merge and target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src:
                target.write_bytes(src.read())
            added += 1
        tar.close()
        gallery.reload()
        return {"restored_files": added, "mode": "merge" if merge else "replace",
                "persons": len(gallery.persons())}

    @app.get("/api/logs")
    def logs(limit: int = 300, level: str | None = None):
        """Die letzten Logzeilen — im Container und standalone sonst nur per Terminal
        einsehbar, genau wenn man wissen will warum nichts erkannt wird."""
        buf = logbuffer.buffer()
        if buf is None:
            return {"lines": [], "note": "Log-Puffer nicht aktiv"}
        return {"lines": buf.tail(max(1, min(limit, 500)), level)}

    @app.get("/api/health")
    def health():
        # "queue" ist die Review-Queue — das ist es, was der Header zeigt. Die interne
        # Verarbeitungs-Warteschlange steht separat unter "processing".
        folder = getattr(processor, "folder_ingest", None)
        return {"status": "ok", "persons": len(gallery.persons()),
                "queue": len(list((data_dir / "unknowns").glob("*.json"))),
                "processing": processor.queue.qsize(),
                "open_events": len(processor.events),
                "source": "folder" if folder and folder.enabled else "frigate",
                "folder": folder.status() if folder and folder.enabled else None,
                "suggest_threshold": float(cfg["faceid"].get("suggest_threshold", 0.40))}

    return app
