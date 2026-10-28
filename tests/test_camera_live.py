"""Camera platform selection, shared stills and unload/HA-stop lifecycle."""
import importlib
import asyncio
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def load_camera_module():
    root = Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"
    package_name = "orvibo_live_camera_platform_tests"
    if f"{package_name}.camera" in sys.modules:
        return sys.modules[f"{package_name}.camera"]
    package = types.ModuleType(package_name)
    package.__path__ = [str(root)]
    sys.modules[package_name] = package

    class Camera:
        def __init__(self):
            self.stream = None

        async def async_create_stream(self):
            return self.stream

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator
            self.removers = []

        async def async_added_to_hass(self):
            pass

        async def async_will_remove_from_hass(self):
            pass

        def async_on_remove(self, callback):
            self.removers.append(callback)

        def async_write_ha_state(self):
            pass

    modules = {}
    for name, attrs in (
        ("homeassistant.components.camera", {"Camera": Camera, "CameraEntityFeature": types.SimpleNamespace(STREAM=2)}),
        ("homeassistant.components.ffmpeg", {"get_ffmpeg_manager": lambda hass: types.SimpleNamespace(binary="ffmpeg")} ),
        ("homeassistant.const", {"EVENT_HOMEASSISTANT_STOP": "homeassistant_stop"}),
        ("homeassistant.exceptions", {"PlatformNotReady": type("PlatformNotReady", (Exception,), {}), "HomeAssistantError": type("HomeAssistantError", (Exception,), {})}),
        ("homeassistant.helpers.aiohttp_client", {"async_get_clientsession": lambda hass: object()}),
        ("homeassistant.config_entries", {"ConfigEntry": object}),
        ("homeassistant.core", {"HomeAssistant": object}),
        ("homeassistant.helpers.entity_platform", {"AddEntitiesCallback": object}),
        ("homeassistant.helpers.update_coordinator", {"CoordinatorEntity": CoordinatorEntity}),
        (f"{package_name}.coordinator", {"OrviboSmartControlCoordinator": object}),
    ):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        modules[name] = module
    with patch.dict(sys.modules, modules):
        return importlib.import_module(f"{package_name}.camera")


class CameraLiveTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_camera_module()

    def platform(self, options=None):
        coordinator = types.SimpleNamespace(devices={
            "nvr": {"device_id": "nvr", "uid": "vendor-nvr", "device_type_raw": 162, "device_name": "录像机"},
            "other": {"device_id": "other", "device_type_raw": 14, "camera_type": 4},
            "lock": {"device_id": "lock", "device_type_raw": 522},
        }, register_lock_camera=MagicMock())
        hass = types.SimpleNamespace(data={"orvibo_smart_control": {"entry": coordinator}})
        entry = types.SimpleNamespace(entry_id="entry", options=options or {})
        return coordinator, hass, entry

    async def test_selected_nvr_gets_two_cameras_joined_to_existing_device(self):
        _, hass, entry = self.platform({"selected_device_ids": ["nvr"]})
        descriptors = [self.module.LiveCameraDescription("nvr", "vendor-nvr", c, True) for c in (1, 3)]
        bootstrap = types.SimpleNamespace(discover=AsyncMock(return_value=descriptors))
        entities = []
        with patch.object(self.module, "LiveCameraBootstrap", return_value=bootstrap):
            await self.module.async_setup_entry(hass, entry, entities.extend)
        self.assertEqual([e._description.channel_id for e in entities], [1, 1, 3, 3])
        self.assertEqual([e._attr_name for e in entities], ["录像机 通道 1", "录像机 通道 1 硬盘回放", "录像机 通道 3", "录像机 通道 3 硬盘回放"])
        self.assertEqual(entities[0].device_info["identifiers"], {("orvibo_smart_control", "nvr")})
        self.assertNotEqual(entities[0]._attr_unique_id, entities[1]._attr_unique_id)
        self.assertEqual(list(bootstrap.discover.call_args.args[0]), ["nvr"])

    async def test_selected_s1_gets_live_and_storage_card_playback_without_changing_nvr_ids(self):
        coordinator, hass, entry = self.platform({"selected_device_ids": ["s1"]})
        coordinator.devices["s1"] = {"device_id": "s1", "uid": "vendor-s1",
            "device_type_raw": 14, "camera_type": 9, "device_name": "巨目2K智能摄像机S1"}
        description = self.module.LiveCameraDescription("s1", "vendor-s1", 0, False)
        bootstrap = types.SimpleNamespace(discover=AsyncMock(return_value=[description]))
        entities = []
        with patch.object(self.module, "LiveCameraBootstrap", return_value=bootstrap):
            await self.module.async_setup_entry(hass, entry, entities.extend)
        self.assertEqual(len(entities), 2)
        live, playback = entities
        playback.entity_id = "camera.s1_sd"
        self.assertEqual(live._attr_unique_id, "orvibo_smart_control_live_s1_0")
        self.assertEqual(playback._attr_unique_id, "orvibo_smart_control_sd_s1_0")
        self.assertEqual(playback._attr_name, "巨目2K智能摄像机S1 存储卡回放")
        self.assertEqual(playback.device_info["identifiers"], {("orvibo_smart_control", "s1")})
        self.assertEqual(playback.extra_state_attributes["video_mode"], "sd_playback")
        self.assertEqual(playback.extra_state_attributes["recording_storage"], "sd_card")
        self.assertEqual(live.extra_state_attributes["playback_entity_id"], "camera.s1_sd")
        self.assertTrue(live.extra_state_attributes["playback_supported"])
        self.assertFalse(live.extra_state_attributes["hdd_playback_supported"])
        self.assertIsNone(live.extra_state_attributes["hdd_playback_entity_id"])
        self.assertEqual(list(bootstrap.discover.call_args.args[0]), ["s1"])
        nvr = self.module.LiveCameraDescription("nvr", "vendor-nvr", 3, True)
        old = self.module.OrviboHddCamera(coordinator, {}, nvr, bootstrap, "ffmpeg")
        self.assertEqual(old._attr_unique_id, "orvibo_smart_control_hdd_nvr_3")
        self.assertEqual(old.extra_state_attributes["video_mode"], "hdd_playback")

    async def test_deselected_nvr_and_other_sdk_do_not_start_cloud_discovery(self):
        _, hass, entry = self.platform({"selected_device_ids": ["other"]})
        entities = []
        with patch.object(self.module, "LiveCameraBootstrap") as bootstrap:
            await self.module.async_setup_entry(hass, entry, entities.extend)
        bootstrap.assert_not_called()
        self.assertEqual(entities, [])

    async def test_fluent_setting_applies_to_live_cameras_only(self):
        for setting, expected in (("fluent", "fluent"), ("default", "default"), ("bad", "default")):
            with self.subTest(setting=setting):
                _, hass, entry = self.platform({"selected_device_ids": ["nvr"], "camera_stream_quality": setting})
                description = self.module.LiveCameraDescription("nvr", "vendor-nvr", 3, True)
                bootstrap = types.SimpleNamespace(discover=AsyncMock(return_value=[description]))
                entities = []
                with patch.object(self.module, "LiveCameraBootstrap", return_value=bootstrap):
                    await self.module.async_setup_entry(hass, entry, entities.extend)
                live, playback = entities
                self.assertEqual(live._bridge.diagnostics["requested_quality"], expected)
                self.assertEqual(playback._bridge.diagnostics["source_mode"], "playback")
                self.assertIsNone(playback._bridge.diagnostics["requested_quality"])

    async def test_retry_does_not_add_partial_lock_entities(self):
        _, hass, entry = self.platform()
        bootstrap = types.SimpleNamespace(discover=AsyncMock(side_effect=ValueError("SECRET")))
        entities = []
        with patch.object(self.module, "LiveCameraBootstrap", return_value=bootstrap):
            with self.assertRaises(self.module.PlatformNotReady) as error:
                await self.module.async_setup_entry(hass, entry, entities.extend)
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(entities, [])

    async def test_lock_snapshot_can_restore_before_added_and_updates_state_when_loaded(self):
        coordinator, _, _ = self.platform()
        coordinator.lock_media = types.SimpleNamespace(unregister_camera=MagicMock())
        entity = self.module.OrviboLockSnapshotCamera(coordinator, coordinator.devices["lock"])
        entity.async_write_ha_state = MagicMock()
        coordinator.register_lock_camera.assert_called_once_with("lock", entity)
        await entity.async_set_image(b"restored")
        revision = entity.extra_state_attributes["snapshot_revision"]
        self.assertEqual(entity.camera_image(), b"restored")
        entity.async_write_ha_state.assert_not_called()
        entity.hass = object()
        await entity.async_set_image(b"new")
        self.assertGreater(entity.extra_state_attributes["snapshot_revision"], revision)
        self.assertEqual(entity.camera_image(), b"new")
        entity.async_write_ha_state.assert_called_once()
        await entity.async_will_remove_from_hass()
        coordinator.lock_media.unregister_camera.assert_called_once_with("lock", entity)

    def entity(self):
        description = self.module.LiveCameraDescription("nvr", "private-vendor", 1, True)
        bootstrap = types.SimpleNamespace(context=AsyncMock(return_value={"secret": "TOKEN"}))
        entity = self.module.OrviboLiveCamera(object(), {"device_name": "NVR"}, description, bootstrap, "ffmpeg")
        entity._bridge = types.SimpleNamespace(async_start=AsyncMock(return_value="http://127.0.0.1/private.ts"), async_prepare=AsyncMock(), async_close=AsyncMock(), last_stage="idle", diagnostics={})
        return entity

    async def test_stream_is_demand_driven_and_stills_share_ha_stream(self):
        entity = self.entity()
        self.assertTrue(entity.use_stream_for_stills)
        entity._bridge.async_start.assert_not_called()
        self.assertEqual(await entity.stream_source(), "http://127.0.0.1/private.ts")
        stream = types.SimpleNamespace(async_get_image=AsyncMock(return_value=b"jpeg"))
        entity.async_create_stream = AsyncMock(return_value=stream)
        self.assertEqual(await entity.async_camera_image(640, 360), b"jpeg")
        stream.async_get_image.assert_awaited_once_with(width=640, height=360)
        self.assertNotIn("private", repr(entity.extra_state_attributes))
        self.assertTrue(entity.extra_state_attributes["hdd_playback_supported"])

    async def test_remove_or_ha_stop_stops_stream_then_bridge_even_if_stream_fails(self):
        for error in (None, RuntimeError("stop failed")):
            entity = self.entity()
            order = []

            async def stop():
                order.append("stream")
                if error:
                    raise error

            async def close():
                order.append("bridge")

            entity.stream = types.SimpleNamespace(stop=stop, dynamic_stream_settings=types.SimpleNamespace(preload_stream=True))
            entity._bridge.async_close.side_effect = close
            if error:
                with self.assertRaises(RuntimeError):
                    await entity._async_stop()
            else:
                await entity.async_will_remove_from_hass()
            self.assertEqual(order, ["bridge", "stream"])
            self.assertFalse(entity.stream.dynamic_stream_settings.preload_stream)
            self.assertIsNone(await entity.stream_source())

    async def test_warmup_precedes_base_stream_creation_and_closed_cannot_restart(self):
        entity = self.entity()
        entity.stream = object()
        self.assertIs(await entity.async_create_stream(), entity.stream)
        entity._bridge.async_prepare.assert_awaited_once()
        entity._closed = True
        self.assertIsNone(await entity.async_create_stream())
        self.assertEqual(entity._bridge.async_prepare.await_count, 1)

    async def test_warmup_failure_does_not_expose_private_message(self):
        entity = self.entity()
        entity._bridge.async_prepare.side_effect = RuntimeError("TOKEN-SECRET")
        with self.assertRaises(self.module.HomeAssistantError) as error:
            await entity.async_create_stream()
        self.assertNotIn("TOKEN", str(error.exception))

    async def test_added_entity_registers_removable_ha_stop_callback(self):
        entity = self.entity()
        entity.hass = types.SimpleNamespace(bus=types.SimpleNamespace(async_listen_once=MagicMock(return_value="unsubscribe")))
        await entity.async_added_to_hass()
        event, callback = entity.hass.bus.async_listen_once.call_args.args
        self.assertEqual(event, "homeassistant_stop")
        self.assertEqual(entity.removers, ["unsubscribe"])
        await callback(object())
        entity._bridge.async_close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
