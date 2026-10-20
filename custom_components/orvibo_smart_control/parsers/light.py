"""Pure parsers for light and switch status packets."""

from __future__ import annotations

from typing import Any, Mapping

from ..light_values import normalize_brightness
from ..protocol_profiles import light_wire_brightness, profile_for
from .base import StatePatch


def _subdevice_type(raw_status: Mapping[str, Any]) -> Any:
    value = raw_status.get("subDeviceType", 0)
    return int(value) if isinstance(value, str) else value


def parse_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse an on/off light using properties first, then legacy value1."""

    props = raw_status.get("properties", {})
    onoff_obj = props.get("onoff", {})
    if isinstance(onoff_obj, dict) and onoff_obj.get("status"):
        state = onoff_obj.get("status") == "on"
    elif isinstance(props.get("onoff_status"), str):
        state = props["onoff_status"] == "on"
    elif raw_status.get("value1") is not None:
        state = int(raw_status["value1"]) == 0
    else:
        return StatePatch({})
    return StatePatch({"state": state})


def _legacy_brightness(raw_status: Mapping[str, Any]) -> int | None:
    """Fall back to properties when a legacy level is missing or invalid."""
    props = raw_status.get("properties") or {}
    brightness = normalize_brightness(raw_status.get("value2"))
    if brightness is None:
        raw_brightness = props.get("brightness")
        if isinstance(raw_brightness, dict):
            # 属性型亮度可能是 {"percent": 0-100} / {"value": 0-255} / {"level": ...}
            percent = raw_brightness.get("percent")
            if normalize_brightness(percent, 100) is not None:
                try:
                    # type=38 量纲为 0-255，percent 换算后钳制
                    brightness = normalize_brightness(float(percent) * 255 / 100)
                except (TypeError, ValueError, OverflowError):
                    brightness = None
            else:
                brightness = next(
                    (
                        raw_brightness[key]
                        for key in ("value", "level", "brightness")
                        if key in raw_brightness
                    ),
                    None,
                )
        else:
            brightness = raw_brightness
    return normalize_brightness(brightness)


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def parse_dim_color_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse the active-low legacy dimmable, tunable-white light."""

    props = raw_status.get("properties") or {}
    brightness = _legacy_brightness(raw_status)

    color_temp = _positive_int(raw_status.get("value3"))
    if color_temp is not None:
        if 150 <= color_temp <= 400:
            color_temp = 1_000_000 // color_temp
    else:
        color_temp = props.get("colortemp")
        if color_temp is None:
            color_temp = props.get("colorTemp")
        if isinstance(color_temp, dict):
            color_temp = color_temp.get("value")
        color_temp = _positive_int(color_temp)
    if color_temp is not None:
        color_temp = min(6500, max(2700, color_temp))

    value1 = raw_status.get("value1")
    if isinstance(value1, dict):
        value1 = value1.get("value")
    if value1 is not None:
        try:
            state = int(value1) == 0
        except (TypeError, ValueError):
            state = None
    else:
        onoff_obj = props.get("onoff", {})
        if isinstance(onoff_obj, dict) and onoff_obj.get("status"):
            state = onoff_obj.get("status") == "on"
        elif brightness is not None:
            state = brightness > 0
        else:
            state = None

    updates = {"state": state} if state is not None else {}
    if brightness is not None:
        updates["brightness"] = brightness
    if color_temp is not None:
        updates["color_temp"] = color_temp
    return StatePatch(updates)


def parse_fast_move_dim_color_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a Fast Move value1/value2/value3 light packet."""

    updates: dict[str, Any] = {}
    value1 = raw_status.get("value1")
    brightness = _legacy_brightness(raw_status)
    color_temp = _positive_int(raw_status.get("value3"))

    if value1 is not None:
        updates["state"] = int(value1) == 0

    if brightness is not None:
        updates["brightness"] = brightness
        if brightness == 0:
            updates["state"] = False

    if color_temp is not None:
        if 150 <= color_temp <= 400:
            updates["color_temp"] = min(6000, max(2700, 1_000_000 // color_temp))
        elif color_temp >= 1000:
            updates["color_temp"] = min(6000, max(2700, color_temp))

    return StatePatch(updates)


def _property_brightness(props: Mapping[str, Any]) -> Any:
    brightness = props.get("brightness", {})
    return brightness.get("percent") if isinstance(brightness, dict) else brightness


def parse_dimmable_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a property-based dimmable light."""

    props = raw_status.get("properties", {})
    updates: dict[str, Any] = {}
    onoff_obj = props.get("onoff", {})
    if isinstance(onoff_obj, dict) and onoff_obj.get("status"):
        updates["state"] = onoff_obj.get("status") == "on"
    elif raw_status.get("value1") in (0, 1, "0", "1"):
        updates["state"] = int(raw_status["value1"]) == 0
    elif "state" not in current_state:
        updates["state"] = False

    brightness = normalize_brightness(_property_brightness(props), 100)
    if brightness is not None:
        updates["brightness"] = brightness
        if brightness == 0:
            updates["state"] = False
    return StatePatch(updates)


def parse_zigbee_dimmable_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse an active-low value-based 0-10V dimmable light."""

    updates: dict[str, Any] = {}
    value1 = raw_status.get("value1")
    brightness = _legacy_brightness(raw_status)

    if value1 is not None:
        updates["state"] = int(value1) == 0
    if brightness is not None:
        updates["brightness"] = brightness
        if brightness == 0:
            updates["state"] = False
    return StatePatch(updates)


def parse_native_dimmer(
    current_state: Mapping[str, Any],
    raw_status: Mapping[str, Any],
    device: Mapping[str, Any],
) -> StatePatch:
    """Keep native value2 units, including property-only cloud/push updates."""
    power_status = dict(raw_status)
    power_status["properties"] = raw_status.get("properties") or {}
    if normalize_brightness(raw_status.get("value1")) is None:
        # Native status -1 means this field was not reported.
        power_status.pop("value1", None)
    updates = dict(parse_light(current_state, power_status).values)
    for field in ("value3", "value4"):
        value = raw_status.get(field)
        try:
            if not isinstance(value, bool) and value is not None and int(value) >= 0:
                updates[f"raw_{field}"] = int(value)
        except (TypeError, ValueError, OverflowError):
            pass
    maximum = 120 if profile_for(device).dimmer_scale == 120 else 255
    brightness = normalize_brightness(raw_status.get("value2"), maximum)
    if brightness is None:
        props = raw_status.get("properties") or {}
        percent = normalize_brightness(_property_brightness(props), 100)
        if percent is not None:
            brightness = light_wire_brightness(percent, device)
    if brightness is not None:
        updates["brightness"] = brightness
    return StatePatch(updates)


def parse_cct_light(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a property-based brightness and color-temperature light."""

    props = raw_status.get("properties", {})
    updates = dict(parse_dimmable_light(current_state, raw_status).values)
    color_temp = props.get("colorTemp", {})
    color_temp = color_temp.get("value") if isinstance(color_temp, dict) else color_temp
    color_temp = _positive_int(color_temp)
    if color_temp is None:
        color_temp = _positive_int(raw_status.get("value3"))
        if color_temp is not None and 150 <= color_temp <= 400:
            color_temp = 1_000_000 // color_temp
    if color_temp is not None:
        updates["color_temp"] = min(6500, max(2000, color_temp))
    return StatePatch(updates)


def parse_switch(
    current_state: Mapping[str, Any], raw_status: Mapping[str, Any]
) -> StatePatch:
    """Parse a switch channel from property or legacy value fields."""

    props = raw_status.get("properties", {})
    onoff_obj = props.get("onoff", {})
    if isinstance(onoff_obj, dict) and onoff_obj.get("status"):
        state = onoff_obj.get("status") == "on"
    elif raw_status.get("value1") is not None:
        sub_device_type = _subdevice_type(raw_status)
        state = int(raw_status["value1"]) == (0 if sub_device_type == -2 else 1)
    else:
        return StatePatch({})
    return StatePatch({"state": state})
