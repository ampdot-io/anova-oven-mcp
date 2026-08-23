"""Thin MCP 2.x wrapper around the reusable oven client."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Image
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from anova_oven import (
    CommandReceipt,
    CookingStage,
    CookPlan,
    HeatingElements,
    PrecisionOvenClient,
    SteamMode,
    TemperatureMode,
    TimerStart,
)


class HeatingElementsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    top: bool = False
    bottom: bool = False
    rear: bool = True

    def to_library(self) -> HeatingElements:
        return HeatingElements(top=self.top, bottom=self.bottom, rear=self.rear)


class CookingStageInput(BaseModel):
    """JSON-friendly cooking stage accepted by MCP tools."""

    model_config = ConfigDict(extra="forbid")

    target_celsius: Annotated[float, Field(ge=25, le=250)]
    duration_seconds: Annotated[int | None, Field(ge=1, le=359_940)] = None
    probe_target_celsius: Annotated[float | None, Field(ge=1, le=100)] = None
    title: Annotated[str, Field(max_length=120)] = ""
    description: Annotated[str, Field(max_length=500)] = ""
    temperature_mode: Literal["dry", "wet"] = "dry"
    timer_start: Literal["immediately", "when-preheated", "manual"] = "immediately"
    manual_transition: bool = False
    fan_speed: Annotated[int, Field(ge=0, le=100)] = 100
    heating_elements: HeatingElementsInput = Field(default_factory=HeatingElementsInput)
    steam_mode: Literal["off", "relative-humidity", "steam-percentage"] = "off"
    steam_percent: Annotated[int, Field(ge=0, le=100)] = 0
    vent_open: bool = False
    rack_position: Annotated[int, Field(ge=1, le=5)] = 3

    def to_library(self) -> CookingStage:
        return CookingStage(
            target_celsius=self.target_celsius,
            duration_seconds=self.duration_seconds,
            probe_target_celsius=self.probe_target_celsius,
            title=self.title,
            description=self.description,
            temperature_mode=TemperatureMode(self.temperature_mode),
            timer_start=TimerStart(self.timer_start),
            manual_transition=self.manual_transition,
            fan_speed=self.fan_speed,
            heating_elements=self.heating_elements.to_library(),
            steam_mode=SteamMode(self.steam_mode),
            steam_percent=self.steam_percent,
            vent_open=self.vent_open,
            rack_position=self.rack_position,
        )


class TemperatureReadingOutput(BaseModel):
    celsius: float | None
    fahrenheit: float | None
    available: bool
    sensor_connected: bool | None


class TemperatureOutput(BaseModel):
    top: TemperatureReadingOutput
    bottom: TemperatureReadingOutput
    wet_bulb: TemperatureReadingOutput
    probe: TemperatureReadingOutput
    probe_connected: bool
    dry_control: TemperatureReadingOutput
    oven_mode: str | None
    measured_at: str


class CommandOutput(BaseModel):
    command: str
    request_id: str
    acknowledged: bool
    detail: str
    stage_ids: list[str]


class DeviceOutput(BaseModel):
    name: str
    generation: str
    redacted_id: str
    paired_at: str | None


class UTCTimeOutput(BaseModel):
    hours: int
    minutes: int
    seconds: int


@dataclass(slots=True)
class AppContext:
    oven: PrecisionOvenClient


ClientFactory = Callable[[], PrecisionOvenClient]


def _command_output(receipt: CommandReceipt) -> CommandOutput:
    return CommandOutput(
        command=receipt.command,
        request_id=receipt.request_id,
        acknowledged=receipt.acknowledged,
        detail=receipt.detail,
        stage_ids=list(receipt.stage_ids),
    )


def _oven(ctx: Context[AppContext, object]) -> PrecisionOvenClient:
    return ctx.request_context.lifespan_context.oven


def _require_acknowledgement(acknowledge_physical_action: bool) -> None:
    if not acknowledge_physical_action:
        raise ValueError(
            "Set acknowledge_physical_action=true only after the user approves the exact "
            "temperature, duration/probe target, steam, fan, and stage settings."
        )


def create_server(client_factory: ClientFactory = PrecisionOvenClient) -> MCPServer[AppContext]:
    """Create an MCP server; dependency injection keeps it testable/portable."""

    @asynccontextmanager
    async def lifespan(_server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
        oven = client_factory()
        async with oven:
            yield AppContext(oven=oven)

    server = MCPServer(
        name="anova-precision-oven",
        title="Anova Precision Oven",
        description="Read and control a paired Anova Precision Oven, including APO 2.0 live video.",
        instructions=(
            "Camera frames require an APO 2.0, an active cook, compatible firmware, and an "
            "eligible Anova subscription. Starting or changing a cook is a physical action: "
            "obtain the user's approval for the exact settings and pass "
            "acknowledge_physical_action=true. Never retry an unacknowledged start command "
            "without first reading oven state."
        ),
        lifespan=lifespan,
    )

    @server.tool(
        name="oven_list_devices",
        description="List ovens paired to the authenticated Anova account; IDs are redacted.",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def oven_list_devices(ctx: Context[AppContext, object]) -> list[DeviceOutput]:
        devices = await _oven(ctx).list_devices()
        return [
            DeviceOutput(
                name=device.name,
                generation=device.generation.value,
                redacted_id=device.redacted_id,
                paired_at=device.paired_at,
            )
            for device in devices
        ]

    @server.tool(
        name="oven_get_temperatures",
        description=(
            "Read all four physical cooking sensors: dry top, dry bottom, wet bulb, and "
            "plug-in food probe. Also returns the active dry control value."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def oven_get_temperatures(
        ctx: Context[AppContext, object],
    ) -> TemperatureOutput:
        snapshot = await _oven(ctx).get_temperatures()
        return TemperatureOutput.model_validate(snapshot.as_dict())

    @server.tool(
        name="oven_get_camera_frame",
        description=(
            "Capture one current JPEG frame from an APO 2.0 live-video stream. The oven "
            "must already be cooking and the Anova account must have camera access."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        structured_output=False,
    )
    async def oven_get_camera_frame(
        ctx: Context[AppContext, object],
        jpeg_quality: Annotated[int, Field(ge=1, le=95)] = 85,
        timeout_seconds: Annotated[float, Field(ge=3, le=60)] = 20,
    ) -> Image:
        frame = await _oven(ctx).capture_frame(timeout=timeout_seconds, jpeg_quality=jpeg_quality)
        return Image(data=frame.jpeg_bytes, format="jpeg")

    @server.tool(
        name="oven_start_cook",
        description=(
            "Start a single-stage cook. duration_seconds=None means run until explicitly "
            "stopped; alternatively set a probe target. This physically activates the oven."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def oven_start_cook(
        stage: CookingStageInput,
        acknowledge_physical_action: bool,
        ctx: Context[AppContext, object],
    ) -> CommandOutput:
        _require_acknowledgement(acknowledge_physical_action)
        receipt = await _oven(ctx).start_cook(CookPlan(stages=(stage.to_library(),)))
        return _command_output(receipt)

    @server.tool(
        name="oven_start_staged_cook",
        description=(
            "Start a cook containing sequential stages. Each stage can use time, probe, "
            "or an explicit stop as its completion condition. This physically activates the oven."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def oven_start_staged_cook(
        stages: Annotated[list[CookingStageInput], Field(min_length=1, max_length=20)],
        acknowledge_physical_action: bool,
        ctx: Context[AppContext, object],
        title: Annotated[str, Field(max_length=120)] = "",
    ) -> CommandOutput:
        _require_acknowledgement(acknowledge_physical_action)
        plan = CookPlan(stages=tuple(stage.to_library() for stage in stages), title=title)
        return _command_output(await _oven(ctx).start_cook(plan))

    @server.tool(
        name="oven_configure_stages",
        description=(
            "Replace every stage in the currently active cook. This immediately changes "
            "future cooking behavior and requires approval of the exact stage list."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    async def oven_configure_stages(
        stages: Annotated[list[CookingStageInput], Field(min_length=1, max_length=20)],
        acknowledge_physical_action: bool,
        ctx: Context[AppContext, object],
    ) -> CommandOutput:
        _require_acknowledgement(acknowledge_physical_action)
        plan = CookPlan(stages=tuple(stage.to_library() for stage in stages))
        return _command_output(await _oven(ctx).configure_stages(plan))

    @server.tool(
        name="oven_start_stage",
        description="Advance an active staged cook to a specific configured stage UUID.",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    async def oven_start_stage(
        stage_id: Annotated[str, Field(pattern=r"^[0-9a-fA-F-]{36}$")],
        acknowledge_physical_action: bool,
        ctx: Context[AppContext, object],
    ) -> CommandOutput:
        _require_acknowledgement(acknowledge_physical_action)
        return _command_output(await _oven(ctx).start_stage(stage_id))

    @server.tool(
        name="oven_stop_cook",
        description="Stop the current cook and close any active camera stream.",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    async def oven_stop_cook(ctx: Context[AppContext, object]) -> CommandOutput:
        return _command_output(await _oven(ctx).stop_cook())

    @server.tool(
        name="get_utc_time",
        description="Return the current UTC time of day as hours, minutes, and seconds.",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def get_utc_time() -> UTCTimeOutput:
        now = datetime.now(UTC)
        return UTCTimeOutput(hours=now.hour, minutes=now.minute, seconds=now.second)

    return server
