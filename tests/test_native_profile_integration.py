"""Exercise native profiles through entities, inventory, routing and transports."""

from __future__ import annotations

import importlib
import sys
import unittest
from enum import IntFlag
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_light import _load_modules, _module


class CoverFeatures(IntFlag):
    OPEN = 1
    CLOSE = 2
    STOP = 4
    SET_POSITION = 8
    SET_TILT_POSITION = 16


class NativeProfileIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.light, cls.inventory_module, cls.protocol = _load_modules()
        package = cls.light.__package__
        namespace = _module(package)
        namespace.__path__ = [str(Path(cls.light.__file__).parent)]
        sys.modules[package] = namespace
        cls.executor_module = importlib.import_module(f"{package}.control_executor")
        cls.dispatcher_module = importlib.import_module(f"{package}.status_dispatcher")
        homeassistant = _module("homeassistant")
        homeassistant.__path__ = []
        core = _module("homeassistant.core", HomeAssistant=object)
        with patch.dict(sys.modules, {"homeassistant": homeassistant, "homeassistant.core": core}):
            cls.ssl_module = importlib.import_module(f"{package}.ssl_client")
        cls.lan_module = importlib.import_module(f"{package}.lan.control_adapter")
        with patch.dict(sys.modules, {
            "homeassistant.components.cover": _module(
                "homeassistant.components.cover", CoverEntity=type("CoverEntity", (), {}),
                CoverDeviceClass=SimpleNamespace(
                    SHUTTER="shutter", CURTAIN="curtain", AWNING="awning",
                ),
                CoverEntityFeature=CoverFeatures,
            ),
            "homeassistant.config_entries": _module("config_entries", ConfigEntry=object),
            "homeassistant.core": _module("core", HomeAssistant=object),
            "homeassistant.helpers.entity_platform": _module("platform", AddEntitiesCallback=object),
            "homeassistant.helpers.update_coordinator": _module(
                "coordinator", CoordinatorEntity=cls.light.CoordinatorEntity,
            ),
            f"{package}.coordinator": _module("coordinator", OrviboSmartControlCoordinator=object),
        }):
            cls.cover = importlib.import_module(f"{package}.cover")

    def harness(self, device_type, *, model="", rf=False, lan=False):
        device = self.protocol.device_to_dict(self.protocol.OrviboDevice(
            uid="lamp", name="Fixture", model=model, device_type=str(device_type),
            sub_device_type="-2", room="", parent_uid="gateway", online=True,
            value1=0, value2=60 if device_type == 116 else 129, value3=17, value4=23,
            is_rf_curtain=rf,
        ))
        devices, states = {}, {}
        store = self.inventory_module.StateStore(states)
        inventory = self.inventory_module.DeviceInventory(None, devices, states, store, lambda _: None)
        inventory.initialize([device])
        packets = []

        async def send_packet(payload, _key):
            packets.append(payload)

        async def send_lan(_uid, payload, **_kwargs):
            packets.append(payload)
            return {"cmd": 15, "status": 0}

        ssl = self.ssl_module.SSLClient.__new__(self.ssl_module.SSLClient)
        ssl.username, ssl.session_key = "fixture", b"fixture-session!"
        ssl.connect_and_login = AsyncMock()
        ssl._send_packet = send_packet
        ssl._wait_for_control_response = AsyncMock(return_value=None)
        adapter = self.lan_module.LanControlAdapter("fixture", SimpleNamespace(send=send_lan))
        executor = self.executor_module.ControlExecutor(
            devices, states, store, lambda: ssl, lambda: None, states.get, lambda: None,
            lan_adapter=lambda: adapter if lan else None, gateway_connected=lambda _: lan,
        )
        coordinator = SimpleNamespace(
            get_device_state=states.get, async_set_brightness=executor.set_brightness,
            async_turn_on=executor.turn_on, async_turn_off=executor.turn_off,
            async_set_cover_position=executor.set_cover_position, async_stop_cover=executor.stop_cover,
        )
        dispatcher = self.dispatcher_module.StatusUpdateDispatcher(
            devices, states, store, {}, [],
            on_motion=lambda *args: None, on_emergency=lambda *args: None,
            on_lock_transient=lambda *args: None, on_lock_message=lambda *args: None,
            on_lock_event=lambda *args: None, on_updated=lambda: None,
        )
        return device, states, inventory, executor, coordinator, dispatcher, packets

    async def test_brightness_round_trip_and_cached_power_on_both_transports(self):
        for device_type, model, level in (
            (0, "", 129), (116, "", 60), (0, "VirtualModelidForMixpadD", 128),
        ):
            for lan in (False, True):
                with self.subTest(device_type=device_type, model=model, lan=lan):
                    device, states, _, executor, coordinator, dispatcher, packets = self.harness(
                        device_type, model=model, lan=lan,
                    )
                    entity = self.light.OrviboLight(coordinator, device)
                    await entity.async_turn_on(brightness=128)
                    self.assertEqual(packets[-1]["order"], "move to level")
                    self.assertEqual(packets[-1]["value2"], level)
                    self.assertEqual(packets[-1]["value3"], 17)
                    self.assertEqual(packets[-1]["value4"], 0 if model else 23)
                    self.assertEqual(states["lamp"]["brightness"], level)
                    self.assertEqual(entity.brightness, 128)
                    dispatcher.dispatch("lamp", {"cmd": 42, "value1": 0, "value2": level,
                                                  "value3": 19, "value4": 29})
                    self.assertEqual(entity.brightness, 128)
                    await entity.async_turn_off()
                    await entity.async_turn_on()
                    self.assertEqual(packets[-1]["value2"], level)
                    self.assertEqual(packets[-1]["value3"], 19)
                    self.assertEqual(packets[-1]["value4"], 0 if model else 29)
                    self.assertEqual(entity.brightness, 128)
                    self.assertEqual(executor.last_transport("lamp"), "lan" if lan else "cloud")

    async def test_native_initial_cloud_and_property_push_units(self):
        for device_type, level in ((0, 129), (116, 60)):
            with self.subTest(device_type=device_type):
                device, states, inventory, _, coordinator, dispatcher, _ = self.harness(device_type)
                entity = self.light.OrviboLight(coordinator, device)
                self.assertEqual(entity.brightness, 128)
                snapshot = dict(device, value2=-1, properties={
                    "brightness": {"percent": 50}, "onoff": {"status": "on"},
                })
                inventory.merge_cloud([snapshot])
                self.assertEqual(states["lamp"]["brightness"], level)
                self.assertEqual(entity.brightness, 128)
                dispatcher.dispatch("lamp", {"cmd": 42, "value2": -1, "properties": {
                    "brightness": {"percent": 50}, "onoff": {"status": "on"},
                }})
                self.assertEqual(states["lamp"]["brightness"], level)
                dispatcher.dispatch("lamp", {"cmd": 42, "properties": {"onoff": {"status": "off"}}})
                self.assertFalse(entity.is_on)
                self.assertEqual(states["lamp"]["brightness"], level)
                dispatcher.dispatch("lamp", {"cmd": 42, "value2": -1, "properties": {"onoff": {"status": "on"}}})
                self.assertEqual(entity.brightness, 128)

    async def test_sentinel_only_push_preserves_native_power_and_mode(self):
        for device_type in (0, 116):
            for sentinel in (-1, "-1", None):
                with self.subTest(device_type=device_type, sentinel=sentinel):
                    _, states, _, _, _, dispatcher, _ = self.harness(device_type)
                    before = dict(states["lamp"])
                    dispatcher.dispatch("lamp", {
                        "cmd": 42, "value1": sentinel, "value2": sentinel,
                        "value3": sentinel, "value4": sentinel,
                    })
                    for field in ("state", "brightness", "raw_value3", "raw_value4"):
                        self.assertEqual(states["lamp"][field], before[field])

    async def test_zero_is_off_and_small_nonzero_is_dimmable(self):
        for device_type in (0, 116):
            device, states, _, executor, coordinator, _, packets = self.harness(device_type)
            entity = self.light.OrviboLight(coordinator, device)
            await entity.async_turn_on(brightness=1)
            self.assertGreater(packets[-1]["value2"], 0)
            await entity.async_turn_on(brightness=0)
            self.assertEqual((packets[-1]["order"], packets[-1]["value1"]), ("off", 1))
            self.assertFalse(states["lamp"]["state"])
            await executor.turn_on("lamp", brightness=128)
            self.assertEqual(entity.brightness, 128)
            await executor.set_brightness("lamp", 0)
            self.assertEqual(packets[-1]["order"], "off")

    async def test_rf_entity_controls_reach_packets_without_position_claims(self):
        for device_type in (34, 35):
            for lan in (False, True):
                with self.subTest(device_type=device_type, lan=lan):
                    device, _, _, executor, coordinator, _, packets = self.harness(device_type, rf=True, lan=lan)
                    entity = self.cover.OrviboCover(coordinator, device)
                    self.assertFalse(entity._attr_supported_features & CoverFeatures.SET_POSITION)
                    self.assertIsNone(entity.current_cover_position)
                    self.assertIsNone(entity.is_closed)
                    await entity.async_open_cover()
                    await entity.async_close_cover()
                    await entity.async_stop_cover()
                    self.assertEqual([p["order"] for p in packets], ["rf control"] * 3)
                    self.assertEqual([p["value1"] for p in packets], [380000, 380001, 380002])
                    self.assertFalse(await executor.set_cover_position("lamp", 50))
                    self.assertEqual(len(packets), 3)

    async def test_string_and_camel_case_type_use_the_same_dimmer_route(self):
        router = importlib.import_module(f"{self.light.__package__}.control_router")
        category = router.DeviceCategory.DIMMABLE_LIGHT
        for device in ({"device_type_raw": "116"}, {"deviceType": "116"},
                       {"device_type_raw": None, "deviceType": 116}):
            route = router.brightness_route(category, 128, {}, device=device)
            self.assertEqual(route.args, (60,))
            self.assertEqual(route.optimistic["brightness"], 60)
