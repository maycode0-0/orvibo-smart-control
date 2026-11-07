"""Pure parser for MixPad background-music properties."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .base import StatePatch


def _property_value(properties: Mapping[str, Any], name: str) -> Any:
    value = properties.get(name)
    if not isinstance(value, Mapping):
        return value
    for key in ("value", name, "status"):
        if key in value:
            return value[key]
    return None


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _song_value(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    decoded: Any = value.strip()
    for _ in range(2):
        if not isinstance(decoded, str):
            break
        try:
            decoded = json.loads(decoded)
        except (TypeError, ValueError):
            return None
    return decoded if isinstance(decoded, Mapping) else None


def parse_music_host(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Normalize property snapshots and statusList updates from a music host."""
    properties = raw_status.get("properties", {})
    if not isinstance(properties, Mapping):
        return StatePatch({})

    updates: dict[str, Any] = {}
    play_status = _int_value(_property_value(properties, "music_play_status"))
    if play_status is not None:
        updates["music_play_status"] = play_status
        updates["media_state"] = "playing" if play_status == 1 else "paused"

    play_mode = _int_value(_property_value(properties, "music_play_mode"))
    if play_mode is not None:
        updates["music_play_mode"] = play_mode

    volume = _int_value(_property_value(properties, "volume"))
    if volume is not None:
        updates["volume"] = max(0, min(100, volume))

    mute = _int_value(_property_value(properties, "mute"))
    if mute is not None:
        # The official app treats property value 0 as the muted state.
        updates["muted"] = mute == 0

    current_song = _song_value(_property_value(properties, "music_current_song"))
    if current_song is not None:
        music = current_song.get("music")
        if not isinstance(music, Mapping):
            music = {}
        updates["media_position"] = max(
            0, (_int_value(current_song.get("position")) or 0) / 1000
        )
        updates["media_title"] = music.get("title")
        updates["media_artist"] = music.get("singer")
        updates["media_album"] = music.get("albums")
        updates["media_image_url"] = music.get("imageUrl")
        duration = _int_value(music.get("duration"))
        if duration is not None:
            updates["media_duration"] = max(0, duration / 1000)

    return StatePatch(updates)
