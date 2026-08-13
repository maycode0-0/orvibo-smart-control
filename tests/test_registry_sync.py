"""Tests for keeping Home Assistant registries aligned with device selection."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


COMPONENT_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibo_smart_control"
)


def _module(name: str, **values) -> ModuleType:
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    return module


def _load_module():
    package_name = "orvibo_smart_control_registry_sync_test"
    package = _module(package_name)
    package.__path__ = [str(COMPONENT_PATH)]

    device_registry = _module(
        "homeassistant.helpers.device_registry",
        async_get=lambda hass: hass.device_registry,
        async_entries_for_config_entry=lambda registry, entry_id: [
            device
            for device in registry.devices
            if entry_id in device.config_entries
        ],
    )
    entity_registry = _module(
        "homeassistant.helpers.entity_registry",
        async_get=lambda hass: hass.entity_registry,
        async_entries_for_config_entry=lambda registry, entry_id: [
            entity
            for entity in registry.entities
            if entity.config_entry_id == entry_id
        ],
    )
    helpers = _module(
        "homeassistant.helpers",
        device_registry=device_registry,
        entity_registry=entity_registry,
    )
    modules = {
        package_name: package,
        "homeassistant": _module("homeassistant", helpers=helpers),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object
        ),
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.entity_registry": entity_registry,
        f"{package_name}.const": _module(
            f"{package_name}.const", DOMAIN="orvibo_smart_control"
        ),
    }

    with patch.dict(sys.modules, modules):
        return importlib.import_module(f"{package_name}.registry_sync")


class RegistrySyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = _load_module()

    def _hass(self):
        selected_device = SimpleNamespace(
            id="registry-selected",
            identifiers={("orvibo_smart_control", "selected")},
            config_entries={"entry-1"},
        )
        hidden_device = SimpleNamespace(
            id="registry-hidden",
            identifiers={("orvibo_smart_control", "hidden")},
            config_entries={"entry-1"},
        )
        other_integration_device = SimpleNamespace(
            id="registry-other",
            identifiers={("other_domain", "other")},
            config_entries={"entry-1"},
        )
        entities = [
            SimpleNamespace(
                entity_id="light.selected",
                device_id="registry-selected",
                config_entry_id="entry-1",
            ),
            SimpleNamespace(
                entity_id="switch.hidden",
                device_id="registry-hidden",
                config_entry_id="entry-1",
            ),
            SimpleNamespace(
                entity_id="sensor.other",
                device_id="registry-other",
                config_entry_id="entry-1",
            ),
        ]
        return SimpleNamespace(
            device_registry=SimpleNamespace(
                devices=[
                    selected_device,
                    hidden_device,
                    other_integration_device,
                ],
                async_remove_device=MagicMock(),
                async_update_device=MagicMock(),
            ),
            entity_registry=SimpleNamespace(
                entities=entities,
                async_remove=MagicMock(),
            ),
        )

    def test_removes_deselected_entities_and_devices_only(self):
        hass = self._hass()
        entry = SimpleNamespace(
            entry_id="entry-1",
            options={
                "selected_device_ids": ["selected", "hidden"],
                "hidden_device_name_patterns": ["继电器"],
            },
        )
        devices = {
            "selected": {
                "device_id": "selected",
                "device_name": "客厅灯",
            },
            "hidden": {
                "device_id": "hidden",
                "device_name": "餐厅灯带继电器",
            },
        }

        removed = self.module.sync_selected_device_registries(
            hass, entry, devices
        )

        self.assertEqual(removed, 1)
        hass.entity_registry.async_remove.assert_called_once_with(
            "switch.hidden"
        )
        hass.device_registry.async_remove_device.assert_called_once_with(
            "registry-hidden"
        )
        hass.device_registry.async_update_device.assert_not_called()

    def test_shared_device_drops_only_this_config_entry(self):
        hass = self._hass()
        hidden = hass.device_registry.devices[1]
        hidden.config_entries.add("entry-2")
        entry = SimpleNamespace(
            entry_id="entry-1",
            options={"selected_device_ids": ["selected"]},
        )

        self.module.sync_selected_device_registries(
            hass,
            entry,
            {
                "selected": {"device_id": "selected"},
                "hidden": {"device_id": "hidden"},
            },
        )

        hass.device_registry.async_remove_device.assert_not_called()
        hass.device_registry.async_update_device.assert_called_once_with(
            "registry-hidden", remove_config_entry_id="entry-1"
        )


if __name__ == "__main__":
    unittest.main()
