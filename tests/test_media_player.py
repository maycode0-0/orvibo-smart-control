"""Media-player entity coverage without requiring Home Assistant."""

from __future__ import annotations

import asyncio
from enum import IntFlag
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "orvibo_smart_control"
    / "media_player.py"
)


def _module(name: str, **values) -> ModuleType:
    module = ModuleType(name)
    module.__dict__.update(values)
    return module


def _load_media_player_module():
    package_name = "orvibo_smart_control_media_player_test"
    package = _module(package_name)
    package.__path__ = [str(MODULE_PATH.parent)]

    class CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    class MediaPlayerEntity:
        pass

    class MediaPlayerEntityFeature(IntFlag):
        PAUSE = 1
        PLAY = 2
        PREVIOUS_TRACK = 4
        NEXT_TRACK = 8
        VOLUME_SET = 16
        VOLUME_MUTE = 32
        SEEK = 64

    modules = {
        package_name: package,
        "homeassistant": _module("homeassistant"),
        "homeassistant.components": _module("homeassistant.components"),
        "homeassistant.components.media_player": _module(
            "homeassistant.components.media_player",
            MediaPlayerDeviceClass=SimpleNamespace(SPEAKER="speaker"),
            MediaPlayerEntity=MediaPlayerEntity,
            MediaPlayerEntityFeature=MediaPlayerEntityFeature,
        ),
        "homeassistant.config_entries": _module(
            "homeassistant.config_entries", ConfigEntry=object
        ),
        "homeassistant.core": _module(
            "homeassistant.core", HomeAssistant=object
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
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
    }

    module_name = f"{package_name}.media_player"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    media_player = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        sys.modules[module_name] = media_player
        spec.loader.exec_module(media_player)
    return media_player


class MediaPlayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_media_player_module()

    def make_coordinator(self):
        return SimpleNamespace(
            devices={
                "music-host": {
                    "device_id": "music-host",
                    "device_name": "MixPad Music",
                    "device_type_raw": 128,
                    "uid": "mixpad-uid",
                },
                "gateway": {
                    "device_id": "gateway",
                    "device_name": "MixPad Gateway",
                    "device_type_raw": 114,
                    "uid": "mixpad-uid",
                },
            },
            device_states={
                "music-host": {
                    "online": True,
                    "media_state": "playing",
                    "volume": 46,
                    "muted": False,
                    "media_position": 12.5,
                }
            },
            ssl_client=object(),
            async_media_control=AsyncMock(return_value=True),
        )

    def test_setup_exposes_only_selected_type_128_music_host(self):
        coordinator = self.make_coordinator()
        hass = SimpleNamespace(
            data={"orvibo_smart_control": {"entry-1": coordinator}}
        )
        entry = SimpleNamespace(
            entry_id="entry-1",
            options={"selected_device_ids": ["music-host", "gateway"]},
        )
        entities = []

        asyncio.run(
            self.module.async_setup_entry(hass, entry, entities.extend)
        )

        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0]._device_id, "music-host")
        self.assertEqual(entities[0].state, "playing")
        self.assertEqual(entities[0].volume_level, 0.46)
        self.assertTrue(entities[0].available)

    def test_controls_convert_home_assistant_units(self):
        coordinator = self.make_coordinator()
        entity = self.module.OrviboMusicPlayer(
            coordinator, coordinator.devices["music-host"]
        )

        asyncio.run(entity.async_set_volume_level(0.375))
        asyncio.run(entity.async_media_seek(12.345))
        asyncio.run(entity.async_mute_volume(True))

        self.assertEqual(
            coordinator.async_media_control.await_args_list[0].args,
            ("music-host", "volume", 38),
        )
        self.assertEqual(
            coordinator.async_media_control.await_args_list[1].args,
            ("music-host", "seek", 12345),
        )
        self.assertEqual(
            coordinator.async_media_control.await_args_list[2].args,
            ("music-host", "mute"),
        )

        coordinator.async_media_control.reset_mock()
        coordinator.device_states["music-host"]["muted"] = True
        asyncio.run(entity.async_mute_volume(True))
        coordinator.async_media_control.assert_not_awaited()

    def test_feature_flags_cover_all_implemented_controls(self):
        feature = self.module.MediaPlayerEntityFeature
        expected = (
            feature.PLAY
            | feature.PAUSE
            | feature.PREVIOUS_TRACK
            | feature.NEXT_TRACK
            | feature.VOLUME_SET
            | feature.VOLUME_MUTE
            | feature.SEEK
        )
        self.assertEqual(
            self.module.OrviboMusicPlayer._attr_supported_features, expected
        )


if __name__ == "__main__":
    unittest.main()
