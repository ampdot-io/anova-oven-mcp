from __future__ import annotations

from datetime import UTC, datetime

import pytest

from anova_oven.telemetry import extract_temperature_snapshot


def test_extracts_all_four_physical_sensors_and_control_value() -> None:
    payload = {
        "cookerId": "not-returned-to-caller",
        "state": {
            "updatedTimestamp": "2026-08-20T12:34:56.789Z",
            "nodes": {
                "temperatureBulbs": {
                    "dryTop": {
                        "current": {"celsius": 31.25},
                        "ntcConnected": True,
                    },
                    "dryBottom": {
                        "current": {"celsius": 30.5},
                        "ntcConnected": True,
                    },
                    "wet": {
                        "current": {"celsius": 28.0},
                        "ntcConnected": True,
                    },
                    "dry": {"current": {"celsius": 31.0}},
                },
                "temperatureProbe": {
                    "connected": True,
                    "ntcConnected": True,
                    "current": {"celsius": 22.75},
                },
            },
            "state": {"mode": "cooking"},
        },
    }
    snapshot = extract_temperature_snapshot(payload)
    assert snapshot.top.celsius == 31.25
    assert snapshot.bottom.celsius == 30.5
    assert snapshot.wet_bulb.celsius == 28.0
    assert snapshot.probe.celsius == 22.75
    assert snapshot.probe_connected is True
    assert snapshot.probe.sensor_connected is True
    assert snapshot.dry_control.celsius == 31.0
    assert snapshot.oven_mode == "cooking"
    assert snapshot.top.fahrenheit == 88.25
    assert snapshot.measured_at == datetime(2026, 8, 20, 12, 34, 56, 789000, tzinfo=UTC)


def test_disconnected_probe_is_explicitly_unavailable() -> None:
    snapshot = extract_temperature_snapshot(
        {
            "state": {
                "nodes": {
                    "temperatureBulbs": {},
                    "temperatureProbe": {
                        "connected": False,
                        "ntcConnected": True,
                    },
                }
            }
        }
    )
    assert snapshot.probe.celsius is None
    assert snapshot.probe.available is False
    assert snapshot.probe_connected is False
    assert snapshot.probe.sensor_connected is False


@pytest.mark.parametrize(
    ("connected", "ntc_connected", "celsius", "expected_connected"),
    [
        (True, True, 25.0, True),
        (True, None, 25.0, True),
        (None, True, 25.0, True),
        (False, True, 25.0, False),
        (True, False, 25.0, False),
        (None, None, 25.0, True),
        (None, None, None, False),
    ],
)
def test_probe_reading_combines_connected_and_ntc_flags(
    connected: bool | None,
    ntc_connected: bool | None,
    celsius: float | None,
    expected_connected: bool,
) -> None:
    probe: dict[str, object] = {}
    if connected is not None:
        probe["connected"] = connected
    if ntc_connected is not None:
        probe["ntcConnected"] = ntc_connected
    if celsius is not None:
        probe["current"] = {"celsius": celsius}

    snapshot = extract_temperature_snapshot(
        {"state": {"nodes": {"temperatureProbe": probe}}}
    )

    assert snapshot.probe_connected is expected_connected
    assert snapshot.probe.sensor_connected is expected_connected
    assert snapshot.probe.celsius == (celsius if expected_connected else None)


def test_state_timestamp_with_offset_is_normalized_to_utc() -> None:
    snapshot = extract_temperature_snapshot(
        {
            "state": {
                "updatedTimestamp": "2026-08-20T05:34:56-07:00",
                "nodes": {},
            }
        }
    )

    assert snapshot.measured_at == datetime(2026, 8, 20, 12, 34, 56, tzinfo=UTC)


@pytest.mark.parametrize("updated_timestamp", [None, "", "not-a-timestamp"])
def test_invalid_or_missing_state_timestamp_falls_back_to_current_utc(
    updated_timestamp: str | None,
) -> None:
    state: dict[str, object] = {"nodes": {}}
    if updated_timestamp is not None:
        state["updatedTimestamp"] = updated_timestamp
    before = datetime.now(UTC)

    snapshot = extract_temperature_snapshot({"state": state})

    after = datetime.now(UTC)
    assert before <= snapshot.measured_at <= after
    assert snapshot.measured_at.tzinfo is UTC
