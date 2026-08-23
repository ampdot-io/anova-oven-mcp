"""Cook-plan validation and Anova command encoding."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .models import (
    CookingStage,
    CookPlan,
    OvenGeneration,
    SteamMode,
    TemperatureMode,
    TimerStart,
    celsius_to_fahrenheit,
)


def _stage_id(stage: CookingStage) -> str:
    return stage.id or str(uuid4())


def _temperature_condition(stage: CookingStage) -> dict[str, dict[str, float]]:
    bulb = "wet" if stage.temperature_mode is TemperatureMode.WET else "dry"
    return {
        f"nodes.temperatureBulbs.{bulb}.current.celsius": {">=": round(stage.target_celsius, 2)}
    }


def _steam_payload(stage: CookingStage) -> dict[str, Any] | None:
    if stage.steam_mode is SteamMode.OFF:
        return None
    if stage.steam_mode is SteamMode.RELATIVE_HUMIDITY:
        return {
            "mode": stage.steam_mode.value,
            "relativeHumidity": {"setpoint": stage.steam_percent},
        }
    return {
        "mode": stage.steam_mode.value,
        "steamPercentage": {"setpoint": stage.steam_percent},
    }


def encode_stage_v2(stage: CookingStage) -> dict[str, Any]:
    stage.validate(OvenGeneration.V2)
    bulb = stage.temperature_mode.value
    action: dict[str, Any] = {
        "type": "cook",
        "fan": {"speed": stage.fan_speed},
        "heatingElements": stage.heating_elements.as_payload(),
        "exhaustVent": {"state": "open-max" if stage.vent_open else "closed"},
        "temperatureBulbs": {
            "mode": bulb,
            bulb: {"setpoint": {"celsius": round(stage.target_celsius, 2)}},
        },
    }
    steam = _steam_payload(stage)
    if steam is not None:
        action["steamGenerators"] = steam

    entry_conditions: dict[str, Any] = {}
    exit_conditions: dict[str, Any] = {}
    if stage.duration_seconds is not None:
        timer: dict[str, Any] = {"initial": stage.duration_seconds}
        if stage.timer_start is TimerStart.WHEN_PREHEATED:
            temperature_condition = _temperature_condition(stage)
            timer["entry"] = {"conditions": {"and": temperature_condition}}
            entry_conditions.update(temperature_condition)
        elif stage.timer_start is TimerStart.MANUAL:
            timer["entry"] = {"conditions": {"and": {"userAction": {"=": True}}}}
        action["timer"] = timer
        exit_conditions["nodes.timer.mode"] = {"=": "completed"}
    elif stage.probe_target_celsius is not None:
        action["temperatureProbe"] = {"setpoint": {"celsius": round(stage.probe_target_celsius, 2)}}
        entry_conditions["nodes.temperatureProbe.connected"] = {"=": True}
        exit_conditions["nodes.temperatureProbe.current.celsius"] = {
            ">=": round(stage.probe_target_celsius, 2)
        }

    if stage.manual_transition:
        exit_conditions["userAction"] = {"=": True}

    encoded: dict[str, Any] = {
        "id": _stage_id(stage),
        "do": action,
        "exit": {"conditions": {"and": exit_conditions}},
        "title": stage.title,
        "description": stage.description,
        "rackPosition": stage.rack_position,
    }
    if entry_conditions:
        encoded["entry"] = {"conditions": {"and": entry_conditions}}
    return encoded


def encode_stage_v1(stage: CookingStage) -> dict[str, Any]:
    stage.validate(OvenGeneration.V1)
    bulb = stage.temperature_mode.value
    target = {
        "celsius": round(stage.target_celsius, 2),
        "fahrenheit": round(celsius_to_fahrenheit(stage.target_celsius), 2),
    }
    payload: dict[str, Any] = {
        "stepType": "stage",
        "id": _stage_id(stage),
        "title": stage.title,
        "description": stage.description,
        "type": "cook",
        "userActionRequired": stage.manual_transition,
        "temperatureBulbs": {"mode": bulb, bulb: {"setpoint": target}},
        "heatingElements": stage.heating_elements.as_payload(),
        "fan": {"speed": stage.fan_speed},
        "vent": {"open": stage.vent_open},
        "rackPosition": stage.rack_position,
        "timerAdded": stage.duration_seconds is not None,
        "probeAdded": stage.probe_target_celsius is not None,
        "stageTransitionType": "manual" if stage.manual_transition else "automatic",
    }
    if stage.duration_seconds is not None:
        timer: dict[str, Any] = {"initial": stage.duration_seconds}
        # Both current oven generations omit startType for an immediate timer.
        # The wire enum contains only manual and when-preheated.
        if stage.timer_start is not TimerStart.IMMEDIATELY:
            timer["startType"] = stage.timer_start.value
        payload["timer"] = timer
    if stage.probe_target_celsius is not None:
        payload["temperatureProbe"] = {
            "setpoint": {
                "celsius": round(stage.probe_target_celsius, 2),
                "fahrenheit": round(celsius_to_fahrenheit(stage.probe_target_celsius), 2),
            }
        }
    steam = _steam_payload(stage)
    if steam is not None:
        payload["steamGenerators"] = steam
    return payload


def encode_cook_plan(
    plan: CookPlan,
    *,
    generation: OvenGeneration,
    device_id: str,
) -> dict[str, Any]:
    plan.validate(generation)
    cook_id = plan.id or str(uuid4())
    if generation is OvenGeneration.V2:
        return {
            "stages": [encode_stage_v2(stage) for stage in plan.stages],
            "cookId": cook_id,
            "cookerId": device_id,
            "cookableId": "",
            "title": plan.title,
            "type": OvenGeneration.V2.value,
            # Camera-capable sessions authenticate like the Android app rather
            # than the PAT-only developer API, so mirror the app's source tag.
            "originSource": "android",
            "cookableType": "manual",
        }
    return {
        "cookId": cook_id,
        "stages": [encode_stage_v1(stage) for stage in plan.stages],
    }


def encode_stage_update(
    plan: CookPlan,
    *,
    generation: OvenGeneration,
    device_id: str,
) -> dict[str, Any]:
    plan.validate(generation)
    encoder = encode_stage_v2 if generation is OvenGeneration.V2 else encode_stage_v1
    return {"stages": [encoder(stage) for stage in plan.stages]}
