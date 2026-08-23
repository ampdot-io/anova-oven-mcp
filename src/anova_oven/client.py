"""High-level reusable Precision Oven client."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, Self
from uuid import uuid4

from .auth import FirebaseTokenProvider
from .camera import CameraSession
from .cooking import encode_cook_plan, encode_stage_update
from .credentials import RefreshTokenStore, default_refresh_token_store
from .models import (
    CameraFrame,
    CommandReceipt,
    CookingStage,
    CookPlan,
    HeatingElements,
    OvenDevice,
    SteamMode,
    TemperatureMode,
    TemperatureSnapshot,
    TimerStart,
)
from .telemetry import extract_temperature_snapshot
from .transport import OvenCloudTransport


class PrecisionOvenClient:
    """Async facade over authentication, telemetry, cooks, and camera frames."""

    def __init__(
        self,
        *,
        refresh_token_store: RefreshTokenStore | None = None,
        device_id: str | None = None,
        transport: OvenCloudTransport | None = None,
    ) -> None:
        self._configured_device_id = device_id or os.environ.get("ANOVA_OVEN_ID")
        self._token_provider: FirebaseTokenProvider | None = None
        if transport is None:
            self._token_provider = FirebaseTokenProvider(
                refresh_token_store or default_refresh_token_store()
            )
            transport = OvenCloudTransport(self._token_provider)
        self._transport = transport
        self._selected_device: OvenDevice | None = None
        self._device_lock = asyncio.Lock()
        self._camera: CameraSession | None = None
        self._camera_lock = asyncio.Lock()
        self._closed = False

    async def _device(self) -> OvenDevice:
        if self._selected_device is not None:
            return self._selected_device
        async with self._device_lock:
            if self._selected_device is None:
                self._selected_device = await self._transport.select_device(
                    self._configured_device_id
                )
            return self._selected_device

    async def list_devices(self) -> tuple[OvenDevice, ...]:
        return await self._transport.list_devices()

    async def get_raw_state(
        self,
        *,
        fresh_within_seconds: float = 35.0,
        timeout: float = 40.0,
    ) -> dict[str, Any]:
        """Return the latest state event for advanced library consumers."""

        device = await self._device()
        return await self._transport.get_state(
            device.id,
            fresh_within_seconds=fresh_within_seconds,
            timeout=timeout,
        )

    async def get_temperatures(
        self,
        *,
        fresh_within_seconds: float = 35.0,
        timeout: float = 40.0,
    ) -> TemperatureSnapshot:
        device = await self._device()
        state = await self._transport.get_state(
            device.id,
            fresh_within_seconds=fresh_within_seconds,
            timeout=timeout,
        )
        return extract_temperature_snapshot(state)

    @staticmethod
    def _materialize_plan(plan: CookPlan) -> CookPlan:
        stages = tuple(
            stage if stage.id is not None else replace(stage, id=str(uuid4()))
            for stage in plan.stages
        )
        return replace(plan, stages=stages, id=plan.id or str(uuid4()))

    async def start_cook(self, plan: CookPlan) -> CommandReceipt:
        device = await self._device()
        plan = self._materialize_plan(plan)
        inner_payload = encode_cook_plan(plan, generation=device.generation, device_id=device.id)
        request_id, _ = await self._transport.send_command(
            "CMD_APO_START", device_id=device.id, payload=inner_payload
        )
        return CommandReceipt(
            command="CMD_APO_START",
            request_id=request_id,
            acknowledged=True,
            detail=f"Anova accepted a {len(plan.stages)}-stage cook.",
            stage_ids=tuple(stage.id for stage in plan.stages if stage.id is not None),
        )

    async def start_simple_cook(
        self,
        *,
        target_celsius: float,
        duration_seconds: int | None = None,
        probe_target_celsius: float | None = None,
        title: str = "",
        temperature_mode: TemperatureMode = TemperatureMode.DRY,
        timer_start: TimerStart = TimerStart.IMMEDIATELY,
        fan_speed: int = 100,
        heating_elements: HeatingElements | None = None,
        steam_mode: SteamMode = SteamMode.OFF,
        steam_percent: int = 0,
        vent_open: bool = False,
        rack_position: int = 3,
    ) -> CommandReceipt:
        stage = CookingStage(
            target_celsius=target_celsius,
            duration_seconds=duration_seconds,
            probe_target_celsius=probe_target_celsius,
            title=title,
            temperature_mode=temperature_mode,
            timer_start=timer_start,
            fan_speed=fan_speed,
            heating_elements=heating_elements or HeatingElements(),
            steam_mode=steam_mode,
            steam_percent=steam_percent,
            vent_open=vent_open,
            rack_position=rack_position,
        )
        return await self.start_cook(CookPlan(stages=(stage,), title=title))

    async def configure_stages(self, plan: CookPlan) -> CommandReceipt:
        """Replace the stage list of the currently active cook."""

        device = await self._device()
        plan = self._materialize_plan(plan)
        inner_payload = encode_stage_update(plan, generation=device.generation, device_id=device.id)
        request_id, _ = await self._transport.send_command(
            "CMD_APO_UPDATE_COOK_STAGES",
            device_id=device.id,
            payload=inner_payload,
        )
        return CommandReceipt(
            command="CMD_APO_UPDATE_COOK_STAGES",
            request_id=request_id,
            acknowledged=True,
            detail=f"Anova accepted {len(plan.stages)} replacement cook stages.",
            stage_ids=tuple(stage.id for stage in plan.stages if stage.id is not None),
        )

    async def start_stage(self, stage_id: str) -> CommandReceipt:
        device = await self._device()
        request_id, _ = await self._transport.send_command(
            "CMD_APO_START_STAGE",
            device_id=device.id,
            payload={"stageId": stage_id},
        )
        return CommandReceipt(
            command="CMD_APO_START_STAGE",
            request_id=request_id,
            acknowledged=True,
            detail="Anova accepted the stage transition.",
        )

    async def stop_cook(self) -> CommandReceipt:
        device = await self._device()
        try:
            request_id, _ = await self._transport.send_command(
                "CMD_APO_STOP", device_id=device.id
            )
        finally:
            # Camera teardown can involve a WHEP request, WebRTC shutdown, and a
            # separate live-stream command. None of those should delay sending
            # the safety-critical cook stop or replace its result with a cleanup
            # failure.
            try:
                await asyncio.wait_for(self.close_camera(), timeout=15.0)
            except Exception:
                pass
        return CommandReceipt(
            command="CMD_APO_STOP",
            request_id=request_id,
            acknowledged=True,
            detail="Anova accepted the stop command.",
        )

    async def _camera_session(self) -> CameraSession:
        if self._camera is not None:
            return self._camera
        async with self._camera_lock:
            if self._camera is None:
                device = await self._device()
                self._camera = CameraSession(self._transport, device.id)
            return self._camera

    async def capture_frame(self, *, timeout: float = 20.0, jpeg_quality: int = 85) -> CameraFrame:
        camera = await self._camera_session()
        try:
            return await camera.next_frame(timeout=timeout, jpeg_quality=jpeg_quality)
        except Exception:
            await self.close_camera()
            raise

    async def frames(
        self,
        *,
        timeout: float = 20.0,
        jpeg_quality: int = 85,
        minimum_interval_seconds: float = 0.0,
    ) -> AsyncIterator[CameraFrame]:
        camera = await self._camera_session()
        async for frame in camera.frames(
            timeout=timeout,
            jpeg_quality=jpeg_quality,
            minimum_interval_seconds=minimum_interval_seconds,
        ):
            yield frame

    async def close_camera(self) -> None:
        async with self._camera_lock:
            camera = self._camera
            self._camera = None
            # Keep session creation serialized through the complete teardown.
            # Otherwise an old session's final STOP_LIVE_STREAM can race with
            # and disable a newly created stream.
            if camera is not None:
                await camera.close()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.close_camera()
        await self._transport.aclose()
        if self._token_provider is not None:
            await self._token_provider.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()
