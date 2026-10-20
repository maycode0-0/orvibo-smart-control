"""Vectors for the HomeMate APK-derived protocol profile layer."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"


def load(name: str):
    package = "orvibo_smart_control_profiles_test"
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = [str(ROOT)]
        sys.modules[package] = module
    return importlib.import_module(f"{package}.{name}")


profiles = load("protocol_profiles")


class ProtocolProfileTests(unittest.TestCase):
    def test_native_dimmer_scales_match_reverse_engineered_paths(self):
        normal = {"device_type_raw": 0}
        type_116 = {"device_type_raw": 116}
        virtual = {"device_type_raw": 0, "model": profiles.VIRTUAL_MIXPAD_DIMMER_MODEL}

        self.assertEqual(profiles.light_wire_brightness(0, normal), 2)
        self.assertEqual(profiles.light_wire_brightness(50, normal), 129)
        self.assertEqual(profiles.light_wire_brightness(100, normal), 255)
        self.assertEqual(profiles.light_wire_brightness(50, type_116), 60)
        self.assertEqual(profiles.light_wire_brightness(100, type_116), 120)
        self.assertEqual(profiles.light_wire_brightness(50, virtual), 128)

        self.assertEqual(profiles.light_percent_from_wire(129, normal), 50)
        self.assertEqual(profiles.light_percent_from_wire(2, normal), 1)
        self.assertEqual(profiles.light_percent_from_wire(60, type_116), 50)
        self.assertEqual(profiles.light_percent_from_wire(0, type_116), 0)
        self.assertEqual(profiles.light_percent_from_wire(128, virtual), 50)
        self.assertEqual(profiles.light_percent_from_wire(0, virtual), 1)
        self.assertEqual(profiles.light_percent_from_wire(1, normal), 1)

    def test_native_decode_integer_rounding_boundary(self):
        self.assertEqual(profiles.light_percent_from_wire(21, {"device_type_raw": 0}), 8)
        for value in (-0.5, float("inf"), float("nan"), True, "unknown"):
            with self.subTest(value=value):
                self.assertIsNone(profiles.light_percent_from_wire(value, {"device_type_raw": 0}))

    def test_unknown_virtual_model_does_not_enable_native_control(self):
        self.assertIsNone(profiles.profile_for({
            "device_type_raw": 999, "model": profiles.VIRTUAL_MIXPAD_DIMMER_MODEL,
        }).dimmer_scale)

    def test_rf_curtain_preserves_native_order_and_values(self):
        device = {"device_type_raw": 34, "is_rf_curtain": True}
        open_command = profiles.curtain_command("open", device)
        self.assertEqual((open_command.order, open_command.value1), ("rf control", 380000))
        stop = profiles.curtain_command("stop", device)
        self.assertEqual((stop.order, stop.value1), ("rf control", 380002))
        self.assertFalse(profiles.profile_for({"device_type_raw": 34}).rf_curtain)


if __name__ == "__main__":
    unittest.main()
