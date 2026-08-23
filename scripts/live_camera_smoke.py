#!/usr/bin/env python3
"""Guarded live camera check that always stops its test cook."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from anova_oven import PrecisionOvenClient


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--acknowledge-empty-oven-and-start-cook",
        action="store_true",
        help="Required: confirms the empty oven may run at 25°C for up to three minutes.",
    )
    parser.add_argument("--output", type=Path, default=Path("oven-camera-smoke.jpg"))
    return parser.parse_args()


async def run(output: Path) -> None:
    async with PrecisionOvenClient() as oven:
        snapshot = await oven.get_temperatures()
        if snapshot.oven_mode not in {None, "idle", "off"}:
            raise SystemExit(
                f"Refusing to replace a cook while oven mode is {snapshot.oven_mode!r}."
            )

        start_attempted = False
        try:
            start_attempted = True
            await oven.start_simple_cook(
                target_celsius=25,
                duration_seconds=180,
                title="Camera smoke test",
            )
            # A fresh observer connection receives an authoritative initial
            # state even when the command connection has not emitted its next
            # periodic state event yet.
            await asyncio.sleep(5)
            async with PrecisionOvenClient() as observer:
                live_snapshot = await observer.get_temperatures()
            if live_snapshot.oven_mode in {None, "idle", "off"}:
                raise RuntimeError("A fresh oven connection did not report an active cook.")
            frame = await oven.capture_frame(timeout=60, jpeg_quality=85)
            await asyncio.to_thread(output.write_bytes, frame.jpeg_bytes)
            resolved_output = await asyncio.to_thread(output.resolve)
            print(f"captured={resolved_output} width={frame.width} height={frame.height}")
        finally:
            if start_attempted:
                stop_task = asyncio.create_task(oven.stop_cook())
                try:
                    await asyncio.wait_for(asyncio.shield(stop_task), timeout=25)
                except asyncio.CancelledError:
                    # A first cancellation must not cancel the physical stop.
                    await asyncio.wait_for(asyncio.shield(stop_task), timeout=25)
                    raise


def main() -> None:
    args = arguments()
    if not args.acknowledge_empty_oven_and_start_cook:
        raise SystemExit(
            "Refusing to start the oven without --acknowledge-empty-oven-and-start-cook."
        )
    asyncio.run(run(args.output))


if __name__ == "__main__":
    main()
