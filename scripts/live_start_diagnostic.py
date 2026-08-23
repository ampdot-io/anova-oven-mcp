#!/usr/bin/env python3
"""Guarded, redacted APO 2.0 cook-start state diagnostic."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from anova_oven import CookingStage, CookPlan, HeatingElements, PrecisionOvenClient
from anova_oven.cooking import encode_cook_plan


def dig(value: Any, *path: str) -> Any:
    current = value
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def state_summary(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.get("state", {})
    nodes = state.get("nodes", {}) if isinstance(state, dict) else {}
    cook = state.get("cook") if isinstance(state, dict) else None
    return {
        "mode": dig(state, "state", "mode"),
        "cook_present": isinstance(cook, dict),
        "active_stage_mode": dig(cook, "activeStageMode"),
        "active_stage_index": dig(cook, "activeStageIndex"),
        "stage_count": len(cook.get("stages", [])) if isinstance(cook, dict) else 0,
        "timer_mode": dig(nodes, "timer", "mode"),
        "timer_initial": dig(nodes, "timer", "initial"),
        "dry_setpoint_celsius": dig(
            nodes, "temperatureBulbs", "dry", "setpoint", "celsius"
        ),
        "heating": {
            "top": dig(nodes, "heatingElements", "top", "on"),
            "bottom": dig(nodes, "heatingElements", "bottom", "on"),
            "rear": dig(nodes, "heatingElements", "rear", "on"),
        },
    }


async def run() -> None:
    async with PrecisionOvenClient() as oven:
        before = await oven.get_temperatures()
        if before.oven_mode not in {None, "idle", "off"}:
            raise SystemExit(f"Refusing diagnostic while mode is {before.oven_mode!r}.")

        device = await oven._device()
        plan = oven._materialize_plan(
            CookPlan(
                stages=(
                    CookingStage(
                        target_celsius=25,
                        duration_seconds=60,
                        fan_speed=75,
                        heating_elements=HeatingElements(
                            top=False,
                            bottom=True,
                            rear=True,
                        ),
                    ),
                )
            )
        )
        payload = encode_cook_plan(
            plan,
            generation=device.generation,
            device_id=device.id,
        )

        start_attempted = False
        try:
            start_attempted = True
            _, response = await oven._transport.send_command(
                "CMD_APO_START",
                device_id=device.id,
                payload=payload,
            )
            response_payload = response.get("payload")
            response_data = response.get("data")
            print(
                json.dumps(
                    {
                        "response_command": response.get("command"),
                        "outer_error_present": bool(response.get("error")),
                        "payload_status": (
                            response_payload.get("status")
                            if isinstance(response_payload, dict)
                            else None
                        ),
                        "payload_error_present": bool(
                            response_payload.get("error")
                            if isinstance(response_payload, dict)
                            else False
                        ),
                        "data_keys": (
                            sorted(str(key) for key in response_data)
                            if isinstance(response_data, dict)
                            else []
                        ),
                    }
                )
            )
            for elapsed in (2, 4, 6, 8, 10):
                await asyncio.sleep(2)
                current_state = state_summary(
                    await oven.get_raw_state(fresh_within_seconds=0, timeout=45)
                )
                print(json.dumps({"elapsed_seconds": elapsed, **current_state}))
        finally:
            if start_attempted:
                stop_task = asyncio.create_task(oven.stop_cook())
                await asyncio.wait_for(asyncio.shield(stop_task), timeout=25)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acknowledge-empty-oven", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_empty_oven:
        raise SystemExit("Refusing to run without --acknowledge-empty-oven.")
    asyncio.run(run())


if __name__ == "__main__":
    main()
