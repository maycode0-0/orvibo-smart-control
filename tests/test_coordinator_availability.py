"""Regression tests for availability across quiet periods and light controls."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock

from tests.test_coordinator_transport import _FakeHass, _load_coordinator_module


class CoordinatorAvailabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.coordinator_module = _load_coordinator_module(
            "orvibo_smart_control_availability_coordinator_test"
        )
        package_name = "orvibo_smart_control_availability_runtime_test"
        package = types.ModuleType(package_name)
        package.__path__ = [str(
            Path(__file__).parents[1] / "custom_components" / "orvibo_smart_control"
        )]
        sys.modules[package_name] = package
        cls.inventory_module = importlib.import_module(f"{package_name}.device_inventory")
        cls.control_module = importlib.import_module(f"{package_name}.control_executor")
        cls.dispatcher_module = importlib.import_module(f"{package_name}.status_dispatcher")

    def make_coordinator(self, mode="auto"):
        module = self.coordinator_module
        coordinator = module.OrviboSmartControlCoordinator(
            _FakeHass(),
            module.AccountCredentials("test-user", "test-hash", "test-family"),
            transport_mode=module.TransportMode(mode),
        )
        coordinator.state_store = self.inventory_module.StateStore(coordinator.device_states)
        coordinator.inventory = self.inventory_module.DeviceInventory(
            coordinator.https_client,
            coordinator.devices,
            coordinator.device_states,
            coordinator.state_store,
            lambda _device_id: None,
        )
        coordinator.inventory.initialize([{
            "device_id": "lamp", "device_type_raw": 1,
            "uid": "test-gateway", "online": True,
        }])
        coordinator.device_states["lamp"].update(state=False, brightness=128)
        coordinator._last_update_time["lamp"] = time.time() - 3600
        return coordinator

    def test_quiet_device_keeps_reported_availability_when_read(self):
        for mode in ("auto", "cloud_only", "lan_only"):
            for online in (True, False):
                with self.subTest(mode=mode, online=online):
                    coordinator = self.make_coordinator(mode)
                    state = coordinator.device_states["lamp"]
                    state["online"] = online
                    expected = dict(state)

                    self.assertIs(coordinator.get_device_state("lamp"), state)
                    self.assertEqual(state, expected)
                    self.assertEqual(coordinator.get_device_state("lamp"), expected)

    def test_fresh_cloud_snapshot_is_not_invalidated_by_old_push_timestamp(self):
        coordinator = self.make_coordinator()
        coordinator.device_states["lamp"]["online"] = False
        coordinator.https_client.fetch_device_status = AsyncMock(return_value={"ok": True})
        coordinator.https_client.parse_device_status_list = lambda _data: [{
            "device_id": "lamp", "device_type_raw": 1, "online": True,
        }]

        asyncio.run(coordinator._async_update_data())

        self.assertTrue(coordinator.get_device_state("lamp")["online"])

    def test_light_controls_without_push_do_not_change_availability(self):
        for on in (True, False):
            with self.subTest(on=on):
                coordinator = self.make_coordinator("cloud_only")
                coordinator.device_states["lamp"]["state"] = not on
                ssl = types.SimpleNamespace(
                    send_control_light=AsyncMock(return_value=True),
                    _wait_for_control_response=AsyncMock(return_value=None),
                )
                coordinator.control = self.control_module.ControlExecutor(
                    coordinator.devices,
                    coordinator.device_states,
                    coordinator.state_store,
                    lambda: ssl,
                    lambda: coordinator,
                    coordinator.get_device_state,
                    lambda: coordinator.async_set_updated_data(coordinator.device_states),
                )
                action = coordinator.async_turn_on if on else coordinator.async_turn_off

                self.assertTrue(asyncio.run(action("lamp")))

                ssl.send_control_light.assert_awaited_once_with("lamp", "test-gateway", on)
                state = coordinator.get_device_state("lamp")
                self.assertEqual(state["state"], on)
                self.assertTrue(state["online"])
                self.assertEqual(state["brightness"], 128)
                self.assertIs(coordinator.data, coordinator.device_states)

    def test_explicit_cloud_offline_still_marks_device_unavailable(self):
        coordinator = self.make_coordinator()
        coordinator.inventory.merge_cloud([{
            "device_id": "lamp", "device_type_raw": 1, "online": False,
        }])

        self.assertFalse(coordinator.get_device_state("lamp")["online"])

    def test_device_report_restores_availability(self):
        coordinator = self.make_coordinator()
        coordinator.device_states["lamp"]["online"] = False
        dispatcher = self.dispatcher_module.StatusUpdateDispatcher(
            coordinator.devices,
            coordinator.device_states,
            coordinator.state_store,
            coordinator._last_update_time,
            [],
            on_motion=lambda *_args: None,
            on_emergency=lambda *_args: None,
            on_lock_transient=lambda *_args: None,
            on_lock_message=lambda *_args: None,
            on_lock_event=lambda *_args: None,
            on_updated=lambda: coordinator.async_set_updated_data(coordinator.device_states),
        )

        dispatcher.dispatch("lamp", {"value1": 0})

        self.assertTrue(coordinator.get_device_state("lamp")["online"])
        self.assertGreater(coordinator._last_update_time["lamp"], time.time() - 5)

    def test_missing_device_has_no_state(self):
        self.assertIsNone(self.make_coordinator().get_device_state("missing"))


if __name__ == "__main__":
    unittest.main()
