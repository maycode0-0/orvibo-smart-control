"""Reverse-engineered HomeMate protocol profiles.

The APK keeps device identity beside the generic device type.  This module
turns the identity fields into small, deterministic wire-value helpers so the
control layer does not have to guess from a Home Assistant entity category.
It deliberately contains no I/O and unknown identities fall back to the
conservative generic profile.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .control import OrviboControlCommand
from .light_values import normalize_brightness

VIRTUAL_MIXPAD_DIMMER_MODEL = "VirtualModelidForMixpadD"
NATIVE_DIMMER_TYPES = frozenset({0, 116})
NATIVE_SIMPLE_LIGHT_TYPES = frozenset({1, 2, 9, 10, 29, 43, 102})
NATIVE_CURTAIN_TYPES = frozenset(
    {3, 4, 8, 34, 35, 37, 39, 42, 70, 73, 74, 75, 76, 105, 106, 109, 110, 111, 118, 126, 127}
)


def _int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _type(device: Mapping[str, Any]) -> int | None:
    value = device.get("device_type_raw")
    return _int(device.get("deviceType") if value is None else value)


def _subtype(device: Mapping[str, Any]) -> int | None:
    return _int(device.get("sub_device_type", device.get("subDeviceType")))


def is_rf_curtain(device: Mapping[str, Any]) -> bool:
    """Return the explicit RF-curtain flag from inventory metadata."""
    value = device.get("is_rf_curtain", device.get("isRFCurtain"))
    return value is True or value in (1, "1", "true", "True")


def light_wire_brightness(percent: float, device: Mapping[str, Any]) -> int:
    """Encode the APK's native brightness conversion.

    Normal dimmers use ``round(p*253/100)+2``; type 116 uses a 0..120 scale,
    and the MixPad virtual dimmer uses the full 0..255 scale.  The caller
    supplies a protocol percentage, not HA's 0..255 value.
    """
    value = max(0.0, min(100.0, float(percent)))
    model = str(device.get("model", ""))
    if model == VIRTUAL_MIXPAD_DIMMER_MODEL:
        return max(0, min(255, math.floor(value * 255 / 100 + 0.5)))
    if _type(device) == 116:
        return max(0, min(120, math.floor(value * 120 / 100 + 0.5)))
    # Java Math.round(x) is floor(x + .5) for these non-negative values.
    return max(2, min(255, math.floor(value * 253 / 100 + 0.5) + 2))


def light_percent_from_wire(value: Any, device: Mapping[str, Any]) -> int | None:
    """Decode a native dimmer value to a protocol percentage."""
    raw = normalize_brightness(value)
    if raw is None:
        return None
    # The decoder checks type 116 before the virtual model, unlike the encoder.
    if _type(device) == 116:
        return min(100, math.floor(min(raw, 120) * 100 / 120 + 0.5))
    if device.get("model") == VIRTUAL_MIXPAD_DIMMER_MODEL:
        return 1 if raw == 0 else min(100, math.floor(raw * 100 / 255 + 0.5))
    # libpvconvert.so getBrightPercentByBright (0x138438), integer division.
    return max(1, min(100, (raw * 100 - 72) // 253))


def light_wire_from_ha(brightness: int, device: Mapping[str, Any]) -> int:
    """Encode a nonzero HA brightness; zero is handled as a power command."""
    percent = max(1, min(100, math.floor(brightness * 100 / 255 + 0.5)))
    return light_wire_brightness(percent, device)


def dimmer_retained_values(
    state: Mapping[str, Any], device: Mapping[str, Any]
) -> dict[str, int]:
    """Preserve known native mode fields; ignore missing/sentinel values."""
    values = {}
    for field in ("value3", "value4"):
        if field == "value4" and device.get("model") == VIRTUAL_MIXPAD_DIMMER_MODEL:
            continue
        value = _int(state.get(f"raw_{field}", device.get(field)))
        if value is not None and value >= 0:
            values[field] = value
    return values


def curtain_command(action: str, device: Mapping[str, Any], position: int | None = None) -> OrviboControlCommand:
    """Build ordinary or RF-curtain values from the native action names."""
    action = action.lower().strip()
    if is_rf_curtain(device):
        value = {"open": 380000, "close": 380001, "stop": 380002}.get(action)
        if value is None:
            raise ValueError("unsupported_rf_curtain_action")
        return OrviboControlCommand("rf control", value)
    if action in {"open", "close"}:
        if position is None:
            position = 100 if action == "open" else 0
        return OrviboControlCommand(action, max(0, min(100, int(position))))
    if action == "stop":
        return OrviboControlCommand("stop", 0)
    raise ValueError("unsupported_curtain_action")


@dataclass(frozen=True, slots=True)
class ProtocolProfile:
    """Stable metadata used by control and diagnostics."""

    device_type: int | None
    sub_device_type: int | None
    dimmer_scale: int | None
    rf_curtain: bool
    control_evidence: str


def profile_for(device: Mapping[str, Any]) -> ProtocolProfile:
    device_type = _type(device)
    model = str(device.get("model", ""))
    if device_type in NATIVE_DIMMER_TYPES and model == VIRTUAL_MIXPAD_DIMMER_MODEL:
        scale = 255
    elif device_type == 116:
        scale = 120
    elif device_type in NATIVE_DIMMER_TYPES:
        scale = 253
    else:
        scale = None
    if is_rf_curtain(device):
        evidence = "native-rf-curtain"
    elif device_type in NATIVE_CURTAIN_TYPES:
        evidence = "native-curtain"
    elif device_type in NATIVE_SIMPLE_LIGHT_TYPES:
        evidence = "native-simple-light"
    elif scale is not None:
        evidence = "native-dimmer"
    else:
        evidence = "generic"
    return ProtocolProfile(device_type, _subtype(device), scale, is_rf_curtain(device), evidence)
