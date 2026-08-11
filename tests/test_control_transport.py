"""Tests for LAN/cloud control transport selection."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest

COMPONENT_PATH = Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"


def _load_module():
    package_name = "orvibo_smart_control_transport_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT_PATH)]
    sys.modules[package_name] = package
    return importlib.import_module(f"{package_name}.control_executor")


class FakeSsl:
    def __init__(self) -> None:
        self.calls = []

    async def send_control_light_colortemp(self, *args, **kwargs):
        self.calls.append(("ssl_light_colortemp", args, kwargs))
        return True

    async def send_control_switch(self, *args, **kwargs):
        self.calls.append(("ssl_switch", args, kwargs))
        return True

    async def send_control_light(self, *args, **kwargs):
        self.calls.append(("ssl_light", args, kwargs))
        return True

    async def send_control_cover(self, *args, **kwargs):
        self.calls.append(("ssl_cover", args, kwargs))
        return True

    async def send_light_bri_ct(self, *args, **kwargs):
        self.calls.append(("ssl_light_bri_ct", args, kwargs))
        return True

    async def _wait_for_control_response(self, device_id):
        del device_id
        return None


class FakeLan:
    def __init__(self) -> None:
        self.calls = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return True

        return method


class FailingLan(FakeLan):
    async def send_control_light_colortemp(self, *args, **kwargs):
        self.calls.append(("lan_light_colortemp", args, kwargs))
        return False


class FailingCoverLan(FakeLan):
    async def send_control_cover(self, *args, **kwargs):
        self.calls.append(("lan_cover", args, kwargs))
        return False


class ControlTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def make_executor(self, device, lan=None, gateway_connected=True, mode="auto"):
        devices = {"device": device}
        states = {"device": {}}
        ssl = FakeSsl()
        store = self.module.StateStore(states)
        executor = self.module.ControlExecutor(
            devices,
            states,
            store,
            lambda: ssl,
            lambda: object(),
            states.get,
            lambda: None,
            lambda: lan,
            lambda _uid: gateway_connected,
            self.module.TransportMode(mode),
        )
        return executor, ssl

    def test_lan_preferred_when_gateway_connected(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(device, lan=lan)

        ok = asyncio.run(executor.turn_on("device"))
        self.assertTrue(ok)
        self.assertTrue(lan.calls)
        self.assertFalse(ssl.calls)
        self.assertEqual(executor.last_transport("device"), "lan")

    def test_ssl_fallback_when_gateway_disconnected(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(device, lan=lan, gateway_connected=False)

        ok = asyncio.run(executor.turn_on("device"))
        self.assertTrue(ok)
        self.assertFalse(lan.calls)
        self.assertTrue(ssl.calls)
        self.assertEqual(executor.last_transport("device"), "cloud")

    def test_cloud_only_mode_forces_ssl(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(
            device, lan=lan, gateway_connected=True, mode="cloud_only"
        )

        ok = asyncio.run(executor.turn_on("device"))
        self.assertTrue(ok)
        self.assertFalse(lan.calls)
        self.assertTrue(ssl.calls)

    def test_cloud_only_lock_never_uses_lan(self) -> None:
        device = {"device_type_raw": 522, "sub_device_type": 463, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(device, lan=lan, gateway_connected=True)

        ok = asyncio.run(executor.turn_on("device"))
        self.assertTrue(ok)
        self.assertFalse(lan.calls)
        self.assertTrue(ssl.calls)

    def test_lan_failure_falls_back_to_ssl(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FailingLan()
        executor, ssl = self.make_executor(device, lan=lan, gateway_connected=True)

        ok = asyncio.run(executor.turn_on("device"))
        self.assertTrue(ok)
        self.assertTrue(lan.calls)
        self.assertTrue(ssl.calls)
        self.assertEqual(executor.last_transport("device"), "cloud")

    def test_lan_only_never_falls_back_when_gateway_is_disconnected(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(
            device,
            lan=lan,
            gateway_connected=False,
            mode="lan_only",
        )

        ok = asyncio.run(executor.turn_on("device"))
        self.assertFalse(ok)
        self.assertFalse(lan.calls)
        self.assertFalse(ssl.calls)

    def test_lan_only_never_falls_back_after_lan_failure(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FailingLan()
        executor, ssl = self.make_executor(
            device,
            lan=lan,
            gateway_connected=True,
            mode="lan_only",
        )

        ok = asyncio.run(executor.turn_on("device"))
        self.assertFalse(ok)
        self.assertTrue(lan.calls)
        self.assertFalse(ssl.calls)
        self.assertIsNone(executor.last_transport("device"))

    def test_direct_cover_path_falls_back_to_ssl(self) -> None:
        device = {"device_type_raw": 34, "uid": "gw-1"}
        lan = FailingCoverLan()
        executor, ssl = self.make_executor(device, lan=lan, gateway_connected=True)

        ok = asyncio.run(executor.set_cover_position("device", 60))
        self.assertTrue(ok)
        self.assertTrue(any(call[0] == "lan_cover" for call in lan.calls))
        self.assertTrue(any(call[0] == "ssl_cover" for call in ssl.calls))

    def test_compound_light_control_returns_transport_result(self) -> None:
        device = {"device_type_raw": 38, "uid": "gw-1"}
        lan = FakeLan()
        executor, ssl = self.make_executor(device, lan=lan, gateway_connected=True)

        ok = asyncio.run(executor.set_light_param("device", 128, 3500))
        self.assertTrue(ok)
        self.assertTrue(any(call[0] == "send_light_bri_ct" for call in lan.calls))
        self.assertFalse(ssl.calls)


if __name__ == "__main__":
    unittest.main()
