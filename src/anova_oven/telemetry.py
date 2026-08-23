"""Tolerant parsing of oven state events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .models import TemperatureReading, TemperatureSnapshot


def _dig(value: Any, *path: str) -> Any:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 3)
    return None


def _first_number(root: Any, paths: tuple[tuple[str, ...], ...]) -> float | None:
    for path in paths:
        found = _number(_dig(root, *path))
        if found is not None:
            return found
    return None


def _updated_at_or_now(state: Any) -> datetime:
    value = _dig(state, "updatedTimestamp")
    if isinstance(value, str):
        normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            pass
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
    return datetime.now(UTC)


def _probe_connection_state(
    connected: Any, ntc_connected: Any, *, has_reading: bool
) -> bool:
    flags = tuple(flag for flag in (connected, ntc_connected) if isinstance(flag, bool))
    if False in flags:
        return False
    if True in flags:
        return True
    return has_reading


def extract_temperature_snapshot(state_event_payload: dict[str, Any]) -> TemperatureSnapshot:
    """Extract top, bottom, wet-bulb, and plug-in probe readings.

    The control bulb is reported separately because it may duplicate a physical
    top/bottom bulb or represent the active control value, depending on firmware.
    """

    state = state_event_payload.get("state", state_event_payload)
    nodes = state.get("nodes", {}) if isinstance(state, dict) else {}

    top = _first_number(
        nodes,
        (
            ("temperatureBulbs", "dryTop", "current", "celsius"),
            ("temperatureSensors", "top", "current", "celsius"),
            ("temperatureSensors", "top", "celsius"),
        ),
    )
    bottom = _first_number(
        nodes,
        (
            ("temperatureBulbs", "dryBottom", "current", "celsius"),
            ("temperatureSensors", "bottom", "current", "celsius"),
            ("temperatureSensors", "bottom", "celsius"),
        ),
    )
    wet = _first_number(
        nodes,
        (
            ("temperatureBulbs", "wet", "current", "celsius"),
            ("temperatureSensors", "wet", "current", "celsius"),
            ("temperatureSensors", "wetBulb", "celsius"),
        ),
    )
    probe = _first_number(
        nodes,
        (
            ("temperatureProbe", "current", "celsius"),
            ("temperatureProbe", "temperature", "current", "celsius"),
            ("temperatureProbe", "celsius"),
            ("probe", "current", "celsius"),
        ),
    )
    control = _first_number(
        nodes,
        (
            ("temperatureBulbs", "dry", "current", "celsius"),
            ("temperatureBulbs", "wet", "current", "celsius"),
        ),
    )
    top_connected = _dig(nodes, "temperatureBulbs", "dryTop", "ntcConnected")
    bottom_connected = _dig(nodes, "temperatureBulbs", "dryBottom", "ntcConnected")
    wet_connected = _dig(nodes, "temperatureBulbs", "wet", "ntcConnected")
    probe_ntc_connected = _dig(nodes, "temperatureProbe", "ntcConnected")
    probe_connected_flag = _dig(nodes, "temperatureProbe", "connected")
    probe_connected = _probe_connection_state(
        probe_connected_flag,
        probe_ntc_connected,
        has_reading=probe is not None,
    )
    if not probe_connected:
        probe = None
    oven_mode = _dig(state, "state", "mode") or _dig(state, "mode")

    return TemperatureSnapshot(
        top=TemperatureReading(top, top_connected if isinstance(top_connected, bool) else None),
        bottom=TemperatureReading(
            bottom, bottom_connected if isinstance(bottom_connected, bool) else None
        ),
        wet_bulb=TemperatureReading(
            wet, wet_connected if isinstance(wet_connected, bool) else None
        ),
        probe=TemperatureReading(probe, probe_connected),
        probe_connected=probe_connected,
        dry_control=TemperatureReading(control),
        oven_mode=oven_mode if isinstance(oven_mode, str) else None,
        measured_at=_updated_at_or_now(state),
    )
