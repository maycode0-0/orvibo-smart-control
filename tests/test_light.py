"""Light entity regressions for invalid readings and cloud refreshes."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


def _module(name, **values):
    module = ModuleType(name)
    module.__dict__.update(values)
    return module


def _load_modules(*, include_dispatcher=False):
    name = "orvibo_smart_control_light_entity_test"
    package = _module(name)
    package.__path__ = [str(
        Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"
    )]

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    class LightEntity:
        pass

    modules = {
        name: package,
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.light": _module(
            "homeassistant.components.light", LightEntity=LightEntity,
            ColorMode=SimpleNamespace(BRIGHTNESS="brightness", COLOR_TEMP="color_temp", ONOFF="onoff"),
        ),
        "homeassistant.config_entries": _module("homeassistant.config_entries", ConfigEntry=object),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=object),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.entity_platform": _module(
            "homeassistant.helpers.entity_platform", AddEntitiesCallback=object,
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity,
        ),
        f"{name}.coordinator": _module(
            f"{name}.coordinator", OrviboSmartControlCoordinator=object,
        ),
    }
    with patch.dict(sys.modules, modules):
        light = importlib.import_module(f"{name}.light")
        inventory = importlib.import_module(f"{name}.device_inventory")
        protocol = importlib.import_module(f"{name}.protocol")
        dispatcher = importlib.import_module(f"{name}.status_dispatcher")
    if include_dispatcher:
        return light, inventory, protocol, dispatcher
    return light, inventory, protocol


class LightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.light, cls.inventory, cls.protocol, cls.dispatcher = _load_modules(include_dispatcher=True)

    def make_entity(self, state, device_type=503, subtype=436):
        coordinator = SimpleNamespace(get_device_state=lambda _: state)
        return self.light.OrviboLight(coordinator, {
            "device_id": "lamp", "device_type_raw": device_type,
            "sub_device_type": subtype,
        })

    def test_invalid_cached_brightness_is_unknown_while_light_stays_on(self):
        for device_type, subtype in ((503, 436), (502, 431), (38, -2), (38, 6), (0, -2)):
            for value in (-1, -2, -0.5, "-1", "unknown", {}, None, True, "nan", "inf"):
                with self.subTest(device_type=device_type, value=value):
                    state = {"state": True, "brightness": value}
                    entity = self.make_entity(state, device_type, subtype)
                    self.assertIsNone(entity.brightness)
                    self.assertTrue(entity.is_on)

    def test_valid_levels_keep_their_units_and_bounds(self):
        for device_type, subtype, value, expected in (
            (503, 436, 80, 204), (502, 431, "50", 127),
            (503, 436, 120, 255), (38, -2, 128, 128),
            (38, -2, 300, 255), (38, -2, 0, 0),
        ):
            with self.subTest(device_type=device_type, value=value):
                state = {"state": True, "brightness": value}
                entity = self.make_entity(state, device_type, subtype)
                self.assertEqual(entity.brightness, expected)
                state["state"] = False
                self.assertIsNone(entity.brightness)

    def test_readtable_refresh_and_turn_on_never_expose_negative_percent(self):
        device = self.protocol.device_to_dict(self.protocol.OrviboDevice(
            uid="lamp", name="Test light", model="", device_type="503",
            sub_device_type="436", room="", parent_uid="", online=True,
            value1=0, value2=-1, value3=-1,
            properties={"onoff": {"status": "on"}, "brightness": {"percent": 80}},
        ))
        states = {}
        store = self.inventory.StateStore(states)
        inventory = self.inventory.DeviceInventory(None, {}, states, store, lambda _: None)
        inventory.initialize([device])
        entity = self.make_entity(states["lamp"])
        self.assertEqual(entity.brightness, 204)
        inventory.merge_cloud([device])
        self.assertEqual(entity.brightness, 204)
        # An on/off-only push must preserve the last valid brightness.
        device["properties"] = {"onoff": {"status": "off"}}
        inventory.merge_cloud([device])
        self.assertIsNone(entity.brightness)
        device["properties"] = {"onoff": {"status": "on"}}
        inventory.merge_cloud([device])
        self.assertTrue(entity.is_on)
        self.assertEqual(entity.brightness, 204)

    def test_readtable_light_count_uses_power_instead_of_retained_brightness(self):
        for device_type, subtype in ((38, -2), (38, 6), (0, -2), (116, -2), (503, 436)):
            with self.subTest(device_type=device_type, subtype=subtype):
                payload = {"data": {
                    "device": [{"deviceId": f"lamp-{i}", "deviceType": device_type,
                                "subDeviceType": subtype} for i in range(9)],
                    "deviceStatus": [{"deviceId": f"lamp-{i}", "online": 1,
                                      "value1": 0, "value2": 13,
                                      "properties": {"onoff": {"status": "on" if i < 4 else "off"}}}
                                     for i in range(9)],
                }}
                devices = [self.protocol.device_to_dict(device)
                           for device in self.protocol.parse_readtable_devices(payload)]
                states = {}
                inventory = self.inventory.DeviceInventory(
                    None, {}, states, self.inventory.StateStore(states), lambda _: None,
                )
                inventory.initialize(devices)
                coordinator = SimpleNamespace(get_device_state=states.get)
                entities = [self.light.OrviboLight(coordinator, device) for device in devices]
                self.assertEqual(sum(entity.is_on for entity in entities), 4)
                # A cloud refresh must also correct previously cached false positives.
                for state in states.values():
                    state["state"] = True
                inventory.merge_cloud(devices)
                self.assertEqual(sum(entity.is_on for entity in entities), 4)
                self.assertTrue(all(entity.brightness is None for entity in entities[4:]))
                self.assertTrue(all(state["brightness"] == 13 for state in states.values()))

    def test_property_off_push_then_brightness_push_keeps_light_off(self):
        for device_type, subtype in ((38, -2), (38, 6), (0, -2), (116, -2), (503, 436)):
            with self.subTest(device_type=device_type, subtype=subtype):
                device = {"device_id": "lamp", "device_type_raw": device_type,
                          "sub_device_type": subtype}
                states = {"lamp": {"state": True, "brightness": 13}}
                dispatcher = self.dispatcher.StatusUpdateDispatcher(
                    {"lamp": device}, states, self.inventory.StateStore(states), {}, [],
                    on_motion=lambda *args: None, on_emergency=lambda *args: None,
                    on_lock_transient=lambda *args: None, on_lock_message=lambda *args: None,
                    on_lock_event=lambda *args: None, on_updated=lambda: None,
                )
                entity = self.light.OrviboLight(SimpleNamespace(get_device_state=states.get), device)
                dispatcher.dispatch("lamp", {"properties": {"onoff": {"status": "off"}}})
                self.assertFalse(entity.is_on)
                dispatcher.dispatch("lamp", {"properties": {"brightness": {"percent": 5}}})
                self.assertFalse(entity.is_on)
                self.assertIsNone(entity.brightness)
                dispatcher.dispatch("lamp", {"properties": {"onoff": {"status": "on"}}})
                self.assertTrue(entity.is_on)
                self.assertGreater(entity.brightness, 0)


if __name__ == "__main__":
    unittest.main()
