"""Tests for lock media orchestration boundaries."""

from __future__ import annotations

import importlib
import asyncio
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"


def _load_module():
    package_name = "orvibo_smart_control_lock_media_manager_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.lock_media_manager")


class FakeConfig:
    def __init__(self, root="test-media"):
        self.root = root

    def path(self, name):
        return str(Path(self.root))


class FakeHass:
    config = FakeConfig()

    def __init__(self, media_root=None):
        if media_root is not None:
            self.config = FakeConfig(media_root)
        self.scheduled = 0

    def async_create_task(self, coroutine):
        self.scheduled += 1
        coroutine.close()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeCos:
    def try_signed_url(self, device_id, uid, key):
        return f"https://example.invalid/{key}"

    def cached_credentials(self, device_id):
        return object()

    async def signed_url(self, device_id, uid, key):
        return f"https://example.invalid/{key}"


class LockMediaManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def test_attach_urls_uses_cached_signature_and_schedules_snapshot(self) -> None:
        hass = FakeHass()
        manager = self.module.LockMediaManager(
            hass, {"lock": {"uid": "uid"}}, b"salt"
        )
        manager.cos = FakeCos()

        result = manager.attach_urls(
            "lock", {}, {"kind": "ring", "pic_url": "picture.jpg", "time": 1}
        )

        self.assertEqual(
            result["pic_media_url"], "https://example.invalid/picture.jpg"
        )
        self.assertEqual(hass.scheduled, 1)

    def test_fetch_video_rejects_unknown_device_before_network(self) -> None:
        manager = self.module.LockMediaManager(FakeHass(), {}, b"salt")
        manager.cos = object()
        manager.archiver = object()

        result = __import__("asyncio").run(
            manager.fetch_video("missing", "video.h264")
        )

        self.assertEqual(result, {"error": "设备不存在或不是门锁"})

    def test_snapshot_is_saved_without_camera_entity(self) -> None:
        async def no_sleep(_delay):
            return None

        with tempfile.TemporaryDirectory() as td:
            hass = FakeHass(td)
            manager = self.module.LockMediaManager(
                hass, {"lock": {"uid": "uid"}}, b"salt"
            )
            manager.cos = FakeCos()
            original_download = self.module._download_bytes
            original_sleep = self.module.asyncio.sleep
            self.module._download_bytes = lambda url: b"jpg-data"
            self.module.asyncio.sleep = no_sleep
            try:
                asyncio.run(
                    manager._update_snapshot(
                        "lock", "uid", "picture.jpg", "ring", 1785672298
                    )
                )
            finally:
                self.module._download_bytes = original_download
                self.module.asyncio.sleep = original_sleep
            saved = list((Path(td) / "orvibo_smart_control" / "lock").glob("ring_1785672298*.jpg"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), b"jpg-data")

    def test_snapshot_limit_has_default_and_validated_bounds(self) -> None:
        for value, expected in ((30, 30), (3, 3), ("5", 5), (0, 1), (900, 500), (None, 30), ("bad", 30)):
            manager = self.module.LockMediaManager(FakeHass(), {}, b"salt", value)
            self.assertEqual(manager.snapshot_limit, expected)

    def test_event_urls_cannot_select_external_downloads(self) -> None:
        for key in ("https://example.com/picture.jpg", "//example.com/image", "../image", "folder/../image", "image?token=secret", "C:\\image.jpg", {"url": "image"}):
            hass = FakeHass()
            manager = self.module.LockMediaManager(hass, {}, b"salt")
            manager.cos = FakeCos()
            self.assertEqual(manager.attach_urls("lock", {}, {"pic_url": key}), {})
            self.assertEqual(hass.scheduled, 0)


class AsyncFakeHass(FakeHass):
    def async_create_task(self, coroutine):
        self.scheduled += 1
        return asyncio.create_task(coroutine)


class LockMediaManagerAsyncTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_module()

    def manager(self, directory, snapshot_limit=30):
        manager = self.module.LockMediaManager(AsyncFakeHass(directory), {}, b"salt", snapshot_limit)
        manager.cos = FakeCos()
        return manager

    async def test_openings_need_no_camera_or_cos_and_reads_wait_for_pending_save(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td)
            manager.cos = None
            event = {'opening_id': 'one', 'time': 100, 'open_direction': 'inside',
                     'unlock_user_name': '旧用户'}
            manager.record_opening('lock', event)
            event['time'] = 200
            records = await manager.list_openings('lock')
            self.assertEqual(records, [
                {'device_id': 'lock', 'id': 'one', 'time': 100, 'open_direction': 'inside'},
            ])
            restored = self.manager(td)
            self.assertEqual(await restored.list_openings('lock'), records)

    async def test_concurrent_openings_are_serialized_and_door_event_is_merged(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td)
            for index in range(20):
                manager.record_opening('lock', {'opening_id': str(index), 'time': index})
            manager.record_opening('lock', {'opening_id': '19', 'time': 19,
                                           'open_direction': 'outside', 'opened_by_name': '张三'})
            records = await manager.list_openings('lock')
            self.assertEqual(len(records), 20)
            self.assertEqual(records[0]['unlock_user_name'], '张三')
            self.assertEqual(await manager.list_openings('other'), [])

    async def test_shutdown_finishes_queued_openings_but_rejects_new_ones(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td)
            manager.record_opening('lock', {'opening_id': 'one', 'time': 100})
            await manager.async_shutdown()
            manager.record_opening('lock', {'opening_id': 'two', 'time': 200})
            self.assertEqual([item['id'] for item in await manager.list_openings('lock')], ['one'])
            self.assertFalse(manager._tasks)

    async def test_manual_cleanup_serializes_with_opening_save_and_only_owned_locks(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td)
            manager.devices = {'lock': {'device_type_raw': 522}}
            for device in ('lock', 'other'):
                await manager._save_opening(device, {'opening_id': 'old', 'time': 100})
            lock = manager._snapshot_locks['lock']
            await lock.acquire()
            pending = asyncio.create_task(manager.cleanup_history(keep_days=0))
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            lock.release()
            self.assertEqual(await pending, 1)
            self.assertEqual(await manager.list_openings('lock'), [])
            self.assertEqual(len(await manager.list_openings('other')), 1)

    async def test_download_is_immediate_and_retries_use_fresh_signature(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td)
            manager.cos.signed_url = AsyncMock(side_effect=["signed-first", TimeoutError(), "signed-second"])
            order = []

            def download(url):
                order.append(url)
                return None if url == "signed-first" else b"image"

            async def sleep(delay):
                order.append(delay)

            with patch.object(self.module, "_download_bytes", side_effect=download), patch.object(self.module.asyncio, "sleep", side_effect=sleep):
                await manager._update_snapshot("lock", "uid", "picture.jpg", "ring", 100)
            self.assertEqual(order, ["signed-first", 8, 10, "signed-second"])
            self.assertEqual(manager.cos.signed_url.await_count, 3)
            self.assertEqual(manager._snapshot_pending, set())

    async def test_capture_count_is_configurable_and_only_images_are_trimmed(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td, 3)
            with patch.object(self.module, "_download_bytes", return_value=b"image"):
                for timestamp in range(5):
                    await manager._update_snapshot("lock", "uid", f"picture-{timestamp}.jpg", "ring", timestamp)
                await manager._update_snapshot("other", "uid", "picture.jpg", "ring", 1)
            images = await manager.list_events("lock", 100, "image")
            self.assertEqual([image["time"] for image in images], ["4", "3", "2"])
            self.assertEqual(len(await manager.list_events("other", 100, "image")), 1)

    async def test_automatic_cleanup_only_touches_owned_locks_and_shares_save_lock(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td, 2)
            manager.devices = {
                "lock": {"device_type_raw": 522},
                "camera": {"device_type_raw": 14},
            }
            for device in ("lock", "other_entry_lock", "camera"):
                directory = self.module.history_dir(td, device)
                for timestamp in range(3):
                    self.module.save_snapshot(directory, "ring", timestamp, b"image")
            own_lock = manager._snapshot_locks.setdefault("lock", asyncio.Lock())
            await own_lock.acquire()
            event_module = types.ModuleType("homeassistant.helpers.event")
            event_module.async_track_time_interval = MagicMock(return_value=MagicMock())
            with patch.dict(sys.modules, {"homeassistant.helpers.event": event_module}):
                manager.start_cleanup()
                tasks = tuple(manager._tasks)
                await asyncio.sleep(0)
                self.assertEqual(len(await manager.list_events("lock")), 3)
                own_lock.release()
                await asyncio.gather(*tasks)
            self.assertEqual(len(await manager.list_events("lock")), 2)
            self.assertEqual(len(await manager.list_events("other_entry_lock")), 3)
            self.assertEqual(len(await manager.list_events("camera")), 3)
            await manager.async_shutdown()

    async def test_duplicate_is_suppressed_until_persistence_completes(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td)
            saving, release = asyncio.Event(), asyncio.Event()
            original_executor = manager.hass.async_add_executor_job

            async def executor(function, *args):
                if getattr(function, "__name__", "") == "persist":
                    saving.set()
                    await release.wait()
                return await original_executor(function, *args)

            manager.hass.async_add_executor_job = executor
            with patch.object(self.module, "_download_bytes", return_value=b"image") as download:
                first = asyncio.create_task(manager._update_snapshot("lock", "uid", "/picture.jpg", "ring", 100))
                await saving.wait()
                await manager._update_snapshot("lock", "uid", "picture.jpg", "ring", 100)
                self.assertEqual(download.call_count, 1)
                self.assertTrue(manager._snapshot_pending)
                release.set()
                await first
            self.assertEqual(manager._snapshot_pending, set())
            self.assertEqual(len(await manager.list_events("lock")), 1)

    async def test_late_old_image_is_archived_without_replacing_latest_camera(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td)
            camera = types.SimpleNamespace(async_set_image=AsyncMock())
            manager._cameras["lock"] = camera
            downloading, release = asyncio.Event(), asyncio.Event()

            async def executor(function, *args):
                if function is self.module._download_bytes:
                    if "old.jpg" in args[0]:
                        downloading.set()
                        await release.wait()
                        return b"old"
                    return b"new"
                return function(*args)

            manager.hass.async_add_executor_job = executor
            old = asyncio.create_task(manager._update_snapshot("lock", "uid", "old.jpg", "ring", 100))
            await downloading.wait()
            await manager._update_snapshot("lock", "uid", "new.jpg", "ring", 200)
            release.set()
            await old
            camera.async_set_image.assert_awaited_once_with(b"new")
            self.assertEqual(len(await manager.list_events("lock")), 2)

    async def test_camera_registration_restores_latest_local_image_without_network(self):
        with tempfile.TemporaryDirectory() as td:
            directory = self.module.history_dir(td, "lock")
            self.module.save_snapshot(directory, "ring", 100, b"legacy")
            self.module.save_snapshot(directory, "ring", 200, b"latest", "picture.jpg")
            manager = self.manager(td)
            manager.cos.signed_url = AsyncMock()
            camera = types.SimpleNamespace(async_set_image=AsyncMock())
            manager.register_camera("lock", camera)
            await asyncio.gather(*tuple(manager._tasks))
            camera.async_set_image.assert_awaited_once_with(b"latest")
            manager.cos.signed_url.assert_not_awaited()
            manager.unregister_camera("lock", camera)
            self.assertNotIn("lock", manager._cameras)

    async def test_reload_does_not_replace_newer_disk_image_with_replayed_event(self):
        with tempfile.TemporaryDirectory() as td:
            directory = self.module.history_dir(td, "lock")
            self.module.save_snapshot(directory, "ring", 200, b"latest")
            manager = self.manager(td)
            camera = types.SimpleNamespace(async_set_image=AsyncMock())
            manager._cameras["lock"] = camera
            with patch.object(self.module, "_download_bytes", return_value=b"old"):
                await manager._update_snapshot("lock", "uid", "old.jpg", "ring", 100)
            camera.async_set_image.assert_not_awaited()

    async def test_shutdown_cancels_delayed_retry_and_cleans_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td)
            manager.cos.signed_url = AsyncMock(return_value=None)
            sleeping = asyncio.Event()

            async def sleep(_delay):
                sleeping.set()
                await asyncio.Event().wait()

            with patch.object(self.module.asyncio, "sleep", side_effect=sleep):
                manager.attach_urls("lock", {}, {"pic_url": "picture.jpg", "time": 100})
                await sleeping.wait()
                await manager.async_shutdown()
            self.assertEqual(manager._snapshot_pending, set())
            self.assertFalse(manager._tasks)
            self.assertEqual(await manager.list_events("lock"), [])

    async def test_cancellation_waits_for_atomic_save_before_releasing_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self.manager(td)
            saving, release = asyncio.Event(), asyncio.Event()
            original_executor = manager.hass.async_add_executor_job

            async def executor(function, *args):
                if getattr(function, "__name__", "") == "persist":
                    saving.set()
                    await release.wait()
                return await original_executor(function, *args)

            manager.hass.async_add_executor_job = executor
            with patch.object(self.module, "_download_bytes", return_value=b"image"):
                task = asyncio.create_task(manager._update_snapshot("lock", "uid", "picture.jpg", "ring", 100))
                await saving.wait()
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertTrue(manager._snapshot_pending)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(manager._snapshot_pending, set())
            self.assertEqual(len(await manager.list_events("lock")), 1)


if __name__ == "__main__":
    unittest.main()
