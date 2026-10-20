"""Offline camera inventory regressions based on the APK's static SDK routes."""

from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"
PACKAGE_NAME = "orvibo_smart_control_camera_metadata_test"
package = types.ModuleType(PACKAGE_NAME)
package.__path__ = [str(COMPONENT_PATH)]
sys.modules[PACKAGE_NAME] = package
protocol = importlib.import_module(f"{PACKAGE_NAME}.protocol")
metadata = importlib.import_module(f"{PACKAGE_NAME}.camera_metadata")
capabilities = importlib.import_module(f"{PACKAGE_NAME}.capabilities")
device_types = importlib.import_module(f"{PACKAGE_NAME}.device_types")
inventory_module = importlib.import_module(f"{PACKAGE_NAME}.device_inventory")
selection = importlib.import_module(f"{PACKAGE_NAME}.device_selection")
https_module = importlib.import_module(f"{PACKAGE_NAME}.https_client")
redact = importlib.import_module(f"{PACKAGE_NAME}.redact")


def camera_row(device_id="camera-device", uid="camera-hardware", device_type=14):
    return {"deviceId": device_id, "uid": uid, "deviceType": device_type,
            "deviceName": "Camera", "online": 0}


class CameraInventoryTests(unittest.TestCase):
    def test_readtable_joins_hardware_uid_without_confusing_type_fields(self):
        data = {
            "device": [camera_row(), camera_row("nvr-device", "nvr-hardware", 162)],
            "cameraInfo": [
                {"uid": "camera-hardware", "type": "8", "productType": 1,
                 "accessToken": "secret-token", "tokenSecret": "secret-key"},
                {"uid": "camera-device", "type": 3},
            ],
        }
        before = copy.deepcopy(data)
        devices = {d.uid: protocol.device_to_dict(d) for d in
                   protocol.parse_readtable_devices({"data": data})}
        camera = devices["camera-device"]
        self.assertEqual(camera["device_type_raw"], 14)
        self.assertEqual(camera["device_type"], "sensor")
        self.assertEqual(camera["uid"], "camera-hardware")
        self.assertEqual(camera["camera_type"], 8)
        self.assertEqual(metadata.camera_sdk_hint(camera), "meari")
        self.assertEqual(metadata.camera_sdk_hint(devices["nvr-device"]), "meari")
        self.assertNotIn("secret-token", repr(devices))
        self.assertNotIn("secret-key", repr(devices))
        self.assertEqual(data, before)

    def test_missing_uid_does_not_join_business_device_id(self):
        row = camera_row()
        row.pop("uid")
        devices = protocol.parse_readtable_devices({"data": {
            "device": [row], "cameraInfo": [{"uid": "camera-device", "type": 8}]
        }})
        self.assertEqual(len(devices), 1)
        self.assertIsNone(devices[0].camera_type)

    def test_keyed_camera_table_and_deleted_rows(self):
        self.assertEqual(protocol.parse_camera_types({
            "hardware-a": {"type": "-1"},
            "hardware-b": {"uid": "hardware-c", "type": 9},
            "deleted": {"type": 4, "delFlag": "1"},
            "bad": "invalid",
        }), {"hardware-a": -1, "hardware-c": 9, "deleted": None})
        self.assertEqual(protocol.parse_camera_types([
            {"uid": "hardware-a", "type": 8},
            {"uid": "hardware-a", "type": 8, "delFlag": 1},
        ]), {"hardware-a": None})

    def test_malformed_camera_types_never_select_an_sdk(self):
        for value in (True, False, 8.5, float("inf"), "8.0", "1e1", {}, [], None,
                      "", "999999999999999999999999999999"):
            with self.subTest(value=value):
                parsed = protocol.parse_camera_types([{"uid": "hardware-a", "type": value}])
                self.assertEqual(parsed, {"hardware-a": None})
        for raw in (None, True, "invalid", 123):
            self.assertEqual(protocol.parse_camera_types(raw), {})
        self.assertEqual(protocol.parse_camera_types([
            None, 5, {"type": 8}, {"uid": [], "type": 8},
            {"deviceId": "camera-device", "type": 8},
        ]), {})

    def test_binary_camera_tables_do_not_create_phantom_devices(self):
        for payload in (
            {"device": [camera_row()], "cameraInfo": [{"uid": "camera-hardware", "type": 4}]},
            {"cmd": 147, "tableNameList": [
                {"tableName": " cameraInfo ", "dataList": [{"uid": "camera-hardware", "type": 4}]},
                {"tableName": "device", "dataList": [camera_row()]},
            ]},
        ):
            with self.subTest(payload=payload):
                devices = protocol.extract_devices(payload)
                self.assertEqual([d.uid for d in devices], ["camera-device"])
                self.assertEqual(devices[0].camera_type, 4)
        self.assertEqual(protocol.extract_devices({
            "cameraInfo": [{"uid": "camera-hardware", "type": 4}]
        }), ())

    def test_https_list_and_dict_formats_agree_and_filter_deleted_devices(self):
        client = https_module.HttpsClient("offline", "0" * 32)
        for raw_devices in ([camera_row()], {"camera-device": camera_row()}):
            with self.subTest(raw_devices=raw_devices):
                devices = client.parse_device_status_list({
                    "device": raw_devices,
                    "cameraInfo": {"camera-hardware": {"type": 3}},
                })
                self.assertEqual(len(devices), 1)
                self.assertEqual(devices[0]["camera_type"], 3)
                self.assertEqual(devices[0]["device_type"], "sensor")
                self.assertEqual(devices[0]["device_type_raw"], 14)
        row = {**camera_row(), "classId": 436}
        self.assertEqual(client._get_device_type(row), "sensor")
        self.assertEqual(client.parse_device_status_list({
            "device": {"camera-device": {**row, "delFlag": 1}},
        }), [])

    def test_cameras_are_visible_but_never_have_device_control_channels(self):
        for device_type in (14, 162):
            device = {"device_id": "camera", "device_type_raw": device_type}
            with self.subTest(device_type=device_type):
                profile = device_types.get_device_profile(device)
                self.assertFalse(device_types.is_hidden_category(profile.category))
                self.assertTrue(profile.registration_only)
                self.assertFalse(profile.hardware_verified)
                self.assertEqual(profile.info.capabilities, ())
                cap = capabilities.capability_for(device)
                expected_platforms = {"sensor", "camera"} if device_type == 162 else {"sensor"}
                self.assertEqual(cap.platforms, frozenset(expected_platforms))
                self.assertEqual(cap.channels, frozenset())
                self.assertFalse(cap.controllable)
                self.assertTrue(cap.status_only)
                self.assertTrue(cap.cloud_only)
                self.assertFalse(capabilities.lan_state_allowed(device))
                self.assertEqual(capabilities.transport_path_for(device), capabilities.TransportPath.CLOUD)
                self.assertEqual(capabilities.transport_path_for(device, capabilities.TransportMode.LAN_ONLY),
                                 capabilities.TransportPath.UNAVAILABLE)
                self.assertEqual(selection.infer_device_group(device), ("cameras", "摄像头"))

    def test_camera_type_takes_precedence_over_name_keywords(self):
        for device_type in (14, 162):
            self.assertEqual(selection.infer_device_group({
                "device_id": "camera", "device_type_raw": device_type,
                "device_name": "客厅灯旁的监控", "device_type": "sensor",
            }), ("cameras", "摄像头"))

    def test_inventory_keeps_camera_and_nvr_and_refreshes_metadata(self):
        devices, states = {}, {}
        inventory = inventory_module.DeviceInventory(None, devices, states,
            inventory_module.StateStore(states), lambda _id: None)
        inventory.initialize([
            {"device_id": "camera", "device_type_raw": 14, "camera_type": 4},
            {"device_id": "nvr", "device_type_raw": 162},
        ])
        self.assertEqual(set(devices), {"camera", "nvr"})
        inventory.merge_cloud([{"device_id": "camera", "device_type_raw": 14, "camera_type": 8}])
        self.assertEqual(metadata.camera_sdk_hint(devices["camera"]), "meari")

    def test_unknown_camera_sdk_does_not_create_stream_entities(self):
        from types import SimpleNamespace
        from test_camera_live import load_camera_module
        import asyncio
        camera = load_camera_module()
        coordinator = SimpleNamespace(devices={
            "camera": {"device_id": "camera", "device_type_raw": 14},
        })
        hass = SimpleNamespace(data={"orvibo_smart_control": {"entry": coordinator}})
        entities = []
        asyncio.run(camera.async_setup_entry(hass,
            SimpleNamespace(entry_id="entry", options={}), entities.extend))
        self.assertEqual(entities, [])


class CameraSdkHintTests(unittest.TestCase):
    def test_stream_support_matches_implemented_camera_platform_route(self):
        examples = (
            ({"device_type_raw": 162}, True),
            *(({"device_type_raw": 14, "camera_type": value}, True) for value in (8, 9, 10)),
            *(({"device_type_raw": 14, "camera_type": value}, False)
              for value in (None, -1, 0, 1, 2, 3, 7, 11, 99, True, "8")),
        )
        for device, supported in examples:
            with self.subTest(device=device):
                device = {**device, "online": False}
                self.assertEqual(metadata.camera_stream_supported(device), supported)
                self.assertEqual(metadata.camera_diagnostics(device)["stream_supported"], supported)
                self.assertEqual("camera" in capabilities.capability_for(device).platforms, supported)
                self.assertFalse(metadata.camera_diagnostics(device)["hardware_verified"])
                self.assertFalse(capabilities.capability_for(device).controllable)
        for device in (None, 162, {}, {"device_type_raw": 999999, "camera_type": 8}):
            self.assertFalse(metadata.camera_stream_supported(device))

    def test_type_based_routes_cover_all_observed_values(self):
        for types_, expected in ((range(3, 8), "danale"), (range(8, 11), "meari"),
                                 ((-1, 1, 2), "ezviz")):
            for camera_type in types_:
                with self.subTest(camera_type=camera_type):
                    self.assertEqual(metadata.camera_sdk_hint({
                        "device_type_raw": 14, "camera_type": camera_type,
                    }), expected)

    def test_unknown_legacy_and_catalogue_types_are_not_guessed(self):
        for camera_type in (None, 0, 11, 99, True, "8"):
            self.assertEqual(metadata.camera_sdk_hint({
                "device_type_raw": 14, "camera_type": camera_type,
                "ext_addr": "legacy-did", "productType": 8,
            }), "unknown")
        for device_type in (521, 522, 107, 1):
            device = {"device_type_raw": device_type, "camera_type": 8}
            self.assertEqual(metadata.camera_sdk_hint(device), "unknown")
            self.assertEqual(metadata.camera_diagnostics(device), {})

    def test_diagnostics_export_only_public_camera_fields(self):
        device = {"device_type_raw": 14, "camera_type": 8, "uid": "private-uid",
                  "tokenSecret": "secret", "password": "secret", "ext_addr": "private-did"}
        self.assertEqual(metadata.camera_diagnostics(device), {
            "camera_type": 8, "camera_sdk_hint": "meari",
            "sdk_hint_source": "camera_info_type", "stream_supported": True,
            "hardware_verified": False,
        })
        self.assertEqual(metadata.camera_diagnostics({"device_type_raw": 14})["sdk_hint_source"], "unknown")
        self.assertEqual(metadata.camera_diagnostics({"device_type_raw": 162})["sdk_hint_source"], "device_type")

    def test_sdk_token_secret_and_legacy_did_are_redacted(self):
        packet = {"cameraInfo": [{"uid": "camera-hardware", "type": 8,
                                  "tokenSecret": "private-secret"}],
                  "ext_addr": "legacy-camera-did"}
        result = redact.redact_packet(packet, b"camera-test-salt", strict=False)
        self.assertEqual(result["cameraInfo"][0]["type"], 8)
        self.assertEqual(result["cameraInfo"][0]["tokenSecret"], "<redacted>")
        self.assertTrue(result["ext_addr"].startswith("<id:"))
        self.assertNotIn("private-secret", repr(result))
        self.assertNotIn("legacy-camera-did", repr(result))
        self.assertEqual(packet["ext_addr"], "legacy-camera-did")

    def test_config_diagnostics_include_camera_hint(self):
        modules = {}
        for name, attr in (("homeassistant.config_entries", "ConfigEntry"),
                           ("homeassistant.core", "HomeAssistant"),
                           ("homeassistant.helpers.device_registry", "DeviceEntry")):
            module = types.ModuleType(name)
            setattr(module, attr, object)
            modules[name] = module
        with patch.dict(sys.modules, modules):
            diagnostics = importlib.import_module(f"{PACKAGE_NAME}.diagnostics")
        import asyncio
        from types import SimpleNamespace
        coordinator = SimpleNamespace(
            devices={"camera-device": {"device_type_raw": 14, "camera_type": 4}},
            device_states={}, _cmd42_log=[],
        )
        hass = SimpleNamespace(data={"orvibo_smart_control": {"entry": coordinator}})
        result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(
            hass, SimpleNamespace(entry_id="entry")))
        camera = next(iter(result["devices"].values()))
        self.assertEqual(camera["camera_sdk_hint"], "danale")
        self.assertFalse(camera["stream_supported"])

    def test_enum_translations_match_all_sdk_options(self):
        for path in ("strings.json", "translations/en.json", "translations/zh-Hans.json"):
            with self.subTest(path=path):
                data = json.loads((COMPONENT_PATH / path).read_text(encoding="utf-8"))
                self.assertEqual(set(data["entity"]["sensor"]["camera_sdk"]["state"]),
                                 set(metadata.CAMERA_SDK_OPTIONS))


if __name__ == "__main__":
    unittest.main()
