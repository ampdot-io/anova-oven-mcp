from __future__ import annotations

import asyncio

from anova_oven.client import PrecisionOvenClient
from anova_oven.models import CookingStage, CookPlan, OvenDevice, OvenGeneration


class FakeTransport:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str, dict | None]] = []
        self.events: list[str] = []

    async def select_device(self, _device_id: str | None = None) -> OvenDevice:
        return OvenDevice(
            id="device-id",
            name="Test Oven",
            generation=OvenGeneration.V2,
        )

    async def send_command(
        self,
        command: str,
        *,
        device_id: str,
        payload: dict | None = None,
        timeout: float | None = None,
    ):
        self.events.append(command)
        self.commands.append((command, device_id, payload))
        return "request-id", {"payload": {"status": "ok"}}

    async def aclose(self) -> None:
        return


class FailingCamera:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def close(self) -> None:
        self.events.append("camera-close")
        raise RuntimeError("simulated camera cleanup failure")


class BlockingCamera:
    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.allow_close.wait()


async def test_client_materializes_and_returns_stage_ids() -> None:
    transport = FakeTransport()
    client = PrecisionOvenClient(transport=transport)  # type: ignore[arg-type]
    try:
        receipt = await client.start_cook(
            CookPlan(stages=(CookingStage(target_celsius=25, duration_seconds=30),))
        )
    finally:
        await client.aclose()
    assert len(receipt.stage_ids) == 1
    stage_id = receipt.stage_ids[0]
    assert transport.commands[0][0] == "CMD_APO_START"
    command_payload = transport.commands[0][2]
    assert command_payload is not None
    assert command_payload["stages"][0]["id"] == stage_id


async def test_stop_cook_is_sent_before_best_effort_camera_cleanup() -> None:
    transport = FakeTransport()
    client = PrecisionOvenClient(transport=transport)  # type: ignore[arg-type]
    client._camera = FailingCamera(transport.events)  # type: ignore[assignment]
    try:
        receipt = await client.stop_cook()
    finally:
        await client.aclose()

    assert receipt.command == "CMD_APO_STOP"
    assert receipt.acknowledged is True
    assert transport.events == ["CMD_APO_STOP", "camera-close"]


async def test_new_camera_session_waits_for_old_session_teardown() -> None:
    transport = FakeTransport()
    client = PrecisionOvenClient(transport=transport)  # type: ignore[arg-type]
    old_camera = BlockingCamera()
    client._camera = old_camera  # type: ignore[assignment]

    closing = asyncio.create_task(client.close_camera())
    await asyncio.wait_for(old_camera.close_started.wait(), timeout=1)
    creating = asyncio.create_task(client._camera_session())
    await asyncio.sleep(0)
    assert not creating.done()

    old_camera.allow_close.set()
    await asyncio.wait_for(closing, timeout=1)
    new_camera = await asyncio.wait_for(creating, timeout=1)
    assert new_camera is not old_camera
    await client.aclose()
