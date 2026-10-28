"""Recording selection races, stop/unload, and independent live camera state."""
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_camera_live import load_camera_module


class HddCameraTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_camera_module()
        self.bridges = []
        def bridge(binary, factory, **kwargs):
            self.assertEqual(kwargs.get("source_mode"), "playback")
            self.assertEqual(kwargs.get("stream_quality", "default"), "default")
            item = types.SimpleNamespace(async_prepare=AsyncMock(), async_close=AsyncMock(),
                async_start=AsyncMock(return_value="http://127.0.0.1/private.ts"),
                last_stage="idle", diagnostics={}, factory=factory)
            self.bridges.append(item)
            return item
        self.patch = patch.object(self.module, "MeariVideoBridge", side_effect=bridge)
        # Base init briefly creates an unused live bridge; allow that constructor.
        def all_bridges(binary, factory, **kwargs):
            if kwargs.get("source_mode", "live") == "live":
                self.assertEqual(kwargs.get("stream_quality"), "default")
                return object()
            return bridge(binary, factory, **kwargs)
        self.patch = patch.object(self.module, "MeariVideoBridge", side_effect=all_bridges)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.bootstrap = types.SimpleNamespace(context=AsyncMock(return_value={"secret": "PRIVATE"}))
        self.description = self.module.LiveCameraDescription("nvr", "vendor", 3, True)
        self.entity = self.module.OrviboHddCamera(object(), {}, self.description, self.bootstrap, "ffmpeg")
        self.entity.entity_id = "camera.nvr_hdd_3"
        self.entity.hass = types.SimpleNamespace(data={}, bus=types.SimpleNamespace(async_listen_once=lambda *args: lambda: None))

    async def test_idle_thumbnail_and_source_never_open_recorder(self):
        self.assertIsNone(await self.entity.async_create_stream())
        self.assertIsNone(await self.entity.stream_source())
        await self.entity.async_camera_image()
        self.bootstrap.context.assert_not_called()
        self.assertEqual(self.entity.extra_state_attributes["video_mode"], "hdd_playback")

    async def test_select_refreshes_private_context_without_saving_it_in_attributes(self):
        await self.entity.async_select_recording("20261015092301")
        context = await self.entity._bridge.factory()
        self.assertEqual(context["hdd"], {"operation": "playback", "date": "20261015092301"})
        self.bootstrap.context.assert_awaited_once_with(self.description)
        self.assertNotIn("PRIVATE", repr(self.entity.extra_state_attributes))
        with self.assertRaises(ValueError):
            await self.entity.async_select_recording("20260230010101")
        self.assertEqual(self.entity._position, "20261015092301")

    async def test_sd_search_and_playback_use_same_zero_channel_descriptor_and_registry(self):
        description = self.module.LiveCameraDescription("s1", "vendor-s1", 0, False)
        private = {"channel_id": 0, "device": {"devTypeID": 2}, "secret": "PRIVATE"}
        bootstrap = types.SimpleNamespace(context=AsyncMock(side_effect=lambda _: dict(private)))
        entity = self.module.OrviboHddCamera(object(), {"device_name": "S1"}, description, bootstrap, "ffmpeg")
        entity.entity_id, entity.hass = "camera.s1_sd", self.entity.hass
        await entity.async_added_to_hass()
        self.assertIs(entity.hass.data[self.module.DATA_KEY][entity.entity_id], entity)
        with patch.object(self.module, "async_query_hdd", new=AsyncMock(return_value={"stage": "hdd_search_received"})) as query:
            for operation, date in (("month", "202610"), ("day", "20261015")):
                await entity.async_search_recordings(operation, date)
                sent = query.call_args.args[0]
                self.assertEqual(sent["channel_id"], 0)
                self.assertEqual(sent["hdd"], {"operation": operation, "date": date})
        await entity.async_select_recording("20261015090000")
        current = entity._bridge
        sent = await current.factory()
        self.assertEqual(sent["channel_id"], 0)
        self.assertEqual(sent["hdd"], {"operation": "playback", "date": "20261015090000"})
        self.assertNotIn("PRIVATE", repr(entity.extra_state_attributes))
        self.assertEqual(entity.extra_state_attributes["video_mode"], "sd_playback")
        await entity.async_will_remove_from_hass()
        current.async_close.assert_awaited_once()
        self.assertNotIn(entity.entity_id, entity.hass.data[self.module.DATA_KEY])
        self.assertIsNone(await entity.stream_source())

    async def test_reselect_closes_old_bridge_and_stream_before_creating_fresh_source(self):
        await self.entity.async_select_recording("20261015090000")
        previous = self.entity._bridge
        stream = types.SimpleNamespace(stop=AsyncMock(), dynamic_stream_settings=types.SimpleNamespace(preload_stream=True))
        self.entity.stream = stream
        await self.entity.async_select_recording("20261015100000")
        previous.async_close.assert_awaited_once()
        stream.stop.assert_awaited_once()
        self.assertFalse(stream.dynamic_stream_settings.preload_stream)
        self.assertIsNone(self.entity.stream)
        self.assertIsNot(self.entity._bridge, previous)
        await self.entity.async_stop_recording()
        self.assertIsNone(self.entity._position)
        self.assertIsNone(await self.entity.stream_source())

    async def test_slow_old_warmup_cannot_install_stream_after_timestamp_changed(self):
        await self.entity.async_select_recording("20261015090000")
        old = self.entity._bridge
        started, release = asyncio.Event(), asyncio.Event()
        async def prepare():
            started.set()
            await release.wait()
        old.async_prepare.side_effect = prepare
        task = asyncio.create_task(self.entity.async_create_stream())
        await started.wait()
        await self.entity.async_select_recording("20261015100000")
        release.set()
        self.assertIsNone(await task)
        self.entity._bridge.async_start.assert_not_called()

    async def test_unload_unregisters_and_cancels_query_before_returning(self):
        await self.entity.async_added_to_hass()
        self.assertIs(self.module.DATA_KEY in self.entity.hass.data, True)
        started, cleaned = asyncio.Event(), asyncio.Event()
        async def query(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()
        with patch.object(self.module, "async_query_hdd", side_effect=query):
            task = asyncio.create_task(self.entity.async_search_recordings("day", "20261015"))
            await started.wait()
            duplicate = await self.entity.async_search_recordings("day", "20261015")
            self.assertEqual(duplicate["stage"], "hdd_query_in_progress")
            await self.entity.async_will_remove_from_hass()
            self.assertTrue(cleaned.is_set())
            self.assertTrue(task.cancelled())
        self.assertEqual(self.entity.hass.data[self.module.DATA_KEY], {})
        with self.assertRaises(ValueError):
            await self.entity.async_select_recording("20261015090000")

    async def test_cancelled_selection_finishes_cleanup_and_publishes_consistent_selection(self):
        started = asyncio.Event()
        async def close():
            started.set()
            await asyncio.sleep(.03)
        self.entity._bridge.async_close.side_effect = close
        task = asyncio.create_task(self.entity.async_select_recording("20261015090000"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.entity._position, "20261015090000")
        self.assertEqual(len(self.bridges), 2)
