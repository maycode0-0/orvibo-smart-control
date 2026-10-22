"""Stateful orchestration for normalized smart-lock events."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import math
import time
from typing import Any, Callable, Mapping, Optional
from uuid import uuid4

from .lock_status import (
    normalize_door_lock_properties,
    normalize_lock_event,
    normalize_message_event,
    resolve_opened_by,
    normalize_unlock_method,
    unlock_direction,
)
from .parsers.base import StatePatch


UserNameResolver = Callable[[str, object], Optional[str]]
CredentialNameResolver = Callable[[str, object, Optional[str]], Optional[str]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class TransientLockUpdate:
    """A transient entity-state patch plus its optional reset category."""

    patch: StatePatch
    reset_kind: Optional[str] = None


class LockEventManager:
    """Normalize, deduplicate, and attribute door-lock events."""

    def __init__(
        self,
        user_name: UserNameResolver,
        *,
        door_open_window: float = 30,
        clock: Clock = time.monotonic,
        credential_name: Optional[CredentialNameResolver] = None,
    ) -> None:
        self._user_name = user_name
        self._credential_name = credential_name
        self._door_open_window = door_open_window
        self._clock = clock
        self._last_signatures: dict[str, tuple[Any, ...]] = {}
        self._last_unlocks: dict[str, dict[str, Any]] = {}
        self._latest_unlock_times: dict[str, float] = {}
        self._door_states: dict[str, bool] = {}
        self._seen_events: dict[str, OrderedDict[tuple[Any, ...], float]] = {}

    def build_event(
        self, device_id: str, raw_status: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Build one public event, returning ``None`` for duplicates/no-op packets."""

        lock = normalize_door_lock_properties(raw_status.get("properties"))
        event = normalize_lock_event(raw_status)
        if event is None and all(
            lock[field] is None
            for field in (
                "locked",
                "door_open",
                "inside_locked",
                "child_locked",
                "leave_home_armed",
            )
        ):
            return None

        if event is not None:
            signature = (
                event.get("kind"),
                event.get("unlock_type"),
                event.get("unlock_user_id"),
                event.get("time"),
                event.get("pic_url") or event.get("doorbell_url"),
            )
        else:
            signature = (
                "state",
                lock["locked"],
                lock["door_open"],
                lock["inside_locked"],
                lock["child_locked"],
                lock["leave_home_armed"],
            )
        now = self._clock()
        if event is not None:
            # A state push between two deliveries must not resurrect an old unlock.
            seen = self._seen_events.setdefault(device_id, OrderedDict())
            key = tuple(str(value) for value in signature)
            if key in seen and (event.get("time") is not None or now - seen[key] < 1):
                return None
            seen[key] = now
            seen.move_to_end(key)
            while len(seen) > 64:
                seen.popitem(last=False)
        elif self._last_signatures.get(device_id) == signature:
            return None
        self._last_signatures[device_id] = signature

        data: dict[str, Any] = {
            "device_id": device_id,
            "uid": raw_status.get("uid", ""),
            "locked": lock["locked"],
            "door_open": lock["door_open"],
            "inside_locked": lock["inside_locked"],
            "child_locked": lock["child_locked"],
            "leave_home_armed": lock["leave_home_armed"],
        }
        if event is not None:
            data.update(event)

        if event is not None and event.get("kind") == "unlock":
            # Keep one identity for an unlock and the following door opening.
            # Producer timestamps also make replayed packets idempotent on disk.
            try:
                event_time = float(event.get("time"))
            except (TypeError, ValueError, OverflowError):
                event_time = None
            if event_time is None or not math.isfinite(event_time) or event_time < 0:
                event_time = None
                data["time"] = int(time.time())
            else:
                data["time"] = int(event_time)
            data["opening_id"] = (
                hashlib.sha256(repr(signature).encode()).hexdigest()[:16]
                if event_time is not None else uuid4().hex[:16]
            )
            name = self._resolve_name(device_id, event.get("unlock_user_id"), event.get("unlock_method"))
            if name:
                data["unlock_user_name"] = name
            if event_time is not None and math.isfinite(event_time):
                if event_time < self._latest_unlock_times.get(device_id, event_time):
                    # Still archive the picture, but do not change the latest
                    # opening/person or create a new correlation opportunity.
                    data["historical"] = True
                    return data
                self._latest_unlock_times[device_id] = event_time
            self._last_unlocks[device_id] = {
                "user_id": event.get("unlock_user_id"),
                "unlock_type": event.get("unlock_type"),
                "open_direction": event.get("open_direction", "inside"),
                "unlock_method": event.get("unlock_method"),
                "time": data.get("time"),
                "opening_id": data["opening_id"],
                "at": now,
            }

        previous_door = self._door_states.get(device_id)
        if lock["door_open"] is not None:
            self._door_states[device_id] = lock["door_open"]
        if lock["door_open"] is False and previous_door is True:
            self._last_unlocks.pop(device_id, None)
        if lock["door_open"] is True and previous_door is not True:
            data["door_opened"] = True
            data["opening_id"] = uuid4().hex[:16]
            try:
                data["time"] = int(raw_status.get("time"))
                if data["time"] < 0:
                    raise ValueError("negative_opening_time")
            except (TypeError, ValueError, OverflowError):
                data["time"] = int(time.time())
            # Openings without a matching outside credential follow the inside policy.
            data["open_direction"] = "inside"
            data["unlock_method"] = None
            data["unlock_user_id"] = None
            data["unlock_type"] = None
            last_unlock = self._last_unlocks.pop(device_id, None)
            elapsed = now - last_unlock["at"] if last_unlock is not None else 0
            # Prefer producer timestamps when both packets carry one. Delayed
            # delivery of an old event must not be attributed to a new opening.
            if (
                last_unlock is not None
                and raw_status.get("time") is not None
                and last_unlock.get("time") is not None
            ):
                try:
                    producer_elapsed = float(raw_status["time"]) - float(last_unlock["time"])
                    elapsed = (
                        max(elapsed, producer_elapsed)
                        if math.isfinite(producer_elapsed) and producer_elapsed >= 0
                        else self._door_open_window + 1
                    )
                except (TypeError, ValueError, OverflowError):
                    elapsed = self._door_open_window + 1
            if last_unlock is not None and 0 <= elapsed <= self._door_open_window:
                data["opening_id"] = last_unlock["opening_id"]
                data["time"] = last_unlock["time"]
                method = last_unlock.get("unlock_method") or normalize_unlock_method(last_unlock.get("unlock_type"))
                data["open_direction"] = unlock_direction(method)
                if last_unlock.get("open_direction") == "inside" and data["open_direction"] != "outside":
                    data["open_direction"] = "inside"
                data["unlock_method"] = method
                data["unlock_type"] = last_unlock.get("unlock_type")
            opened = (
                resolve_opened_by(
                    last_unlock,
                    elapsed,
                    self._door_open_window,
                )
                if last_unlock is not None and data["open_direction"] == "outside"
                else None
            )
            if opened is not None:
                data["unlock_user_id"] = opened["user_id"]
                data["opened_by_user_id"] = opened["user_id"]
                data["opened_by_type"] = opened["unlock_type"]
                name = self._resolve_name(device_id, opened["user_id"], data.get("unlock_method"))
                if name:
                    data["opened_by_name"] = name
        return data

    def _resolve_name(self, device_id: str, user_id: object, method: Optional[str]) -> Optional[str]:
        if self._credential_name is not None:
            return self._credential_name(device_id, user_id, method)
        return self._user_name(device_id, user_id)

    @staticmethod
    def build_message(
        device_id: str, raw_status: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Normalize a cmd=82 lock message for public event publication."""

        event = normalize_message_event(raw_status)
        if event is None:
            return None
        event["device_id"] = device_id or event.get("device_id")
        event["snapshot_kind"] = LockEventManager.snapshot_kind(
            event.get("text") or ""
        )
        return event

    @staticmethod
    def transient_update(raw_status: Mapping[str, Any]) -> TransientLockUpdate:
        """Convert cmd=352 UI flags to a pure patch and reset instruction."""

        event = normalize_lock_event(raw_status)
        if event is None:
            return TransientLockUpdate(StatePatch({}))
        kind = event.get("kind")
        if kind == "ring":
            return TransientLockUpdate(
                StatePatch(
                    {
                        "doorbell_ring": True,
                        "doorbell_url": event.get("doorbell_url"),
                        "doorbell_ip": event.get("doorbell_ip"),
                    }
                ),
                "doorbell",
            )
        if kind == "answered":
            return TransientLockUpdate(StatePatch({"doorbell_answered": True}))
        if kind == "bye":
            return TransientLockUpdate(StatePatch({"doorbell_answered": False}))
        if kind == "unlock":
            return TransientLockUpdate(
                StatePatch(
                    {
                        "unlock_event": True,
                    }
                ),
                "unlock",
            )
        return TransientLockUpdate(StatePatch({}))

    @staticmethod
    def snapshot_kind(text: str) -> str:
        """Classify message snapshots for history-card grouping."""

        if "逗留" in text:
            return "loiter"
        if "来访" in text or "访客" in text:
            return "visit"
        return "message"

    def remove(self, device_id: str) -> None:
        """Discard event correlation state for a removed device."""

        self._last_signatures.pop(device_id, None)
        self._last_unlocks.pop(device_id, None)
        self._latest_unlock_times.pop(device_id, None)
        self._door_states.pop(device_id, None)
        self._seen_events.pop(device_id, None)
