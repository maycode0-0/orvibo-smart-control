"""Tests for lock media orchestration boundaries."""

from __future__ import annotations

import importlib
import asyncio
from pathlib import Path
import sys
import tempfile
import types
import unittest

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
            saved = Path(td) / "orvibo_smart_control" / "lock" / "ring_1785672298.jpg"
            self.assertEqual(saved.read_bytes(), b"jpg-data")


if __name__ == "__main__":
    unittest.main()
