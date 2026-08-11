"""Tests for grouped device selection used by both config-flow screens."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType

MODULE_PATH = Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"
_package = sys.modules.setdefault(
    "orvibo_smart_control", ModuleType("orvibo_smart_control")
)
_package.__path__ = [str(MODULE_PATH)]


def _import_module(name: str):
    module_name = f"orvibo_smart_control.{name}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    module_path = MODULE_PATH / f"{name}.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


selection = _import_module("selection")
device_selection = _import_module("device_selection")


DEVICES = [
    {"device_id": "light-1", "device_type_raw": 38, "device_name": "客厅灯"},
    {"device_id": "light-2", "device_type_raw": 501, "subDeviceType": 426, "device_name": "卧室灯"},
    {"device_id": "light-3", "device_type_raw": 502, "subDeviceType": 431, "device_name": "书房灯"},
    {"device_id": "switch-1", "device_type_raw": 135, "device_name": "玄关开关"},
    {"device_id": "curtain-1", "device_type_raw": 34, "device_name": "客厅窗帘"},
    {"device_id": "lock-1", "device_type_raw": 522, "subDeviceType": 463, "device_name": "入户门锁"},
    {"device_id": "unknown-1", "device_type_raw": 999, "device_name": "未知设备"},
]


class TestGroupedDeviceSelection(unittest.TestCase):
    def test_name_classifies_device_without_type_metadata(self):
        group = device_selection.infer_device_group(
            {"device_id": "named-light", "device_name": "楼梯感应灯"}
        )
        self.assertEqual(group, ("lights", "灯具"))

    def test_groups_keep_api_order_and_classify_devices(self):
        grouped = device_selection.grouped_devices(DEVICES)
        self.assertEqual(
            [device["device_id"] for device in grouped["lights"]],
            ["light-1", "light-2", "light-3"],
        )
        self.assertEqual(
            [device["device_id"] for device in grouped["other"]],
            ["unknown-1"],
        )

    def test_single_device_type_remains_its_own_group(self):
        groups = device_selection.device_selection_groups(DEVICES)
        switch_group = next(group for group in groups if group.key == "switches")
        self.assertEqual(switch_group.label, "开关")
        self.assertEqual(switch_group.device_ids, ("switch-1",))

    def test_all_checkbox_selects_every_device_in_category(self):
        light_group = next(
            group for group in device_selection.device_selection_groups(DEVICES)
            if group.key == "lights"
        )
        result = device_selection.merge_grouped_selection(
            {light_group.all_field: True}, DEVICES
        )
        self.assertEqual(result, ["light-1", "light-2", "light-3"])

    def test_select_all_option_selects_every_device_in_category(self):
        light_group = next(
            group for group in device_selection.device_selection_groups(DEVICES)
            if group.key == "lights"
        )
        result = device_selection.merge_grouped_selection(
            {light_group.device_field: [light_group.all_value]}, DEVICES
        )
        self.assertEqual(result, ["light-1", "light-2", "light-3"])

    def test_legacy_all_checkbox_field_remains_supported(self):
        light_group = next(
            group for group in device_selection.device_selection_groups(DEVICES)
            if group.key == "lights"
        )
        result = device_selection.merge_grouped_selection(
            {light_group.legacy_all_field: True}, DEVICES
        )
        self.assertEqual(result, ["light-1", "light-2", "light-3"])

    def test_individual_selection_can_mix_categories(self):
        groups = device_selection.device_selection_groups(DEVICES)
        lights = next(group for group in groups if group.key == "lights")
        switches = next(group for group in groups if group.key == "switches")
        result = device_selection.merge_grouped_selection(
            {
                lights.device_field: ["light-2"],
                switches.device_field: ["switch-1"],
            },
            DEVICES,
        )
        self.assertEqual(result, ["light-2", "switch-1"])

    def test_unknown_ids_are_removed_and_order_is_stable(self):
        lights = next(
            group for group in device_selection.device_selection_groups(DEVICES)
            if group.key == "lights"
        )
        result = device_selection.merge_grouped_selection(
            {lights.device_field: ["missing", "light-1"]}, DEVICES
        )
        self.assertEqual(result, ["light-1"])

    def test_legacy_flat_input_remains_supported(self):
        result = device_selection.merge_grouped_selection(
            {
                selection.CONF_SELECTED_DEVICE_IDS: [
                    "lock-1", "light-1", "not-available"
                ]
            },
            DEVICES,
        )
        self.assertEqual(result, ["light-1", "lock-1"])


if __name__ == "__main__":
    unittest.main()
