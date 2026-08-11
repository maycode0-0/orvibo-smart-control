"""Tests for optional availability and release notifications."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import AsyncMock

MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibo_smart_control"
    / "runtime_options.py"
)


def _module(name: str, **values) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _load_runtime_options():
    package_name = "orvibo_smart_control_runtime_options_test"
    package = _module(package_name)
    package.__path__ = [str(MODULE_PATH.parent)]
    modules = {
        package_name: package,
        "homeassistant": _module("homeassistant"),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.aiohttp_client": _module(
            "homeassistant.helpers.aiohttp_client",
            async_get_clientsession=lambda hass: None,
        ),
    }
    module_name = f"{package_name}.runtime_options"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    from unittest.mock import patch

    with patch.dict(sys.modules, modules):
        sys.modules[module_name] = result
        spec.loader.exec_module(result)
    return result


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, bool]] = []

    async def async_call(self, domain, service, data, *, blocking=False):
        self.calls.append((domain, service, data, blocking))


class FakeHass:
    def __init__(self) -> None:
        self.services = FakeServices()
        self.tasks: list[asyncio.Task] = []

    def async_create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self, **kwargs):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return FakeResponse(self.payload)


class RuntimeOptionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_runtime_options()

    def test_stable_version_parser_rejects_prereleases(self):
        self.assertEqual(self.module.version_tuple("v1.2.3"), (1, 2, 3))
        self.assertIsNone(self.module.version_tuple("v1.2.3b1"))

    async def test_availability_notifier_only_reports_transitions(self):
        hass = FakeHass()
        states = {"device": {"online": True}}
        notifier = self.module.AvailabilityNotifier(
            hass,
            "entry",
            {"device": {"device_name": "客厅灯"}},
            states,
            {"device"},
        )

        notifier.process()
        self.assertFalse(hass.tasks)

        states["device"]["online"] = False
        notifier.process()
        await asyncio.gather(*hass.tasks)

        self.assertEqual(len(hass.services.calls), 1)
        domain, service, data, blocking = hass.services.calls[0]
        self.assertEqual((domain, service), ("persistent_notification", "create"))
        self.assertIn("客厅灯", data["message"])
        self.assertFalse(blocking)

    async def test_update_checker_notifies_for_newer_stable_release(self):
        hass = FakeHass()
        checker = self.module.IntegrationUpdateChecker(
            hass,
            interval_hours=24,
            current_version="0.1.0",
            session=FakeSession({"tag_name": "v0.2.0"}),
        )

        latest = await checker.check_once()

        self.assertEqual(latest, "v0.2.0")
        self.assertEqual(len(hass.services.calls), 1)
        self.assertEqual(
            hass.services.calls[0][:2],
            ("persistent_notification", "create"),
        )

    async def test_update_checker_ignores_same_version(self):
        hass = FakeHass()
        checker = self.module.IntegrationUpdateChecker(
            hass,
            interval_hours=24,
            current_version="0.1.0",
            session=FakeSession({"tag_name": "v0.1.0"}),
        )

        self.assertIsNone(await checker.check_once())
        self.assertFalse(hass.services.calls)


if __name__ == "__main__":
    unittest.main()
