"""Runtime device grouping helpers for the config and options flows."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .selection import CONF_SELECTED_DEVICE_IDS

DEVICE_GROUP_FIELD_PREFIX = "device_group_"
DEVICE_GROUP_ALL_FIELD_PREFIX = "device_group_all_"


@dataclass(frozen=True)
class DeviceSelectionGroup:
    """A group generated from the devices returned by HomeMate."""

    key: str
    label: str
    devices: tuple[Mapping[str, Any], ...]

    @property
    def device_field(self) -> str:
        return f"{DEVICE_GROUP_FIELD_PREFIX}{self.key}"

    @property
    def all_field(self) -> str:
        return f"{DEVICE_GROUP_ALL_FIELD_PREFIX}{self.key}"

    @property
    def device_ids(self) -> tuple[str, ...]:
        return tuple(str(device.get("device_id") or "") for device in self.devices)


# Name matching is intentionally a small, editable heuristic.  It lets real
# device names improve the UI without coupling config-flow rendering to the
# integration's controllability enum.
_NAME_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("lights", "灯具", ("灯", "灯带", "射灯", "筒灯", "照明")),
    ("switches", "开关", ("开关", "面板", "插座")),
    ("curtains", "窗帘", ("窗帘", "卷帘", "纱帘")),
    ("clothes_horse", "晾衣机", ("晾衣", "晾衣架")),
    ("climate", "暖通", ("空调", "地暖", "暖气", "风机盘管")),
    ("ventilation", "新风", ("新风", "通风")),
    (
        "sensors",
        "传感器",
        ("传感器", "温湿度", "人体", "烟雾", "水浸", "燃气", "门磁", "门窗"),
    ),
    ("locks", "门锁", ("门锁", "智能锁", "指纹锁")),
    ("cameras", "摄像头", ("摄像头", "相机", "监控")),
)

# Fallback for records whose display name is generic or empty.
_RAW_TYPE_GROUPS: dict[int, tuple[str, str]] = {
    0: ("lights", "灯具"),
    1: ("lights", "灯具"),
    14: ("cameras", "摄像头"),
    22: ("sensors", "传感器"),
    23: ("sensors", "传感器"),
    25: ("sensors", "传感器"),
    26: ("sensors", "传感器"),
    27: ("sensors", "传感器"),
    34: ("curtains", "窗帘"),
    35: ("curtains", "窗帘"),
    36: ("climate", "暖通"),
    38: ("lights", "灯具"),
    43: ("lights", "灯具"),
    46: ("sensors", "传感器"),
    52: ("clothes_horse", "晾衣机"),
    54: ("sensors", "传感器"),
    56: ("sensors", "传感器"),
    102: ("lights", "灯具"),
    112: ("climate", "暖通"),
    114: ("other", "其他设备"),
    128: ("other", "其他设备"),
    135: ("switches", "开关"),
    136: ("switches", "开关"),
    137: ("switches", "开关"),
    143: ("switches", "开关"),
    150: ("other", "其他设备"),
    501: ("lights", "灯具"),
    502: ("lights", "灯具"),
    503: ("lights", "灯具"),
    506: ("curtains", "窗帘"),
    511: ("switches", "开关"),
    516: ("ventilation", "新风"),
    518: ("switches", "开关"),
    522: ("locks", "门锁"),
    10086: ("lights", "灯具"),
}

_TYPE_NAME_GROUPS: dict[str, tuple[str, str]] = {
    "light": ("lights", "灯具"),
    "switch": ("switches", "开关"),
    "cover": ("curtains", "窗帘"),
    "climate": ("climate", "暖通"),
    "fan": ("climate", "暖通"),
    "sensor": ("sensors", "传感器"),
    "binary_sensor": ("sensors", "传感器"),
    "lock": ("locks", "门锁"),
    "camera": ("cameras", "摄像头"),
}


def _text(value: object) -> str:
    return str(value or "").strip().lower()


def _raw_type(device: Mapping[str, Any]) -> int | None:
    value = device.get("device_type_raw")
    if value is None:
        value = device.get("deviceType")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sub_type(device: Mapping[str, Any]) -> int | None:
    value = device.get("sub_device_type")
    if value is None:
        value = device.get("subDeviceType")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return value or "other"


def infer_device_group(device: Mapping[str, Any]) -> tuple[str, str]:
    """Infer a group key and display label from one device record."""

    searchable = " ".join(
        _text(device.get(field))
        for field in ("device_name", "class_name", "model", "ui_model", "device_type")
    )
    for key, label, keywords in _NAME_GROUPS:
        if any(keyword in searchable for keyword in keywords):
            return key, label

    raw_type = _raw_type(device)
    if raw_type == 300:
        sub_type = _sub_type(device)
        if sub_type == 481:
            return "climate", "暖通"
        if sub_type == 491:
            return "sensors", "传感器"
    raw_group = _RAW_TYPE_GROUPS.get(raw_type)
    if raw_group is not None:
        return raw_group

    type_name = _text(device.get("device_type"))
    named_group = _TYPE_NAME_GROUPS.get(type_name)
    if named_group is not None:
        return named_group
    if type_name and type_name != "unknown":
        return _slug(type_name), str(device.get("device_type")).strip()
    return "other", "其他设备"


def device_selection_groups(
    devices: Iterable[Mapping[str, Any]],
) -> tuple[DeviceSelectionGroup, ...]:
    """Build dynamic groups, preserving the order of the returned devices."""

    buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    labels: dict[str, str] = {}
    for device in devices:
        key, label = infer_device_group(device)
        buckets[key].append(device)
        labels.setdefault(key, label)
    return tuple(
        DeviceSelectionGroup(key, labels[key], tuple(values))
        for key, values in buckets.items()
    )


def grouped_devices(
    devices: Iterable[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Compatibility view of :func:`device_selection_groups`."""

    return {
        group.key: list(group.devices)
        for group in device_selection_groups(devices)
    }


def merge_grouped_selection(
    user_input: Mapping[str, Any],
    devices: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Merge dynamic category fields into the persisted flat ID list."""

    ordered_devices = list(devices)
    available = {str(device.get("device_id") or "") for device in ordered_devices}
    if CONF_SELECTED_DEVICE_IDS in user_input:
        raw_requested = user_input.get(CONF_SELECTED_DEVICE_IDS, [])
        if not isinstance(raw_requested, (list, tuple, set)):
            return []
        requested = {str(value) for value in raw_requested}
    else:
        requested: set[str] = set()
        for group in device_selection_groups(ordered_devices):
            if bool(user_input.get(group.all_field, False)):
                requested.update(group.device_ids)
                continue
            values = user_input.get(group.device_field, [])
            if isinstance(values, (list, tuple, set)):
                requested.update(str(value) for value in values)

    requested &= available
    return [
        str(device.get("device_id") or "")
        for device in ordered_devices
        if str(device.get("device_id") or "") in requested
    ]


__all__ = [
    "DeviceSelectionGroup",
    "device_selection_groups",
    "grouped_devices",
    "infer_device_group",
    "merge_grouped_selection",
]
