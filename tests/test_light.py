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


def _load_modules():
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
    return light, inventory, protocol


class LightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.light, cls.inventory, cls.protocol = _load_modules()

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


if __name__ == "__main__":
    unittest.main()
