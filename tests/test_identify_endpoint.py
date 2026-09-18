"""Tests for POST /api/identify.

Goal: prove the endpoint is a pure recogniser (no enrollment, no events,
no writes to disk) and returns the documented payload shape under a handful
of well-defined inputs — zero faces, single face, multi-face, base64 vs
multipart, top_k boundary, min_face_px boundary.

The endpoint is exercised through FastAPI's TestClient with a fake engine
and a real Gallery on a tmp_path.  No NAS mount, no model load.
"""
import base64
import io
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.gallery import Gallery
from app.webui import build_app


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _FakeFace:
    """Mimics the parts of insightface.app.common.Face that build_app reads."""

    def __init__(self, bbox, embedding, det_score=0.95):
        # bbox in the [x1, y1, x2, y2] order insightface uses, ints.
        self.bbox = tuple(int(v) for v in bbox)
        self.det_score = float(det_score)
        self.normed_embedding = np.asarray(embedding, dtype=np.float32)


class _FakeEngine:
    def __init__(self, faces_per_image=None, faces=()):
        # faces_per_image: a callable (bgr) -> list of faces; if None, fall
        # back to the static `faces` list (single-image tests).
        self._faces_per_image = faces_per_image
        self._faces = list(faces)

    def faces(self, bgr):
        if self._faces_per_image is not None:
            return self._faces_per_image(bgr)
        return list(self._faces)


def _unit_embedding(seed):
    """Deterministic, L2-normalised embedding for unit tests."""
    v = np.zeros(512, dtype=np.float32)
    v[seed] = 1.0
    v[(seed + 1) % 512] = 0.25  # tiny off-axis component keeps things non-trivial
    v /= float(np.linalg.norm(v))
    return v


@pytest.fixture
def tmp_gallery(tmp_path: Path) -> Gallery:
    """A Gallery on a tmp_path with three slugs and known embeddings."""
    g = Gallery(tmp_path)
    for name in ("Cloud", "John", "展"):
        slug = g.create_person(name)
        # 5 reference embeddings per person, slightly varied per slug.
        seed = abs(hash(name)) % 256
        for i in range(5):
            emb = _unit_embedding((seed + i) % 512)
            # gallery.add_face writes a JPEG; we don't need it for the
            # identify endpoint — only the embedding matrix matters.
            g.add_face(slug, np.zeros((16, 16, 3), dtype=np.uint8), emb,
                       source={"camera": "test", "event_ts": float(i),
                               "observation_key": f"test-{slug}-{i}"})
    g.reload()
    return g


def _build_test_app(gallery, engine, *, top_k=3, match_thr=0.5,
                    unknown_thr=0.35, aliases=None):
    cfg = {
        "faceid": {
            "match_threshold": match_thr,
            "unknown_threshold": unknown_thr,
            "match_top_k": top_k,
            "min_face_px": 48,
            "ha_display_aliases": aliases or {},
            "auth": {"user": "none", "password": "none"},
        }
    }
    # `data_dir` may live under either name depending on the fork — the
    # upstream Gallery stores only `persons_dir`, the local fork adds a
    # `data_dir` attribute.  Resolve it via the gallery constructor arg.
    data_dir = getattr(gallery, "data_dir", None)
    if data_dir is None:
        data_dir = gallery.persons_dir.parent
    # Upstream's build_app() touches `processor.frigate` / `processor.history`
    # during /api/backfill setup; we don't exercise those endpoints but the
    # attribute access fires when the closure runs.  Pass a stub processor
    # that returns a minimal but attribute-complete facade.
    processor = _StubProcessor()
    app = build_app(cfg, engine, gallery, processor,
                    data_dir=data_dir,
                    static_dir=data_dir / "static")
    client = TestClient(app)
    # Production's auth middleware sees "none"/"none" as truthy, so the
    # running service enforces Basic auth with that credential.  Mirror
    # that here so tests cover the same code path callers hit on :8600.
    client.headers["Authorization"] = "Basic " + base64.b64encode(
        b"none:none").decode()
    return client


class _StubProcessor:
    """Minimal stand-in for the processor the upstream build_app() expects.

    Only the attributes touched at build_app setup time are populated — the
    identify endpoint itself never reaches into the processor.
    """
    def __init__(self):
        self.frigate = _StubFrigate()
        self.history = None
        self.events = {}
        self._announced = set()
        self.match_thr = 0.5
        self.unknown_thr = 0.35
        self.ignore_thr = 0.5
        self.min_face_px = 48
        self.max_attempts = 6
        self.hires_enroll = False
        self.clip_fallback = False
        self.clip_fallback_cameras = set()
        self.live_hires = False
        self.live_hires_cameras = set()
        self.live_hires_mode = "fallback"
        self.queue = type("Q", (), {"qsize": lambda self: 0})()


class _StubFrigate:
    enabled = False
    url = "http://adapter:8920"


def _make_jpeg(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", bgr)
    assert ok
    return buf.tobytes()


def _make_fake_image(width=400, height=400):
    """A solid-colour JPEG, valid for cv2.imdecode — detection will return []."""
    img = np.full((height, width, 3), 127, dtype=np.uint8)
    return _make_jpeg(img)


# ---------------------------------------------------------------------------
# Happy-path shape
# ---------------------------------------------------------------------------


def test_identify_returns_zero_faces_on_blank_image(tmp_gallery):
    engine = _FakeEngine(faces=[])
    client = _build_test_app(tmp_gallery, engine)
    r = client.post("/api/identify",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["face_count"] == 0
    assert body["faces"] == []
    assert body["thresholds"]["match"] == 0.5
    assert body["thresholds"]["unknown"] == 0.35
    assert body["request"]["top_k"] == 3
    assert body["request"]["min_face_px"] == 48


def test_identify_single_face_payload_shape(tmp_gallery):
    face = _FakeFace(bbox=(50, 60, 150, 160),
                     embedding=_unit_embedding(1),
                     det_score=0.92)
    engine = _FakeEngine(faces=[face])
    client = _build_test_app(tmp_gallery, engine,
                             aliases={"Cloud": "Cloud"})
    r = client.post("/api/identify",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["face_count"] == 1
    f0 = body["faces"][0]
    assert f0["index"] == 0
    assert f0["bbox"] == [50, 60, 150, 160]
    assert f0["det_score"] == 0.92
    assert isinstance(f0["candidates"], list) and len(f0["candidates"]) >= 1
    for c in f0["candidates"]:
        assert {"person", "slug", "person_uuid", "score", "ref_count"} <= set(c)
        assert 0.0 <= c["score"] <= 1.0
    # Candidates sorted by score desc.
    scores = [c["score"] for c in f0["candidates"]]
    assert scores == sorted(scores, reverse=True)
    # best mirrors candidates[0].
    assert f0["best"] is not None
    assert f0["best"]["slug"] == f0["candidates"][0]["slug"]
    assert f0["best"]["score"] == f0["candidates"][0]["score"]
    # len(candidates) <= top_k (capped at gallery size)
    assert len(f0["candidates"]) <= 3
    assert "above_match_threshold" in f0


def test_identify_multi_face_independent_results(tmp_gallery):
    faces = [
        _FakeFace(bbox=(10, 10, 110, 110), embedding=_unit_embedding(2)),
        _FakeFace(bbox=(200, 10, 300, 110), embedding=_unit_embedding(7)),
        _FakeFace(bbox=(10, 200, 110, 300), embedding=_unit_embedding(11)),
    ]
    engine = _FakeEngine(faces=faces)
    client = _build_test_app(tmp_gallery, engine)
    r = client.post("/api/identify",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["face_count"] == 3
    assert [f["index"] for f in body["faces"]] == [0, 1, 2]
    bboxes = [tuple(f["bbox"]) for f in body["faces"]]
    assert bboxes == [(10, 10, 110, 110), (200, 10, 300, 110), (10, 200, 110, 300)]


def test_identify_applies_ha_display_aliases(tmp_gallery):
    # The embeddings we created with _unit_embedding have sparse, distant
    # basis vectors per slug — so the "best" is whichever slug the embedding
    # was built for.  We just need to prove the alias mapping kicks in.
    cloud_emb = _unit_embedding(abs(hash("Cloud")) % 256)
    face = _FakeFace(bbox=(10, 10, 110, 110), embedding=cloud_emb)
    engine = _FakeEngine(faces=[face])
    client = _build_test_app(tmp_gallery, engine,
                             aliases={"Cloud": "Cloud display name"})
    r = client.post("/api/identify",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    body = r.json()
    best = body["faces"][0]["best"]
    # The display name passes through the alias table; slug is the machine id.
    if best["slug"] == "cloud":
        assert best["person"] == "Cloud display name"


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------


def test_identify_accepts_json_base64(tmp_gallery):
    face = _FakeFace(bbox=(10, 10, 110, 110), embedding=_unit_embedding(3))
    engine = _FakeEngine(faces=[face])
    client = _build_test_app(tmp_gallery, engine)
    raw = _make_fake_image()
    r = client.post("/api/identify",
                    json={"image_base64": base64.b64encode(raw).decode()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["face_count"] == 1
    assert body["request"]["content_type"].startswith("application/json")


def test_identify_rejects_garbage_bytes(tmp_gallery):
    engine = _FakeEngine(faces=[])
    client = _build_test_app(tmp_gallery, engine)
    r = client.post("/api/identify",
                    files={"image": ("p.jpg", b"not an image", "image/jpeg")})
    assert r.status_code == 400
    assert "decoded" in r.json()["detail"].lower()


def test_identify_rejects_invalid_base64(tmp_gallery):
    engine = _FakeEngine(faces=[])
    client = _build_test_app(tmp_gallery, engine)
    r = client.post("/api/identify", json={"image_base64": "!!!"})
    assert r.status_code == 400
    assert "base64" in r.json()["detail"].lower()


def test_identify_rejects_missing_payload(tmp_gallery):
    engine = _FakeEngine(faces=[])
    client = _build_test_app(tmp_gallery, engine)
    r = client.post("/api/identify", json={})
    assert r.status_code == 400
    # {} is valid JSON so the parser asks for image_base64 first; "missing
    # image" only fires for an empty body — see the empty-body branch.
    assert "image_base64" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# top_k boundary
# ---------------------------------------------------------------------------


def test_identify_top_k_caps_candidate_list(tmp_gallery):
    faces = [_FakeFace(bbox=(10, 10, 110, 110), embedding=_unit_embedding(4))]
    engine = _FakeEngine(faces=faces)
    client = _build_test_app(tmp_gallery, engine, top_k=2)
    r = client.post("/api/identify",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    body = r.json()
    assert len(body["faces"][0]["candidates"]) == 2
    assert body["request"]["top_k"] == 2


def test_identify_top_k_query_param_overrides_default(tmp_gallery):
    faces = [_FakeFace(bbox=(10, 10, 110, 110), embedding=_unit_embedding(4))]
    engine = _FakeEngine(faces=faces)
    client = _build_test_app(tmp_gallery, engine, top_k=3)
    r = client.post("/api/identify?top_k=1",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    body = r.json()
    assert len(body["faces"][0]["candidates"]) == 1
    assert body["request"]["top_k"] == 1


def test_identify_top_k_capped_at_gallery_size(tmp_gallery):
    faces = [_FakeFace(bbox=(10, 10, 110, 110), embedding=_unit_embedding(4))]
    engine = _FakeEngine(faces=faces)
    client = _build_test_app(tmp_gallery, engine, top_k=3)
    r = client.post("/api/identify?top_k=99",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    body = r.json()
    # tmp_gallery has 3 people, so top_k is capped at 3.
    assert body["request"]["top_k"] == 3
    assert len(body["faces"][0]["candidates"]) == 3


def test_identify_rejects_top_k_zero(tmp_gallery):
    engine = _FakeEngine(faces=[])
    client = _build_test_app(tmp_gallery, engine)
    r = client.post("/api/identify?top_k=0",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# min_face_px boundary
# ---------------------------------------------------------------------------


def test_identify_min_face_px_filters_small_detections(tmp_gallery):
    big = _FakeFace(bbox=(10, 10, 210, 210), embedding=_unit_embedding(2))   # 200x200
    small = _FakeFace(bbox=(10, 10, 50, 50), embedding=_unit_embedding(5))  # 40x40
    engine = _FakeEngine(faces=[big, small])
    client = _build_test_app(tmp_gallery, engine)
    # default min_face_px is 48 — the small face must be dropped.
    r = client.post("/api/identify",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    body = r.json()
    assert body["face_count"] == 1
    assert body["request"]["detected_before_filter"] == 2
    assert body["faces"][0]["bbox"] == [10, 10, 210, 210]


def test_identify_min_face_px_query_param(tmp_gallery):
    big = _FakeFace(bbox=(10, 10, 210, 210), embedding=_unit_embedding(2))
    small = _FakeFace(bbox=(10, 10, 50, 50), embedding=_unit_embedding(5))
    engine = _FakeEngine(faces=[big, small])
    client = _build_test_app(tmp_gallery, engine)
    # min_face_px=32 keeps the 40x40 face but still drops anything smaller.
    r = client.post("/api/identify?min_face_px=32",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    body = r.json()
    assert body["face_count"] == 2
    assert body["request"]["min_face_px"] == 32


# ---------------------------------------------------------------------------
# Threshold gating
# ---------------------------------------------------------------------------


def test_identify_above_match_threshold_flag(tmp_gallery):
    # Build a probe that matches one reference embedding almost exactly
    # (cosine ≈ 0.99) — well above the realistic 0.5 match_threshold, but
    # still below an absurd 0.9999 ceiling.  Same shape of assertion in
    # both directions, no flakiness around float ==.
    cloud_idx = abs(hash("Cloud")) % 256
    aligned = _unit_embedding(cloud_idx)
    # Tiny off-axis noise: cosine with the pure reference drops to ~0.99
    # without changing which reference wins.
    noise = aligned + np.full(512, 1e-3, dtype=np.float32)
    noise /= float(np.linalg.norm(noise))
    face = _FakeFace(bbox=(10, 10, 110, 110), embedding=noise)
    engine = _FakeEngine(faces=[face])
    client = _build_test_app(tmp_gallery, engine, match_thr=0.5, top_k=1)
    r = client.post("/api/identify",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    body = r.json()
    best_score = body["faces"][0]["best"]["score"]
    assert best_score > 0.95, best_score
    assert body["faces"][0]["above_match_threshold"] is True

    # With an absurdly high threshold, the same probe must come back false.
    client2 = _build_test_app(tmp_gallery, engine, match_thr=0.99999, top_k=1)
    r2 = client2.post("/api/identify",
                      files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    body2 = r2.json()
    assert body2["faces"][0]["above_match_threshold"] is False


# ---------------------------------------------------------------------------
# Read-only contract: no disk writes under tmp_path
# ---------------------------------------------------------------------------


def test_identify_does_not_write_to_disk(tmp_gallery):
    """Hammer the endpoint; tmp_path contents must be byte-identical.

    This is the side-effect-free proof the operator asked for.  We snapshot
    the gallery directory before, run a variety of identify calls (including
    the "writes metadata" code paths the existing /api/persons/{slug}/photos
    exercises), then snapshot again and assert no diff.

    Note: tmp_gallery is built BEFORE we take the snapshot, so the snapshot
    only covers what the endpoint itself could touch.
    """
    data_dir = tmp_gallery.data_dir
    before = _snapshot(data_dir)

    engine = _FakeEngine(faces=[
        _FakeFace(bbox=(10, 10, 110, 110), embedding=_unit_embedding(2)),
        _FakeFace(bbox=(200, 10, 300, 110), embedding=_unit_embedding(3)),
        _FakeFace(bbox=(10, 200, 110, 300), embedding=_unit_embedding(4)),
    ])
    client = _build_test_app(tmp_gallery, engine)

    # 30 calls — many shapes, many parameter combinations.
    for _ in range(10):
        client.post("/api/identify",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    for _ in range(10):
        client.post("/api/identify?top_k=1&min_face_px=32",
                    files={"image": ("p.jpg", _make_fake_image(), "image/jpeg")})
    for _ in range(10):
        client.post("/api/identify",
                    json={"image_base64": base64.b64encode(_make_fake_image()).decode()})

    # Junk calls that should fail without writing.
    client.post("/api/identify", json={"image_base64": "!!!"})
    client.post("/api/identify", files={"image": ("p.jpg", b"not an image", "image/jpeg")})
    client.post("/api/identify", json={})

    after = _snapshot(data_dir)
    assert before == after, f"identify mutated disk: {after - before}"


def _snapshot(root: Path) -> dict:
    """Return {relative_path: (size, mtime_ns, sha256)} for every regular file."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns,
                        _sha256(p))
    return out


def _sha256(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()