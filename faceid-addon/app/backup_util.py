"""Backup der Galerie (persons + ignored) als tar.gz — geteilt von API und Auto-Scheduler."""
import io
import logging
import tarfile
import threading
import time
from pathlib import Path

log = logging.getLogger("faceid.backup")

# Nur die unersetzliche Handarbeit sichern — nicht die Unknown-Queue oder Frigate-Vollbilder.
BACKUP_SUBDIRS = ("persons", "ignored")


def build_backup_gz(data_dir: Path) -> bytes:
    """Aktuelle Galerie als gzip-tar-Bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for sub in BACKUP_SUBDIRS:
            d = data_dir / sub
            if d.exists():
                tar.add(d, arcname=sub)
    return buf.getvalue()


def write_backup_file(data_dir: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"faceid-backup-{ts}.tar.gz"
    path.write_bytes(build_backup_gz(data_dir))
    return path


def prune_backups(backup_dir: Path, keep: int):
    if keep <= 0:
        return
    files = sorted(backup_dir.glob("faceid-backup-*.tar.gz"), reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)


def start_auto_backup(cfg_faceid: dict, data_dir: Path):
    """Täglicher Backup-Thread, wenn faceid.backup_enabled gesetzt ist.
    Liest die Config bei jedem Tick neu (Settings-Tab wirkt live)."""
    def loop():
        last_day = None
        while True:
            try:
                if cfg_faceid.get("backup_enabled"):
                    hour = int(cfg_faceid.get("backup_hour", 3))
                    now = time.localtime()
                    day = time.strftime("%Y-%m-%d", now)
                    if now.tm_hour >= hour and day != last_day:
                        backup_dir = Path(cfg_faceid.get("backup_dir") or (data_dir / "backups"))
                        p = write_backup_file(data_dir, backup_dir)
                        prune_backups(backup_dir, int(cfg_faceid.get("backup_keep", 7)))
                        last_day = day
                        log.info("auto backup written: %s", p)
            except Exception:
                log.exception("auto backup failed")
            time.sleep(300)  # alle 5 Min prüfen

    threading.Thread(target=loop, daemon=True, name="faceid-autobackup").start()
