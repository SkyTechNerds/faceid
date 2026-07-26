"""FaceID — Gesichtserkennung für Frigate/HA. Start: python -m app.main"""
import json
import logging
from pathlib import Path

import uvicorn
import yaml

from . import logbuffer
from .engine import FaceEngine
from .frigate_api import FrigateAPI
from .gallery import Gallery
from .mqtt_listener import EventProcessor
from .webui import build_app
from .backup_util import start_auto_backup

BASE = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logbuffer.install()   # damit die Weboberflaeche das Log zeigen kann
log = logging.getLogger("faceid")


def main():
    cfg = yaml.safe_load((BASE / "config.yaml").read_text())
    data_dir = BASE / "data"
    # Live-editierbare Einstellungen (Settings-Tab) liegen als Overlay in data/settings.json
    # und gewinnen über config.yaml — persistent auch beim Add-on (config.yaml wird dort
    # bei jedem Start neu generiert, /data überlebt).
    settings_f = data_dir / "settings.json"
    if settings_f.exists():
        try:
            cfg.setdefault("faceid", {}).update(json.loads(settings_f.read_text()))
        except (json.JSONDecodeError, OSError):
            log.warning("settings.json unreadable — ignoring it")
    log.info("loading InsightFace (buffalo_l) …")
    engine = FaceEngine(det_size=int(cfg["faceid"].get("det_size", 640)))
    gallery = Gallery(data_dir,
                      top_k=int(cfg["faceid"].get("match_top_k", 3)),
                      max_per_person=int(cfg["faceid"].get("max_faces_per_person", 40)))
    gallery.trimmed_keep = int(cfg["faceid"].get("trimmed_keep", 10))
    gallery.dedupe_threshold = float(cfg["faceid"].get("dedupe_threshold", 0.65))
    frigate = FrigateAPI(cfg["frigate"]["url"])
    processor = EventProcessor(cfg, engine, gallery, frigate)
    processor.start()
    start_auto_backup(cfg["faceid"], data_dir)
    app = build_app(cfg, engine, gallery, processor, data_dir, BASE / "static")
    uvicorn.run(app, host="0.0.0.0", port=int(cfg["faceid"].get("port", 8600)), log_level="warning")


if __name__ == "__main__":
    main()
