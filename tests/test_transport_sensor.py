"""Tests for the per-device transport diagnostic entity."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibo_smart_control"
    / "sensor.py"
)


def _module(name: str, **values) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _load_sensor_module():
    package_name = "orvibo_smart_control_transport_sensor_test"
    package = _module(package_name)
    package.__path__ = [str(MODULE_PATH.parent)]

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

        async def async_added_to_hass(self):
            self.coordinator.added = True

        def _handle_coordinator_update(self):
            self.coordinator.updated = True

    class SensorEntity:
        pass

    modules = {
        package_name: package,
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.sensor": _module(
            "homeassistant.components.sensor",
            SensorEntity=SensorEntity,
            SensorDeviceClass=SimpleNamespace(
                BATTERY="battery",
                TEMPERATURE="temperature",
                HUMIDITY="humidity",
                ENUM="enum",
            ),
            SensorStateClass=SimpleNamespace(MEASUREMENT="measurement"),
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
        ),
        "homeassistant.const": _module(
            "homeassistant.const",
            EntityCategory=SimpleNamespace(DIAGNOSTIC="diagnostic"),
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.entity_registry": _module(
            "homeassistant.helpers.entity_registry",
            RegistryEntryHider=SimpleNamespace(INTEGRATION="integration", USER="user"),
            async_get=lambda hass: hass.registry,
        ),
        "homeassistant.helpers.entity_platform": _module(
            "homeassistant.helpers.entity_platform",
            AddEntitiesCallback=object,
        ),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=CoordinatorEntity,
        ),
        f"{package_name}.const": _module(
            f"{package_name}.const",
            DOMAIN="orvibo_smart_control",
            MANUFACTURER="ORVIBO",
        ),
        f"{package_name}.coordinator": _module(
            f"{package_name}.coordinator",
            OrviboSmartControlCoordinator=object,
        ),
        f"{package_name}.selection": _module(
            f"{package_name}.selection",
            selected_device_ids=lambda _options, devices: set(devices),
        ),
    }

    module_name = f"{package_name}.sensor"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    sensor = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        sys.modules[module_name] = sensor
        spec.loader.exec_module(sensor)
    return sensor


class _Control:
    def last_transport(self, device_id: str) -> str | None:
        return "lan" if device_id == "light-1" else None


class _Coordinator:
    def __init__(self, module):
        mode_type = module.transport_path_for.__globals__["TransportMode"]
        self.mode_type = mode_type
        self.transport_mode = mode_type.AUTO
        self.control = _Control()
        self.devices = {
            "light-1": {
                "device_id": "light-1",
                "device_name": "Light",
                "device_type": "light",
                "device_type_raw": 38,
                "uid": "gateway-1",
            },
            "clothes-1": {
                "device_id": "clothes-1",
                "device_name": "Clothes horse",
                "device_type": "sensor",
                "device_type_raw": 52,
                "uid": "wifi-device",
            },
        }

    def lan_gateway_connected(self, device_id: str) -> bool:
        return device_id == "light-1"

    def get_device_state(self, _device_id: str) -> dict:
        return {"online": True}


class TransportSensorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_sensor_module()

    def test_setup_adds_one_transport_marker_per_selected_device(self):
        coordinator = _Coordinator(self.module)
        hass = SimpleNamespace(
            data={
                "orvibo_smart_control": {
                    "entry-1": coordinator,
                }
            }
        )
        entry = SimpleNamespace(entry_id="entry-1", options={})
        entities = []

        asyncio.run(
            self.module.async_setup_entry(hass, entry, entities.extend)
        )

        markers = [
            entity
            for entity in entities
            if isinstance(entity, self.module.OrviboTransportPathSensor)
        ]
        self.assertEqual(len(markers), 2)
        by_device = {entity._device_id: entity for entity in markers}
        self.assertEqual(by_device["light-1"].native_value, "lan_cloud")
        self.assertEqual(by_device["clothes-1"].native_value, "cloud")
        self.assertEqual(
            by_device["light-1"].extra_state_attributes,
            {
                "configured_mode": "auto",
                "lan_control_supported": True,
                "cloud_control_supported": True,
                "cloud_only": False,
                "gateway_connected": True,
                "last_control_transport": "lan",
            },
        )

        coordinator.transport_mode = coordinator.mode_type.LAN_ONLY
        self.assertEqual(by_device["light-1"].native_value, "lan")
        self.assertEqual(by_device["clothes-1"].native_value, "unavailable")

    def test_camera_sdk_sensor_tracks_refreshes_and_offline_metadata(self):
        coordinator = _Coordinator(self.module)
        device = {"device_id": "camera", "device_type_raw": 14,
                  "camera_type": 4, "online": False}
        coordinator.devices = {"camera": device, "nvr": {
            "device_id": "nvr", "device_type_raw": 162, "online": False,
        }}
        hass = SimpleNamespace(data={"orvibo_smart_control": {"entry-1": coordinator}})
        entry = SimpleNamespace(entry_id="entry-1", options={})
        entities = []
        asyncio.run(self.module.async_setup_entry(hass, entry, entities.extend))
        sdk_entities = {entity._device_id: entity for entity in entities
                        if isinstance(entity, self.module.OrviboCameraSdkSensor)}
        self.assertEqual(set(sdk_entities), {"camera", "nvr"})
        camera = sdk_entities["camera"]
        self.assertTrue(camera.available)
        self.assertEqual(camera.native_value, "danale")
        self.assertEqual(sdk_entities["nvr"].native_value, "meari")
        self.assertFalse(camera.extra_state_attributes["stream_supported"])
        coordinator.devices["camera"] = {**device, "camera_type": 8}
        self.assertEqual(camera.native_value, "meari")
        self.assertTrue(camera.extra_state_attributes["stream_supported"])
        self.assertTrue(sdk_entities["nvr"].extra_state_attributes["stream_supported"])
        self.assertEqual(camera.extra_state_attributes["camera_type"], 8)
        coordinator.devices["camera"] = {**device, "camera_type": None}
        self.assertEqual(camera.native_value, "unknown")
        self.assertFalse(camera.extra_state_attributes["stream_supported"])
        coordinator.devices.pop("camera")
        self.assertFalse(camera.available)

    def test_camera_sdk_sensor_respects_selection(self):
        coordinator = _Coordinator(self.module)
        coordinator.devices = {"camera": {"device_id": "camera", "device_type_raw": 14}}
        hass = SimpleNamespace(data={"orvibo_smart_control": {"entry-1": coordinator}})
        entry = SimpleNamespace(entry_id="entry-1", options={})
        entities = []
        with patch.object(self.module, "selected_device_ids", return_value=set()):
            asyncio.run(self.module.async_setup_entry(hass, entry, entities.extend))
        self.assertEqual(entities, [])

    def test_setup_adds_lock_sensors_for_p20_raw_type(self):
        coordinator = _Coordinator(self.module)
        coordinator.devices = {
            "lock-1": {
                "device_id": "lock-1",
                "device_name": "P20",
                "device_type": "unknown",
                "device_type_raw": 107,
                "uid": "lock-uid",
            }
        }
        hass = SimpleNamespace(
            data={"orvibo_smart_control": {"entry-1": coordinator}}
        )
        entry = SimpleNamespace(entry_id="entry-1", options={})
        entities = []

        asyncio.run(
            self.module.async_setup_entry(hass, entry, entities.extend)
        )

        expected_types = (
            self.module.OrviboDoorLockDryBatterySensor,
            self.module.OrviboDoorLockLithiumBatterySensor,
            self.module.OrviboDoorLockStateSensor,
            self.module.OrviboDoorLockUnlockSensor,
        )
        created_types = {type(entity) for entity in entities}
        self.assertTrue(set(expected_types).issubset(created_types))

    def test_lock_sensors_stay_available_when_online_flag_is_false(self):
        coordinator = _Coordinator(self.module)
        coordinator.devices = {
            "lock-1": {
                "device_id": "lock-1",
                "device_name": "P20",
                "device_type": "unknown",
                "device_type_raw": 107,
                "uid": "lock-uid",
            }
        }
        coordinator.get_device_state = lambda _device_id: {
            "online": False,
            "dry_battery_level": 86,
            "lithium_battery_level": 92,
            "lock_status": "locked",
        }
        hass = SimpleNamespace(
            data={"orvibo_smart_control": {"entry-1": coordinator}}
        )
        entry = SimpleNamespace(entry_id="entry-1", options={})
        entities = []

        asyncio.run(
            self.module.async_setup_entry(hass, entry, entities.extend)
        )

        lock_entities = [
            entity
            for entity in entities
            if isinstance(entity, self.module.OrviboDoorLockSensorBase)
        ]
        self.assertEqual(len(lock_entities), 4)
        self.assertTrue(all(entity.available for entity in lock_entities))
        self.assertEqual(
            next(
                entity
                for entity in lock_entities
                if isinstance(entity, self.module.OrviboDoorLockDryBatterySensor)
            ).native_value,
            86,
        )
        self.assertEqual(
            next(
                entity
                for entity in lock_entities
                if isinstance(entity, self.module.OrviboDoorLockStateSensor)
            ).native_value,
            "locked",
        )


    def test_lock_battery_visibility_tracks_valid_source_without_changing_user_choice(self):
        coordinator = _Coordinator(self.module)
        state = {"dry_battery_level": 60, "lithium_battery_level": None}
        coordinator.get_device_state = lambda _: state
        entries = {"sensor.dry": SimpleNamespace(hidden_by=None),
                   "sensor.lithium": SimpleNamespace(hidden_by=None)}
        updates = []

        def update(entity_id, **values):
            updates.append((entity_id, values))
            entries[entity_id].hidden_by = values["hidden_by"]

        registry = SimpleNamespace(async_get=entries.get, async_update_entity=update)
        device = {"device_id": "lock"}
        dry = self.module.OrviboDoorLockDryBatterySensor(coordinator, device)
        lithium = self.module.OrviboDoorLockLithiumBatterySensor(coordinator, device)
        for entity, entity_id in ((dry, "sensor.dry"), (lithium, "sensor.lithium")):
            entity.hass = SimpleNamespace(registry=registry)
            entity.entity_id = entity_id
            asyncio.run(entity.async_added_to_hass())
        self.assertTrue(coordinator.added)
        self.assertEqual(dry.native_value, 60)
        self.assertEqual(updates, [("sensor.lithium", {"hidden_by": "integration"})])
        state["lithium_battery_level"] = 0
        lithium._handle_coordinator_update()
        self.assertTrue(coordinator.updated)
        self.assertEqual(updates[-1], ("sensor.lithium", {"hidden_by": None}))
        for level in (None, 55):
            entries["sensor.lithium"].hidden_by = "user"
            state["lithium_battery_level"] = level
            count = len(updates)
            lithium._sync_battery_visibility()
            self.assertEqual(len(updates), count)

    def test_type300_legacy_battery_restores_dry_sensor_visibility(self):
        coordinator = _Coordinator(self.module)
        device = {"device_id": "lock", "device_name": "Lock", "device_type": "unknown",
                  "device_type_raw": 300, "sub_device_type": 400, "online": False,
                  "model": "dec7d494f0454110805c0d5f7e7cba73",
                  "properties": {"battery_power": 60, "handle": "on"}}
        coordinator.devices = {"lock": device}
        package_name = "orvibo_lock_battery_sensor_regression"
        package = _module(package_name)
        package.__path__ = [str(MODULE_PATH.parent)]
        with patch.dict(sys.modules, {package_name: package}):
            parser = importlib.import_module(f"{package_name}.parsers.lock")
        state = {"online": False}
        parser.parse_door_lock(state, {"properties": {"battery_power": 60,
            "handle": "on", "reverse_lock": "off", "door_status": "off"}}).apply_to(state)
        coordinator.get_device_state = lambda _: state
        entities = []
        hass = SimpleNamespace(data={"orvibo_smart_control": {"entry-1": coordinator}})
        asyncio.run(self.module.async_setup_entry(hass,
            SimpleNamespace(entry_id="entry-1", options={}), entities.extend))
        dry = next(entity for entity in entities
                   if isinstance(entity, self.module.OrviboDoorLockDryBatterySensor))
        lithium = next(entity for entity in entities
                       if isinstance(entity, self.module.OrviboDoorLockLithiumBatterySensor))
        entries = {"sensor.dry": SimpleNamespace(hidden_by="integration"),
                   "sensor.lithium": SimpleNamespace(hidden_by="integration")}

        def update(entity_id, **values):
            entries[entity_id].hidden_by = values["hidden_by"]

        registry = SimpleNamespace(async_get=entries.get, async_update_entity=update)
        for entity, entity_id in ((dry, "sensor.dry"), (lithium, "sensor.lithium")):
            entity.hass = SimpleNamespace(registry=registry)
            entity.entity_id = entity_id
            asyncio.run(entity.async_added_to_hass())
        self.assertEqual(dry.native_value, 60)
        self.assertTrue(dry.available)
        self.assertIsNone(entries["sensor.dry"].hidden_by)
        self.assertIsNone(lithium.native_value)
        self.assertEqual(entries["sensor.lithium"].hidden_by, "integration")

    def test_lock_battery_sensor_rejects_invalid_and_removed_sources(self):
        coordinator = _Coordinator(self.module)
        state = {}
        coordinator.get_device_state = lambda _: state
        for sensor_type, prefix in ((self.module.OrviboDoorLockDryBatterySensor, "dry"),
                                    (self.module.OrviboDoorLockLithiumBatterySensor, "lithium")):
            sensor = sensor_type(coordinator, {"device_id": "lock"})
            for invalid in (-1, 101, True, "bad", float("nan")):
                state.clear()
                state[f"{prefix}_battery_level"] = invalid
                self.assertIsNone(sensor.native_value)
            state[f"{prefix}_battery_level"] = 60
            state[f"{prefix}_battery_setup"] = False
            self.assertIsNone(sensor.native_value)


if __name__ == "__main__":
    unittest.main()
