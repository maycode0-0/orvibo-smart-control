"""门锁事件历史归档：截图/视频统一落盘到 media 目录，供回溯浏览。

目录结构：
  config/media/orvibo_smart_control/<device_id>/<kind>_<时间戳>.(jpg|h264|mp4)
  config/media/orvibo_smart_control/<device_id>/opening_events.json

HA 媒体浏览器按目录/文件名自然形成时间倒序的历史列表；list_events
服务动态扫描目录返回结构化记录，供自动化与自定义卡片使用。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

_FILE_PATTERN = re.compile(r"^([A-Za-z0-9_]+)_(\d+)(?:-([0-9a-f]{16}))?\.(\w+)$")


def _device_name(device_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(device_id)) or "device"


def _entries(directory: Path) -> list[tuple[int, Path]]:
    """Parse both legacy names and names containing a COS object identity."""
    if not directory.is_dir():
        return []
    entries: list[tuple[int, int, Path]] = []
    for path in directory.iterdir():
        match = _FILE_PATTERN.fullmatch(path.name)
        if not match:
            continue
        try:
            if path.is_file():
                entries.append((int(match.group(2)), path.stat().st_mtime_ns, path))
        except FileNotFoundError:
            continue
    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2].name), reverse=True)
    return [(timestamp, path) for timestamp, _modified, path in entries]


def history_dir(media_root: Path, device_id: str) -> Path:
    """返回设备归档目录（自动创建）。"""
    safe = _device_name(device_id)
    d = Path(media_root) / "orvibo_smart_control" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _opening_history_path(directory: Path) -> Path:
    return Path(directory) / "opening_events.json"


def _write_opening_records(path: Path, records: list[dict]) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".part", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(records, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_opening_records(path: Path) -> list[dict]:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(records, list):
        return []
    valid = []
    for item in records:
        if (not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item["id"])
                or type(item.get("time")) is not int or item["time"] < 0):
            continue
        direction = item.get("open_direction")
        record = {
            "id": item["id"], "time": item["time"],
            "open_direction": direction if direction in ("inside", "outside") else "unknown",
        }
        if direction == "outside":
            for key in ("unlock_method", "unlock_user_id", "unlock_user_name"):
                if isinstance(item.get(key), (str, int)):
                    record[key] = str(item[key])[:64]
        valid.append(record)
    valid.sort(key=lambda item: item["time"], reverse=True)
    return valid


def save_opening_event(
    directory: Path, event: dict, limit: int = 500,
) -> None:
    """Atomically save or update one lock opening record by event identity."""
    opening_id = str(event.get("opening_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", opening_id):
        return
    try:
        timestamp = int(event.get("time"))
    except (TypeError, ValueError, OverflowError):
        return
    if timestamp < 0:
        return
    direction = event.get("open_direction")
    if direction not in ("inside", "outside"):
        direction = "unknown"
    record = {
        "id": opening_id,
        "time": timestamp,
        "open_direction": direction,
    }
    if direction == "outside":
        for key, source in (
            ("unlock_method", "unlock_method"),
            ("unlock_user_id", "opened_by_user_id"),
        ):
            value = event.get(source, event.get(key))
            if value is not None:
                record[key] = str(value)[:64]
        name = event.get("opened_by_name") or event.get("unlock_user_name")
        if name:
            record["unlock_user_name"] = str(name)[:64]

    path = _opening_history_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [item for item in _read_opening_records(path) if item["id"] != opening_id]
    records.append(record)
    records.sort(key=lambda item: int(item.get("time", 0)), reverse=True)
    records = records[:max(1, int(limit))]
    _write_opening_records(path, records)


def list_opening_events(
    media_root: Path, device_id: str, limit: int = 100,
) -> list[dict]:
    """Return recent opening records without exposing their local file path."""
    path = Path(media_root) / "orvibo_smart_control" / _device_name(device_id) / "opening_events.json"
    return _read_opening_records(path)[:max(1, int(limit))]


def snapshot_path(
    history_dir: Path, kind: str, ts: int | str, object_key: str | None = None,
) -> Path:
    """截图路径；可选对象身份后缀区分同秒事件，旧命名仍兼容。"""
    kind = re.sub(r"[^A-Za-z0-9_]", "_", str(kind)) or "event"
    timestamp = int(ts)
    if timestamp < 0:
        raise ValueError("invalid_snapshot_timestamp")
    identity = (
        "-" + hashlib.sha256(object_key.lstrip("/").encode()).hexdigest()[:16]
        if object_key else ""
    )
    return Path(history_dir) / f"{kind}_{timestamp}{identity}.jpg"


def save_snapshot(
    history_dir: Path,
    kind: str,
    ts: int | str,
    image_bytes: bytes,
    object_key: str | None = None,
) -> Path:
    """保存事件截图；同事件已存在则跳过，返回实际路径。"""
    dest = snapshot_path(history_dir, kind, ts, object_key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(image_bytes)
        os.replace(temporary, dest)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return dest


def trim_snapshots(directory: Path, limit: int = 30) -> int:
    """Keep the newest captures for one lock, independently of video retention."""
    pictures = [
        (ts, path) for ts, path in _entries(Path(directory))
        if path.suffix.lower() == ".jpg"
    ]
    removed = 0
    for _timestamp, path in pictures[max(1, int(limit)):]:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


def latest_snapshot(media_root: Path, device_id: str) -> tuple[int, bytes] | None:
    """Read the most recent complete local capture, including legacy files."""
    directory = Path(media_root) / "orvibo_smart_control" / _device_name(device_id)
    for timestamp, path in _entries(directory):
        if path.suffix.lower() != ".jpg":
            continue
        try:
            image = path.read_bytes()
        except OSError:
            continue
        if image:
            return timestamp, image
    return None


def media_source_id(media_root: Path, file_path: Path) -> str:
    """Reference our archive source, independently of HA's configured media dirs."""
    archive = Path(media_root) / "orvibo_smart_control"
    rel = Path(file_path).resolve().relative_to(archive.resolve())
    return f"media-source://orvibo_smart_control/archive/{rel.as_posix()}"


def list_history(
    media_root: Path,
    device_id: Optional[str] = None,
    limit: int = 100,
    media_type: str | None = None,
) -> list[dict[str, str]]:
    """扫描归档目录，按时间倒序返回历史记录。

    每条记录：{device_id, kind, time, type, file, media_id}。
    """
    root = Path(media_root) / "orvibo_smart_control"
    if not root.is_dir():
        return []
    entries: list[dict[str, str]] = []
    dirs = [root / _device_name(device_id)] if device_id else sorted(root.iterdir(), reverse=True)
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir(), reverse=True):
            if not f.is_file():
                continue
            m = _FILE_PATTERN.match(f.name)
            if not m:
                continue
            kind, ts, _identity, ext = m.groups()
            entry_type = (
                "video" if ext.lower() in ("mp4", "h264") else "image"
            )
            if media_type is not None and entry_type != media_type:
                continue
            entries.append(
                {
                    "device_id": d.name,
                    "kind": kind,
                    "time": ts,
                    "type": entry_type,
                    "file": str(f),
                    "media_id": media_source_id(media_root, f),
                }
            )
    entries.sort(key=lambda e: int(e["time"]), reverse=True)
    return entries[: max(1, int(limit))]


def cleanup(
    media_root: Path,
    keep_days: int = 7,
    device_id: Optional[str] = None,
    max_entries: Optional[int] = None,
    snapshot_limit: Optional[int] = None,
) -> int:
    """清理历史归档：删除超过 keep_days 天的文件，可选按条数裁剪。

    按文件名时间戳判断（<kind>_<ts>.<ext>）；空目录一并删除。
    返回删除的媒体文件数与开门记录数之和。
    """
    root = Path(media_root) / "orvibo_smart_control"
    if not root.is_dir():
        return 0
    cutoff = time.time() - max(0, int(keep_days)) * 86400
    dirs = [root / _device_name(device_id)] if device_id else list(root.iterdir())
    removed = 0
    for d in dirs:
        if not d.is_dir():
            continue
        opening_path = _opening_history_path(d)
        try:
            opening_records = json.loads(opening_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            opening_records = None
        if isinstance(opening_records, list):
            retained = [
                item for item in opening_records
                if isinstance(item, dict)
                and isinstance(item.get("time"), int)
                and item["time"] >= cutoff
            ]
            retained.sort(key=lambda item: item["time"], reverse=True)
            if max_entries:
                retained = retained[:max_entries]
            if len(retained) != len(opening_records):
                if retained:
                    _write_opening_records(opening_path, retained)
                else:
                    opening_path.unlink(missing_ok=True)
                removed += len(opening_records) - len(retained)
        if snapshot_limit is not None:
            removed += trim_snapshots(d, snapshot_limit)
        entries = _entries(d)
        protected = set()
        if snapshot_limit is not None:
            pictures = [path for _ts, path in entries if path.suffix.lower() == ".jpg"]
            protected = set(pictures[:max(1, int(snapshot_limit))])
        # 超过 keep_days 的删除
        for ts, f in entries:
            if ts < cutoff and f not in protected:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        # 按条数裁剪（保留最近 max_entries 条）
        entries = [(ts, f) for ts, f in entries if f.exists()]
        if max_entries and len(entries) > max_entries:
            for ts, f in entries[max_entries:]:
                if not f.exists():
                    continue
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        # 删除空目录
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return removed
