"""Backup der Galerie (persons + ignored) als tar.gz — geteilt von API und Auto-Scheduler."""
import io
import logging
import tarfile
import threading
import time
from pathlib import Path
from .nas_io import atomic_write_bytes_pinned, unlink_pinned, mkdir_pinned

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


def _validated_backup_dir(data_dir: Path, backup_dir: Path, storage_guard=None) -> Path:
    root = data_dir.resolve()
    target = backup_dir.resolve()
    if target != root and root not in target.parents:
        raise RuntimeError(f"backup directory must be under NAS data root: {target}")
    if storage_guard is not None:
        storage_guard(data=root)
    mkdir_pinned(target, storage_guard)
    if storage_guard is not None:
        storage_guard(data=target)
    return target


def write_backup_file(data_dir: Path, backup_dir: Path, storage_guard=None) -> Path:
    backup_dir = _validated_backup_dir(data_dir, backup_dir, storage_guard)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = backup_dir / f"faceid-backup-{ts}.tar.gz"
    if storage_guard is not None:
        storage_guard(data=backup_dir)
    try:
        atomic_write_bytes_pinned(path, build_backup_gz(data_dir), storage_guard)
        if storage_guard is not None:
            storage_guard(data=backup_dir)
    finally:
        pass
    return path


def prune_backups(backup_dir: Path, keep: int = 7, storage_guard=None):
    if keep <= 0:
        return
    files = sorted(backup_dir.glob("faceid-backup-*.tar.gz"), reverse=True)
    for old in files[keep:]:
        unlink_pinned(old, storage_guard)


def start_auto_backup(cfg_faceid: dict, data_dir: Path, storage_guard=None):
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
                        p = write_backup_file(data_dir, backup_dir, storage_guard)
                        prune_backups(backup_dir, int(cfg_faceid.get("backup_keep", 7)), storage_guard)
                        last_day = day
                        log.info("auto backup written: %s", p)
            except Exception:
                log.exception("auto backup failed")
            time.sleep(300)  # alle 5 Min prüfen

    threading.Thread(target=loop, daemon=True, name="faceid-autobackup").start()
