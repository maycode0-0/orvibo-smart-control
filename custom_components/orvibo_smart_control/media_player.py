"""Home Assistant media-player entities for MixPad background music."""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import OrviboSmartControlCoordinator
from .device_types import DeviceCategory, classify_device
from .selection import selected_device_ids


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OrviboSmartControlCoordinator = hass.data[DOMAIN][
        config_entry.entry_id
    ]
    selected = selected_device_ids(config_entry.options, coordinator.devices)
    entities = [
        OrviboMusicPlayer(coordinator, device)
        for device_id, device in coordinator.devices.items()
        if device_id in selected
        and classify_device(device) == DeviceCategory.MUSIC_HOST
    ]
    async_add_entities(entities)


class OrviboMusicPlayer(CoordinatorEntity, MediaPlayerEntity):
    """Cloud-controlled media player exposed by a MixPad music host."""

    _attr_has_entity_name = True
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SEEK
    )

    def __init__(
        self,
        coordinator: OrviboSmartControlCoordinator,
        device: dict,
    ) -> None:
        super().__init__(coordinator)
        self._device = device
        self._device_id = str(device["device_id"])
        name = str(device.get("device_name") or "MixPad Music")
        self._attr_unique_id = f"{DOMAIN}_media_player_{self._device_id}"
        self._attr_name = name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": name,
            "model": device.get("model", ""),
            "manufacturer": MANUFACTURER,
        }

    @property
    def _state_data(self) -> dict:
        return self.coordinator.device_states.get(self._device_id, {})

    @property
    def available(self) -> bool:
        return bool(self.coordinator.ssl_client) and self._state_data.get(
            "online", True
        )

    @property
    def state(self) -> str:
        return str(self._state_data.get("media_state") or "idle")

    @property
    def volume_level(self) -> float | None:
        volume = self._state_data.get("volume")
        return None if volume is None else max(0.0, min(1.0, float(volume) / 100))

    @property
    def is_volume_muted(self) -> bool | None:
        muted = self._state_data.get("muted")
        return None if muted is None else bool(muted)

    @property
    def media_title(self) -> str | None:
        return self._state_data.get("media_title")

    @property
    def media_artist(self) -> str | None:
        return self._state_data.get("media_artist")

    @property
    def media_album_name(self) -> str | None:
        return self._state_data.get("media_album")

    @property
    def media_image_url(self) -> str | None:
        return self._state_data.get("media_image_url")

    @property
    def media_duration(self) -> float | None:
        return self._state_data.get("media_duration")

    @property
    def media_position(self) -> float | None:
        return self._state_data.get("media_position")

    async def async_media_play(self) -> None:
        await self.coordinator.async_media_control(self._device_id, "play")

    async def async_media_pause(self) -> None:
        await self.coordinator.async_media_control(self._device_id, "pause")

    async def async_media_previous_track(self) -> None:
        await self.coordinator.async_media_control(self._device_id, "previous")

    async def async_media_next_track(self) -> None:
        await self.coordinator.async_media_control(self._device_id, "next")

    async def async_set_volume_level(self, volume: float) -> None:
        level = max(0, min(100, round(float(volume) * 100)))
        await self.coordinator.async_media_control(
            self._device_id, "volume", level
        )

    async def async_mute_volume(self, mute: bool) -> None:
        if self.is_volume_muted is None or bool(mute) != self.is_volume_muted:
            await self.coordinator.async_media_control(self._device_id, "mute")

    async def async_media_seek(self, position: float) -> None:
        milliseconds = max(0, round(float(position) * 1000))
        await self.coordinator.async_media_control(
            self._device_id, "seek", milliseconds
        )

    @property
    def should_poll(self) -> bool:
        return False
