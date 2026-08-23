#!/usr/bin/env python3
"""Read-only live discovery and temperature check; emits no full device IDs."""

from __future__ import annotations

import asyncio
import json

from anova_oven import PrecisionOvenClient


async def main() -> None:
    async with PrecisionOvenClient() as oven:
        devices = await oven.list_devices()
        print(f"paired_oven_count={len(devices)}")
        for device in devices:
            print(
                json.dumps(
                    {
                        "name": device.name,
                        "generation": device.generation.value,
                        "redacted_id": device.redacted_id,
                        "paired_at": device.paired_at,
                    },
                    indent=2,
                )
            )
        if not devices:
            return
        print(json.dumps((await oven.get_temperatures()).as_dict(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
