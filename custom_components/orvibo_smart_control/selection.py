"""Device selection and area mapping helpers for Orvibo config entries.

参照 orvibo-cloud 的设计，支持用户在配置流程中选择要暴露的设备，
并自动将 ORVIBO 房间映射到 Home Assistant 区域。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping
from typing import Any

CONF_SELECTED_DEVICE_IDS = "selected_device_ids"
CONF_DEVICE_AREAS = "device_areas"
CONF_HIDDEN_DEVICE_NAME_PATTERNS = "hidden_device_name_patterns"


def parse_hidden_device_name_patterns(value: object) -> list[str]:
    """Normalize newline/comma-separated name patterns without duplicates."""

    if isinstance(value, str):
        candidates = re.split(r"[\r\n,，;；]+", value)
    elif isinstance(value, (list, tuple, set)):
        candidates = [str(item) for item in value]
    else:
        return []
    patterns: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        pattern = candidate.strip()
        folded = pattern.casefold()
        if not pattern or folded in seen:
            continue
        seen.add(folded)
        patterns.append(pattern)
    return patterns


def device_name_is_hidden(
    device: Mapping[str, Any], patterns: Iterable[str]
) -> bool:
    """Return whether a device name matches a substring or glob pattern."""

    name = str(device.get("device_name") or device.get("name") or "").casefold()
    if not name:
        return False
    for raw_pattern in patterns:
        pattern = str(raw_pattern).strip().casefold()
        if not pattern:
            continue
        if any(token in pattern for token in "*?["):
            if fnmatch.fnmatchcase(name, pattern):
                return True
        elif pattern in name:
            return True
    return False


def visible_devices_by_name(
    devices: Iterable[Mapping[str, Any]], patterns: Iterable[str]
) -> list[Mapping[str, Any]]:
    """Filter name-matched devices while retaining cloud/API order."""

    normalized = parse_hidden_device_name_patterns(list(patterns))
    return [
        device
        for device in devices
        if not device_name_is_hidden(device, normalized)
    ]


def selected_device_ids(
    options: Mapping[str, Any],
    available_device_ids: Iterable[str] | Mapping[str, Any],
) -> set[str]:
    """Return selected visible IDs; legacy entries select every visible device."""

    if isinstance(available_device_ids, Mapping):
        available = {str(device_id) for device_id in available_device_ids}
        patterns = parse_hidden_device_name_patterns(
            options.get(CONF_HIDDEN_DEVICE_NAME_PATTERNS, [])
        )
        if patterns:
            available -= {
                str(device_id)
                for device_id, device in available_device_ids.items()
                if isinstance(device, Mapping)
                and device_name_is_hidden(device, patterns)
            }
    else:
        available = {str(device_id) for device_id in available_device_ids}
    if CONF_SELECTED_DEVICE_IDS not in options:
        return available
    configured = options.get(CONF_SELECTED_DEVICE_IDS)
    if not isinstance(configured, (list, tuple, set)):
        return set()
    return {str(device_id) for device_id in configured} & available


def device_is_selected(options: Mapping[str, Any], device_id: str) -> bool:
    """检查某个设备是否被选中。"""
    if CONF_SELECTED_DEVICE_IDS not in options:
        return True
    configured = options.get(CONF_SELECTED_DEVICE_IDS)
    if not isinstance(configured, (list, tuple, set)):
        return False
    return device_id in {str(value) for value in configured}


def configured_device_areas(options: Mapping[str, Any]) -> dict[str, str | None]:
    """返回配置的区域映射，过滤无效值。"""
    configured = options.get(CONF_DEVICE_AREAS)
    if not isinstance(configured, Mapping):
        return {}
    areas: dict[str, str | None] = {}
    for device_id, area_id in configured.items():
        if area_id is None:
            areas[str(device_id)] = None
        elif isinstance(area_id, str) and area_id:
            areas[str(device_id)] = area_id
    return areas
