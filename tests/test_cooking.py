from __future__ import annotations

import pytest

from anova_oven.cooking import encode_cook_plan, encode_stage_v1, encode_stage_v2
from anova_oven.exceptions import InvalidCookPlanError
from anova_oven.models import (
    CookingStage,
    CookPlan,
    HeatingElements,
    OvenGeneration,
    TemperatureMode,
    TimerStart,
)


def test_v2_timed_stage_uses_timer_completion() -> None:
    encoded = encode_stage_v2(CookingStage(target_celsius=180, duration_seconds=90))
    assert encoded["do"]["timer"] == {"initial": 90}
    assert encoded["exit"]["conditions"]["and"] == {"nodes.timer.mode": {"=": "completed"}}
    assert "entry" not in encoded
    assert encoded["do"]["exhaustVent"]["state"] == "closed"


def test_when_preheated_condition_is_duplicated_for_v2_timer() -> None:
    encoded = encode_stage_v2(
        CookingStage(
            target_celsius=54.4,
            duration_seconds=300,
            timer_start=TimerStart.WHEN_PREHEATED,
        )
    )
    expected = {"nodes.temperatureBulbs.dry.current.celsius": {">=": 54.4}}
    assert encoded["entry"]["conditions"]["and"] == expected
    assert encoded["do"]["timer"]["entry"]["conditions"]["and"] == expected


def test_manual_timer_and_manual_transition_compile_to_conditions() -> None:
    encoded = encode_stage_v2(
        CookingStage(
            target_celsius=100,
            duration_seconds=60,
            timer_start=TimerStart.MANUAL,
            manual_transition=True,
        )
    )
    assert encoded["do"]["timer"]["entry"]["conditions"]["and"] == {"userAction": {"=": True}}
    assert encoded["exit"]["conditions"]["and"] == {
        "nodes.timer.mode": {"=": "completed"},
        "userAction": {"=": True},
    }
    assert "userActionRequired" not in encoded


def test_unbounded_stage_has_no_exit_condition() -> None:
    encoded = encode_stage_v2(CookingStage(target_celsius=25))
    assert "timer" not in encoded["do"]
    assert encoded["exit"]["conditions"]["and"] == {}


def test_probe_stage_waits_for_connection_and_exits_on_temperature() -> None:
    encoded = encode_stage_v2(CookingStage(target_celsius=100, probe_target_celsius=65))
    assert encoded["entry"]["conditions"]["and"] == {
        "nodes.temperatureProbe.connected": {"=": True}
    }
    assert encoded["exit"]["conditions"]["and"] == {
        "nodes.temperatureProbe.current.celsius": {">=": 65}
    }


def test_full_v2_plan_has_required_metadata() -> None:
    plan = CookPlan(stages=(CookingStage(target_celsius=25, duration_seconds=1),))
    encoded = encode_cook_plan(
        plan,
        generation=OvenGeneration.V2,
        device_id="secret-device-id",
    )
    assert encoded["cookerId"] == "secret-device-id"
    assert encoded["type"] == "oven_v2"
    assert encoded["originSource"] == "android"
    assert encoded["cookableType"] == "manual"
    assert len(encoded["stages"]) == 1


def test_v1_immediate_timer_omits_unsupported_start_type() -> None:
    immediate = encode_stage_v1(CookingStage(target_celsius=100, duration_seconds=60))
    preheated = encode_stage_v1(
        CookingStage(
            target_celsius=100,
            duration_seconds=60,
            timer_start=TimerStart.WHEN_PREHEATED,
        )
    )

    assert immediate["timer"] == {"initial": 60}
    assert preheated["timer"]["startType"] == "when-preheated"


@pytest.mark.parametrize("duration", [0, 359_941])
def test_timer_range_is_enforced(duration: int) -> None:
    with pytest.raises(InvalidCookPlanError):
        CookingStage(target_celsius=100, duration_seconds=duration).validate()


def test_wet_and_heating_element_limits_are_enforced() -> None:
    with pytest.raises(InvalidCookPlanError):
        CookingStage(
            target_celsius=101,
            temperature_mode=TemperatureMode.WET,
        ).validate()
    with pytest.raises(InvalidCookPlanError):
        CookingStage(
            target_celsius=100,
            heating_elements=HeatingElements(top=True, bottom=True, rear=True),
        ).validate()
