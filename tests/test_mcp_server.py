from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO

import pytest
from mcp import Client
from mcp.types import ImageContent
from PIL import Image as PILImage

from anova_oven.models import (
    CameraFrame,
    CommandReceipt,
    OvenDevice,
    OvenGeneration,
    TemperatureReading,
    TemperatureSnapshot,
)
from anova_oven_mcp import create_server


class FakeOven:
    def __init__(self) -> None:
        self.started_plans = []
        self.updated_plans = []
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        self.entered = False

    async def list_devices(self):
        return (
            OvenDevice(
                id="sensitive-device-identifier",
                name="Test Oven",
                generation=OvenGeneration.V2,
            ),
        )

    async def get_temperatures(self) -> TemperatureSnapshot:
        return TemperatureSnapshot(
            top=TemperatureReading(25.0, True),
            bottom=TemperatureReading(24.5, True),
            wet_bulb=TemperatureReading(23.0, True),
            probe=TemperatureReading(20.0, True),
            probe_connected=True,
            dry_control=TemperatureReading(24.8),
            oven_mode="idle",
            measured_at=datetime.now(UTC),
        )

    async def capture_frame(self, **_kwargs: object) -> CameraFrame:
        buffer = BytesIO()
        PILImage.new("RGB", (4, 3), color="navy").save(buffer, format="JPEG")
        return CameraFrame(
            jpeg_bytes=buffer.getvalue(),
            width=4,
            height=3,
            captured_at=datetime.now(UTC),
        )

    async def start_cook(self, plan):
        self.started_plans.append(plan)
        return CommandReceipt("CMD_APO_START", "request-1", True, "accepted")

    async def configure_stages(self, plan):
        self.updated_plans.append(plan)
        return CommandReceipt("CMD_APO_UPDATE_COOK_STAGES", "request-2", True, "accepted")

    async def start_stage(self, _stage_id: str):
        return CommandReceipt("CMD_APO_START_STAGE", "request-3", True, "accepted")

    async def stop_cook(self):
        return CommandReceipt("CMD_APO_STOP", "request-4", True, "stopped")


@pytest.mark.asyncio
async def test_mcp_contracts_structured_data_image_and_safety_gate() -> None:
    fake = FakeOven()
    server = create_server(lambda: fake)  # type: ignore[arg-type]
    async with Client(server) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {
            "oven_get_camera_frame",
            "oven_get_temperatures",
            "oven_start_cook",
            "oven_stop_cook",
            "oven_configure_stages",
            "get_utc_time",
        } <= names

        devices = await client.call_tool("oven_list_devices", {})
        assert devices.is_error is False
        serialized = str(devices.structured_content)
        assert "sensitive-device-identifier" not in serialized
        assert "sens…fier" in serialized

        temperatures = await client.call_tool("oven_get_temperatures", {})
        assert temperatures.is_error is False
        assert temperatures.structured_content["top"]["celsius"] == 25.0
        assert temperatures.structured_content["probe_connected"] is True

        frame = await client.call_tool("oven_get_camera_frame", {})
        assert frame.is_error is False
        assert isinstance(frame.content[0], ImageContent)
        assert frame.content[0].mime_type == "image/jpeg"

        refused = await client.call_tool(
            "oven_start_cook",
            {
                "stage": {"target_celsius": 25, "duration_seconds": 30},
                "acknowledge_physical_action": False,
            },
        )
        assert refused.is_error is True
        assert fake.started_plans == []

        started = await client.call_tool(
            "oven_start_cook",
            {
                "stage": {"target_celsius": 25, "duration_seconds": 30},
                "acknowledge_physical_action": True,
            },
        )
        assert started.is_error is False
        assert started.structured_content["command"] == "CMD_APO_START"
        assert len(fake.started_plans) == 1

        utc = await client.call_tool("get_utc_time", {})
        assert utc.is_error is False
        assert 0 <= utc.structured_content["hours"] <= 23
        assert 0 <= utc.structured_content["minutes"] <= 59
        assert 0 <= utc.structured_content["seconds"] <= 59
