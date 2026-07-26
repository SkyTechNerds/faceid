"""Die letzten Logzeilen im Speicher halten, damit die Web-UI sie zeigen kann.

Wer FaceID als Home-Assistant-App betreibt, hat den Log-Tab der App. Standalone und
im Container liegt das Log dagegen in journalctl bzw. `docker logs` — beides sieht
niemand, der gerade in der Oberfläche nach dem Grund sucht, warum nichts erkannt wird.
"""
import logging
import re
from collections import deque

# Rauschen der Inferenz-Bibliothek, das im UI-Log nur ablenkt.
_NOISE = re.compile(r"pthread_setaffinity_np|Applied providers|find model:|model ignore:|"
                    r"set det-size|FutureWarning|tform\.estimate")


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 500):
        super().__init__()
        self.records: deque = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord):
        try:
            msg = record.getMessage()
            if _NOISE.search(msg):
                return
            self.records.append({
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name.removeprefix("faceid."),
                "msg": msg if len(msg) <= 2000 else msg[:2000] + " …",
            })
        except Exception:      # ein kaputter Logeintrag darf nichts umbringen
            pass

    def tail(self, limit: int = 300, level: str | None = None):
        items = list(self.records)
        if level in ("WARNING", "ERROR"):
            wanted = {"WARNING", "ERROR", "CRITICAL"} if level == "WARNING" else {"ERROR", "CRITICAL"}
            items = [r for r in items if r["level"] in wanted]
        return items[-limit:]


_handler: RingBufferHandler | None = None


def install(capacity: int = 500) -> RingBufferHandler:
    """Am Root-Logger einhängen; mehrfacher Aufruf liefert denselben Puffer."""
    global _handler
    if _handler is None:
        _handler = RingBufferHandler(capacity)
        _handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(_handler)
    return _handler


def buffer() -> RingBufferHandler | None:
    return _handler
