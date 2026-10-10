"""Tests for device discovery and cloud inventory reconciliation."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"


def _load_module():
    package_name = "orvibo_smart_control_device_inventory_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.device_inventory")


class FakeClient:
    def __init__(self, status=None, description=None, homepage=None):
        self.status = status
        self.description = description
        self.homepage = homepage

    async def fetch_device_status(self):
        return self.status

    async def fetch_device_desc(self, last_update_time=0):
        return self.description

    async def fetch_homepage_data(self):
        return self.homepage

    def parse_device_status_list(self, payload):
        return payload.get("parsed", payload.get("device", []))


class DeviceInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def make_inventory(self, client=None):
        devices = {}
        states = {}
        removed = []
        store = self.module.StateStore(states)
        inventory = self.module.DeviceInventory(
            client or FakeClient(), devices, states, store, removed.append
        )
        return inventory, devices, states, removed

    def test_discover_uses_description_fallback(self) -> None:
        client = FakeClient(
            status={"parsed": []},
            description={"deviceDescList": [{"device_id": "light"}]},
        )
        inventory, _, _, _ = self.make_inventory(client)

        status, devices = asyncio.run(inventory.discover())

        self.assertEqual(status, {"parsed": []})
        self.assertEqual(devices, [{"device_id": "light"}])

    def test_initialize_filters_hidden_and_adds_category_defaults(self) -> None:
        inventory, devices, states, _ = self.make_inventory()

        inventory.initialize(
            [
                {
                    "device_id": "gateway",
                    "device_type_raw": 114,
                },
                {
                    "device_id": "lock",
                    "device_type_raw": 522,
                    "sub_device_type": 463,
                    "online": "online",
                    "properties": {
                        "batteryManager": {
                            "level": 80,
                            "isSetupBattery": "on",
                        }
                    },
                },
                {
                    "device_id": "rack",
                    "device_type_raw": 52,
                },
            ]
        )

        self.assertNotIn("gateway", devices)
        self.assertTrue(states["lock"]["online"])
        self.assertIn("dry_battery_level", states["lock"])
        self.assertEqual(states["rack"]["motor_state"], "stop")

    def test_initialize_honors_inverted_light_subdevice_type(self) -> None:
        inventory, _devices, states, _ = self.make_inventory()

        inventory.initialize(
            [
                {
                    "device_id": "light",
                    "device_type_raw": 38,
                    "sub_device_type": -2,
                    "value1": 1,
                    "value2": 26,
                    "value3": 2700,
                }
            ]
        )

        self.assertFalse(states["light"]["state"])
        self.assertEqual(states["light"]["brightness"], 26)

    def test_merge_cloud_removes_hidden_and_merges_status(self) -> None:
        inventory, devices, states, removed = self.make_inventory()
        devices["old"] = {"device_id": "old"}
        states["old"] = {"state": True}

        inventory.merge_cloud(
            [
                {"device_id": "old", "device_type_raw": 114},
                {
                    "device_id": "light",
                    "device_type_raw": 1,
                    "online": True,
                    "status": {"value1": 1},
                },
            ]
        )

        self.assertNotIn("old", devices)
        self.assertEqual(removed, ["old"])
        self.assertTrue(states["light"]["online"])
        self.assertEqual(states["light"]["value1"], 1)

    def test_merge_cloud_refreshes_door_lock_state(self) -> None:
        inventory, _devices, states, _removed = self.make_inventory()

        inventory.merge_cloud(
            [
                {
                    "device_id": "lock",
                    "device_type_raw": 522,
                    "sub_device_type": 463,
                    "online": True,
                    "properties": {
                        "doorLock": {"doorState": "on", "lockState": "on"}
                    },
                }
            ]
        )

        self.assertTrue(states["lock"]["door_state"])
        self.assertFalse(states["lock"]["locked"])
        self.assertEqual(states["lock"]["lock_status"], "unlocked")

    def test_property_light_cloud_refresh_does_not_replace_valid_percentage(self):
        for device_type, subtype in ((502, 431), (503, 436), (503, 461)):
            with self.subTest(device_type=device_type, subtype=subtype):
                inventory, _, states, _ = self.make_inventory()
                protocol = importlib.import_module(
                    self.module.__package__ + ".protocol"
                )
                device = protocol.device_to_dict(protocol.OrviboDevice(
                    uid="test-light", name="Test light", model="",
                    device_type=str(device_type), sub_device_type=str(subtype),
                    room="", parent_uid="", online=True,
                    value1=0, value2=-1, value3=-1,
                    properties={"onoff": {"status": "on"},
                                "brightness": {"percent": 80},
                                "colorTemp": {"value": 4000}},
                ))
                inventory.initialize([device])
                self.assertEqual(states["test-light"]["brightness"], 80)
                inventory.merge_cloud([device])
                self.assertEqual(states["test-light"]["brightness"], 80)
                self.assertTrue(states["test-light"]["state"])

    def test_invalid_cloud_level_preserves_previous_valid_level(self):
        for device_type, subtype in ((38, -2), (38, 6), (0, -2), (503, 436)):
            with self.subTest(device_type=device_type, subtype=subtype):
                inventory, _, states, _ = self.make_inventory()
                device = {"device_id": "lamp", "device_type_raw": device_type,
                          "sub_device_type": subtype, "value1": 0, "value2": -1,
                          "brightness": -1, "value3": -1, "color_temp": -1,
                          "state": True, "online": True}
                states["lamp"] = {"state": True, "brightness": 80, "color_temp": 4000}
                inventory.merge_cloud([device])
                self.assertEqual(states["lamp"]["brightness"], 80)
                self.assertEqual(states["lamp"]["color_temp"], 4000)
                self.assertTrue(states["lamp"]["state"])

    def test_invalid_initial_level_is_unknown_for_both_discovery_paths(self):
        for method in ("initialize", "merge_cloud"):
            for device_type, subtype in ((38, -2), (38, 6), (0, -2), (503, 436)):
                with self.subTest(method=method, device_type=device_type, subtype=subtype):
                    inventory, _, states, _ = self.make_inventory()
                    getattr(inventory, method)([{
                        "device_id": "lamp", "device_type_raw": device_type,
                        "sub_device_type": subtype, "state": True, "value1": 0,
                        "value2": -1, "brightness": -1,
                    }])
                    self.assertIsNone(states["lamp"]["brightness"])
                    self.assertTrue(states["lamp"]["state"])

    def test_cloud_light_refresh_keeps_recent_lan_measurement(self):
        inventory, _, states, _ = self.make_inventory()
        state_module = importlib.import_module(self.module.__package__ + ".state_store")
        states["lamp"] = {"state": True, "brightness": 80}
        inventory.state_store.merge("lamp", {"brightness": 90}, state_module.StateSource.LAN)
        inventory.merge_cloud([{
            "device_id": "lamp", "device_type_raw": 503, "sub_device_type": 436,
            "value2": -1, "brightness": -1,
            "properties": {"brightness": {"percent": 50}},
        }])
        self.assertEqual(states["lamp"]["brightness"], 90)

    def test_cloud_light_refresh_accepts_valid_legacy_snapshot(self):
        for device_type, subtype in ((38, -2), (38, 6), (503, 436)):
            with self.subTest(device_type=device_type, subtype=subtype):
                inventory, _, states, _ = self.make_inventory()
                device = {"device_id": "lamp", "device_type_raw": device_type,
                          "sub_device_type": subtype, "value1": 0, "value2": 80,
                          "value3": 4000, "brightness": 80, "color_temp": 4000}
                inventory.initialize([device])
                self.assertEqual(states["lamp"]["color_temp"], 4000)
                inventory.merge_cloud([device])
                self.assertTrue(states["lamp"]["state"])
                self.assertEqual(states["lamp"]["brightness"], 80)
                self.assertEqual(states["lamp"]["color_temp"], 4000)
                device["value1"] = 1
                inventory.merge_cloud([device])
                self.assertFalse(states["lamp"]["state"])

    def test_cloud_legacy_light_converts_properties_and_preserves_partial_updates(self):
        inventory, _, states, _ = self.make_inventory()
        device = {"device_id": "lamp", "device_type_raw": 38, "sub_device_type": -2,
                  "value1": 0, "value2": -1, "value3": -1,
                  "brightness": -1, "color_temp": -1,
                  "properties": {"brightness": {"percent": 80},
                                 "colorTemp": {"value": 4000}}}
        inventory.merge_cloud([device])
        self.assertEqual(states["lamp"]["brightness"], 204)
        self.assertEqual(states["lamp"]["color_temp"], 4000)
        inventory.merge_cloud([{
            "device_id": "lamp", "device_type_raw": 38, "sub_device_type": -2,
            "status": {"value1": 1},
        }])
        self.assertFalse(states["lamp"]["state"])
        self.assertEqual(states["lamp"]["brightness"], 204)
        self.assertEqual(states["lamp"]["color_temp"], 4000)


if __name__ == "__main__":
    unittest.main()
