"""FaceID — Gesichtserkennung für Frigate/HA. Start: python -m app.main"""
import logging
from pathlib import Path

import uvicorn
import yaml

from .engine import FaceEngine
from .frigate_api import FrigateAPI
from .gallery import Gallery
from .mqtt_listener import EventProcessor
from .webui import build_app

BASE = Path(__file__).resolve().parent.parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("faceid")


def main():
    cfg = yaml.safe_load((BASE / "config.yaml").read_text())
    data_dir = BASE / "data"
    log.info("Lade InsightFace (buffalo_l) …")
    engine = FaceEngine(det_size=int(cfg["faceid"].get("det_size", 640)))
    gallery = Gallery(data_dir,
                      top_k=int(cfg["faceid"].get("match_top_k", 3)),
                      max_per_person=int(cfg["faceid"].get("max_faces_per_person", 40)))
    frigate = FrigateAPI(cfg["frigate"]["url"])
    processor = EventProcessor(cfg, engine, gallery, frigate)
    processor.start()
    app = build_app(cfg, engine, gallery, processor, data_dir, BASE / "static")
    uvicorn.run(app, host="0.0.0.0", port=int(cfg["faceid"].get("port", 8600)), log_level="warning")


if __name__ == "__main__":
    main()
