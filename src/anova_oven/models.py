"""Stable, MCP-independent public models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from .exceptions import InvalidCookPlanError


def celsius_to_fahrenheit(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


class OvenGeneration(StrEnum):
    V1 = "oven_v1"
    V2 = "oven_v2"


class TemperatureMode(StrEnum):
    DRY = "dry"
    WET = "wet"


class TimerStart(StrEnum):
    IMMEDIATELY = "immediately"
    WHEN_PREHEATED = "when-preheated"
    MANUAL = "manual"


class SteamMode(StrEnum):
    OFF = "off"
    RELATIVE_HUMIDITY = "relative-humidity"
    STEAM_PERCENTAGE = "steam-percentage"


@dataclass(frozen=True, slots=True)
class OvenDevice:
    id: str = field(repr=False)
    name: str
    generation: OvenGeneration
    paired_at: str | None = None

    @property
    def redacted_id(self) -> str:
        if len(self.id) <= 8:
            return "***"
        return f"{self.id[:4]}…{self.id[-4:]}"


@dataclass(frozen=True, slots=True)
class TemperatureReading:
    celsius: float | None
    sensor_connected: bool | None = None

    @property
    def fahrenheit(self) -> float | None:
        if self.celsius is None:
            return None
        return round(celsius_to_fahrenheit(self.celsius), 2)

    @property
    def available(self) -> bool:
        return self.celsius is not None


@dataclass(frozen=True, slots=True)
class TemperatureSnapshot:
    """The oven's four physical temperature sensors plus control-bulb data."""

    top: TemperatureReading
    bottom: TemperatureReading
    wet_bulb: TemperatureReading
    probe: TemperatureReading
    probe_connected: bool
    dry_control: TemperatureReading
    oven_mode: str | None
    measured_at: datetime

    def as_dict(self) -> dict[str, Any]:
        def reading(value: TemperatureReading) -> dict[str, float | bool | None]:
            return {
                "celsius": value.celsius,
                "fahrenheit": value.fahrenheit,
                "available": value.available,
                "sensor_connected": value.sensor_connected,
            }

        return {
            "top": reading(self.top),
            "bottom": reading(self.bottom),
            "wet_bulb": reading(self.wet_bulb),
            "probe": reading(self.probe),
            "probe_connected": self.probe_connected,
            "dry_control": reading(self.dry_control),
            "oven_mode": self.oven_mode,
            "measured_at": self.measured_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class HeatingElements:
    top: bool = False
    bottom: bool = False
    rear: bool = True

    def validate(self) -> None:
        enabled = (self.top, self.bottom, self.rear)
        if all(enabled) or not any(enabled):
            raise InvalidCookPlanError(
                "At least one, but not all three, heating elements must be enabled."
            )

    def as_payload(self) -> dict[str, dict[str, bool]]:
        return {
            "top": {"on": self.top},
            "bottom": {"on": self.bottom},
            "rear": {"on": self.rear},
        }


@dataclass(frozen=True, slots=True)
class CookingStage:
    """One stage of an oven cook.

    ``duration_seconds=None`` creates an unbounded stage that continues until
    stopped. A probe target may be used instead of a timer.
    """

    target_celsius: float
    duration_seconds: int | None = None
    probe_target_celsius: float | None = None
    title: str = ""
    description: str = ""
    temperature_mode: TemperatureMode = TemperatureMode.DRY
    timer_start: TimerStart = TimerStart.IMMEDIATELY
    manual_transition: bool = False
    fan_speed: int = 100
    heating_elements: HeatingElements = field(default_factory=HeatingElements)
    steam_mode: SteamMode = SteamMode.OFF
    steam_percent: int = 0
    vent_open: bool = False
    rack_position: int = 3
    id: str | None = None

    def validate(self, generation: OvenGeneration = OvenGeneration.V2) -> None:
        self.heating_elements.validate()
        maximum = 100.0 if self.temperature_mode is TemperatureMode.WET else 250.0
        if (
            self.temperature_mode is TemperatureMode.DRY
            and self.heating_elements.bottom
            and not self.heating_elements.top
            and not self.heating_elements.rear
        ):
            maximum = 230.0 if generation is OvenGeneration.V2 else 180.0
        if not 25.0 <= self.target_celsius <= maximum:
            raise InvalidCookPlanError(
                f"Target must be between 25°C and {maximum:g}°C for this mode."
            )
        if self.duration_seconds is not None and not (1 <= self.duration_seconds <= 359_940):
            raise InvalidCookPlanError(
                "Cook duration must be between 1 and 359940 seconds (99h 59m)."
            )
        if self.probe_target_celsius is not None and not (
            1.0 <= self.probe_target_celsius <= 100.0
        ):
            raise InvalidCookPlanError("Probe target must be between 1°C and 100°C.")
        if self.duration_seconds is not None and self.probe_target_celsius is not None:
            raise InvalidCookPlanError(
                "Choose either a timer or a probe completion target, not both."
            )
        if not 0 <= self.fan_speed <= 100:
            raise InvalidCookPlanError("Fan speed must be between 0 and 100 percent.")
        if not 0 <= self.steam_percent <= 100:
            raise InvalidCookPlanError("Steam or humidity must be between 0 and 100.")
        if self.steam_mode is SteamMode.OFF and self.steam_percent != 0:
            raise InvalidCookPlanError("steam_percent must be zero when steam_mode is 'off'.")
        if not 1 <= self.rack_position <= 5:
            raise InvalidCookPlanError("Rack position must be between 1 and 5.")
        if self.id is not None:
            try:
                parsed_id = UUID(self.id)
            except (ValueError, AttributeError) as error:
                raise InvalidCookPlanError("Stage IDs must be UUID strings.") from error
            if parsed_id.version != 4:
                raise InvalidCookPlanError("Stage IDs must be UUID version 4 strings.")


@dataclass(frozen=True, slots=True)
class CookPlan:
    stages: tuple[CookingStage, ...]
    title: str = ""
    id: str | None = None

    def validate(self, generation: OvenGeneration = OvenGeneration.V2) -> None:
        if not self.stages:
            raise InvalidCookPlanError("A cook plan needs at least one stage.")
        if len(self.stages) > 20:
            raise InvalidCookPlanError("A cook plan can contain at most 20 stages.")
        for stage in self.stages:
            stage.validate(generation)
        if self.id is not None:
            try:
                parsed_id = UUID(self.id)
            except (ValueError, AttributeError) as error:
                raise InvalidCookPlanError("Cook IDs must be UUID strings.") from error
            if parsed_id.version != 4:
                raise InvalidCookPlanError("Cook IDs must be UUID version 4 strings.")


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command: str
    request_id: str
    acknowledged: bool
    detail: str
    stage_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CameraFrame:
    jpeg_bytes: bytes = field(repr=False)
    width: int
    height: int
    captured_at: datetime
