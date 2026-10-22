"""Tests for dependency-free history archive helpers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibo_smart_control"
    / "history.py"
)
SPEC = importlib.util.spec_from_file_location("orvibo_smart_control_history", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
history = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = history
SPEC.loader.exec_module(history)


class HistoryDirTests(unittest.TestCase):
    def test_dir_created_and_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-test-door-lock-id")
            self.assertTrue(d.is_dir())
            self.assertEqual(
                d, root / "orvibo_smart_control" / "w-test-door-lock-id"
            )


class SaveSnapshotTests(unittest.TestCase):
    def test_save_and_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-lock")
            p1 = history.save_snapshot(d, "ring", 1785672298, b"jpg-data")
            p2 = history.save_snapshot(d, "ring", 1785672298, b"other")
            self.assertEqual(p1, p2)
            self.assertEqual(p1.read_bytes(), b"jpg-data")

    def test_object_identity_preserves_distinct_captures_in_same_second(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = history.history_dir(Path(td), "lock")
            first = history.save_snapshot(directory, "ring", 100, b"one", "/uid/one.jpg")
            second = history.save_snapshot(directory, "ring", 100, b"two", "/uid/two.jpg")
            duplicate = history.save_snapshot(directory, "ring", 100, b"changed", "uid/one.jpg")
            self.assertNotEqual(first, second)
            self.assertEqual(first, duplicate)
            self.assertEqual(first.read_bytes(), b"one")
            self.assertEqual(second.read_bytes(), b"two")
            self.assertEqual(len(history.list_history(Path(td), "lock")), 2)
            self.assertFalse(list(directory.glob("*.part")))

    def test_timestamp_cannot_escape_archive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                history.snapshot_path(Path(td), "ring", "../../outside")


class ListHistoryTests(unittest.TestCase):
    def test_sorted_and_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-lock")
            (d / "ring_1785672298.jpg").write_bytes(b"a")
            (d / "picklock_1785652830.mp4").write_bytes(b"b")
            (d / "picklock_1785652830.h264").write_bytes(b"c")
            (d / "unlock_1785672400.jpg").write_bytes(b"d")
            entries = history.list_history(root, "w-lock", limit=10)
            # ring 图 / picklock mp4 / picklock h264 / unlock 图 = 4 条
            self.assertEqual(len(entries), 4)
            self.assertEqual(entries[0]["kind"], "unlock")
            self.assertEqual(entries[0]["time"], "1785672400")
            self.assertEqual(entries[0]["type"], "image")
            self.assertEqual(entries[1]["kind"], "ring")
            self.assertEqual(entries[2]["kind"], "picklock")
            self.assertIn("media-source://orvibo_smart_control/archive/", entries[0]["media_id"])

    def test_device_filter_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d1 = history.history_dir(root, "w-lock1")
            d2 = history.history_dir(root, "w-lock2")
            (d1 / "ring_1785672298.jpg").write_bytes(b"a")
            (d2 / "ring_1785672299.jpg").write_bytes(b"b")
            all_entries = history.list_history(root, limit=10)
            self.assertEqual(len(all_entries), 2)
            only_1 = history.list_history(root, "w-lock1", limit=10)
            self.assertEqual(len(only_1), 1)
            self.assertEqual(only_1[0]["device_id"], "w-lock1")

    def test_image_filter_applies_before_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            directory = history.history_dir(root, "lock")
            history.save_snapshot(directory, "ring", 100, b"image", "picture.jpg")
            (directory / "ring_200.mp4").write_bytes(b"video")
            (directory / "ring_201.h264").write_bytes(b"video")
            self.assertEqual(history.list_history(root, "lock", 1)[0]["type"], "video")
            images = history.list_history(root, "lock", 1, "image")
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0]["type"], "image")
            self.assertEqual(images[0]["time"], "100")

    def test_latest_snapshot_reads_legacy_and_identity_names_and_skips_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            directory = history.history_dir(root, "lock")
            history.save_snapshot(directory, "ring", 100, b"legacy")
            history.save_snapshot(directory, "ring", 200, b"current", "picture.jpg")
            (directory / "ring_300.jpg").write_bytes(b"")
            (directory / "ring_400.mp4").write_bytes(b"video")
            (directory / "ring_500.jpg.part").write_bytes(b"incomplete")
            self.assertEqual(history.latest_snapshot(root, "lock"), (200, b"current"))
            self.assertIsNone(history.latest_snapshot(root, "missing"))


class MediaSourceIdTests(unittest.TestCase):
    def test_media_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-lock")
            f = d / "ring_1785672298.jpg"
            self.assertEqual(
                history.media_source_id(root, f),
                "media-source://orvibo_smart_control/archive/w-lock/ring_1785672298.jpg",
            )


class OpeningHistoryTests(unittest.TestCase):
    def test_openings_are_persistent_sorted_and_independent_of_media(self):
        with tempfile.TemporaryDirectory() as td:
            directory = history.history_dir(td, 'lock')
            history.save_opening_event(directory, {
                'opening_id': 'outside', 'time': 100, 'open_direction': 'outside',
                'unlock_user_id': 0, 'unlock_user_name': '管理员', 'unlock_method': 'fingerprint',
                'file': '/private/path',
            })
            history.save_opening_event(directory, {
                'opening_id': 'inside', 'time': 200, 'open_direction': 'inside',
                'unlock_user_id': 7, 'unlock_user_name': '旧用户', 'unlock_method': 'password',
            })
            records = history.list_opening_events(td, 'lock')
            self.assertEqual([record['id'] for record in records], ['inside', 'outside'])
            self.assertEqual(records[0], {'id': 'inside', 'time': 200, 'open_direction': 'inside'})
            self.assertEqual(records[1]['unlock_user_id'], '0')
            self.assertEqual(records[1]['unlock_user_name'], '管理员')
            self.assertNotIn('file', records[1])
            self.assertEqual(history.list_history(td, 'lock'), [])
            self.assertEqual(history.list_opening_events(td, 'other'), [])
            self.assertFalse(list(directory.glob('*.part')))

    def test_matching_door_event_updates_one_record_and_respects_limit(self):
        with tempfile.TemporaryDirectory() as td:
            directory = history.history_dir(td, 'lock')
            for identity, timestamp in (('first', 100), ('second', 200), ('third', 300)):
                history.save_opening_event(directory, {
                    'opening_id': identity, 'time': timestamp, 'open_direction': 'outside',
                }, limit=2)
            history.save_opening_event(directory, {
                'opening_id': 'second', 'time': 200, 'open_direction': 'outside',
                'opened_by_user_id': 7, 'opened_by_name': '张三',
            }, limit=2)
            records = history.list_opening_events(td, 'lock')
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1]['unlock_user_name'], '张三')
            self.assertEqual(len(history.list_opening_events(td, 'lock', 1)), 1)

    def test_malformed_storage_cannot_break_save_or_expose_inside_identity(self):
        with tempfile.TemporaryDirectory() as td:
            directory = history.history_dir(td, 'lock')
            path = directory / 'opening_events.json'
            path.write_text(json.dumps([
                {'id': 'bad-time', 'time': 'broken'}, None,
                {'id': 'boolean', 'time': True}, {'id': 'negative', 'time': -1},
                {'id': 'inside', 'time': 100, 'open_direction': 'inside', 'unlock_user_name': '旧用户'},
            ]), encoding='utf-8')
            self.assertEqual(history.list_opening_events(td, 'lock'), [
                {'id': 'inside', 'time': 100, 'open_direction': 'inside'},
            ])
            history.save_opening_event(directory, {'opening_id': 'valid', 'time': 200})
            self.assertEqual(len(history.list_opening_events(td, 'lock')), 2)
            path.write_text('{broken', encoding='utf-8')
            self.assertEqual(history.list_opening_events(td, 'lock'), [])
            history.save_opening_event(directory, {'opening_id': 'valid', 'time': 200})
            self.assertEqual(len(history.list_opening_events(td, 'lock')), 1)

    def test_invalid_event_cannot_select_path_or_create_storage(self):
        with tempfile.TemporaryDirectory() as td:
            directory = history.history_dir(td, 'lock')
            for event in ({'opening_id': '../escape', 'time': 1},
                          {'opening_id': 'bad', 'time': -1},
                          {'opening_id': 'bad', 'time': float('inf')}):
                history.save_opening_event(directory, event)
            self.assertEqual(list(directory.iterdir()), [])

    def test_cleanup_prunes_openings_by_age_and_count(self):
        with tempfile.TemporaryDirectory() as td:
            directory = history.history_dir(td, 'lock')
            now = int(time.time())
            for identity, timestamp in (('old', now - 8 * 86400), ('one', now - 3), ('two', now - 2), ('three', now - 1)):
                history.save_opening_event(directory, {'opening_id': identity, 'time': timestamp})
            self.assertEqual(history.cleanup(td, keep_days=7, device_id='lock', max_entries=2), 2)
            self.assertEqual([record['id'] for record in history.list_opening_events(td, 'lock')], ['three', 'two'])
            self.assertEqual(history.cleanup(td, keep_days=0, device_id='lock'), 2)
            self.assertFalse(directory.exists())


class CleanupTests(unittest.TestCase):
    def test_snapshot_limit_is_per_device_and_independent_of_video_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for device in ("one", "two"):
                directory = history.history_dir(root, device)
                for timestamp in range(35):
                    history.save_snapshot(directory, "ring", timestamp, b"jpg", str(timestamp))
                (directory / "ring_100.mp4").write_bytes(b"video")
                self.assertEqual(history.trim_snapshots(directory, 30), 5)
                self.assertEqual(len(list(directory.glob("*.jpg"))), 30)
                self.assertTrue((directory / "ring_100.mp4").exists())

    def test_automatic_cleanup_protects_retained_old_captures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            directory = history.history_dir(root, "lock")
            for timestamp in range(35):
                history.save_snapshot(directory, "ring", timestamp, b"jpg", str(timestamp))
            (directory / "ring_100.mp4").write_bytes(b"video")
            removed = history.cleanup(root, keep_days=7, snapshot_limit=30)
            self.assertEqual(removed, 6)
            images = history.list_history(root, "lock", 100, "image")
            self.assertEqual(len(images), 30)
            self.assertEqual(images[-1]["time"], "5")

    def test_removes_expired_keeps_recent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-lock")
            now = int(time.time())
            old_ts = now - 10 * 86400
            new_ts = now - 1 * 86400
            old_pic = d / f"ring_{old_ts}.jpg"
            old_video = d / f"picklock_{old_ts}.mp4"
            new_pic = d / f"ring_{new_ts}.jpg"
            old_pic.write_bytes(b"a")
            old_video.write_bytes(b"b")
            new_pic.write_bytes(b"c")
            removed = history.cleanup(root, keep_days=7)
            self.assertEqual(removed, 2)
            self.assertFalse(old_pic.exists())
            self.assertFalse(old_video.exists())
            self.assertTrue(new_pic.exists())

    def test_max_entries_trim(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-lock")
            now = int(time.time())
            for i in range(5):
                (d / f"ring_{now - i * 100}.jpg").write_bytes(b"x")
            removed = history.cleanup(root, keep_days=365, max_entries=3)
            self.assertEqual(removed, 2)
            files = [f.name for f in d.iterdir()]
            self.assertEqual(len(files), 3)

    def test_empty_dir_removed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = history.history_dir(root, "w-lock")
            history.cleanup(root, keep_days=7)
            self.assertFalse(d.exists())


if __name__ == "__main__":
    unittest.main()
