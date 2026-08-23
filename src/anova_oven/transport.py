"""Authenticated Anova cloud WebSocket transport."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.typing import Subprotocol

from .auth import FirebaseTokenProvider
from .exceptions import CommandError, ConnectionError, DeviceNotFoundError
from .models import OvenDevice, OvenGeneration

ANOVA_WEBSOCKET_ENDPOINT = "wss://devices.anovaculinary.io"
ANOVA_WEBSOCKET_SUBPROTOCOL = "ANOVA_V2"


def _error_from_response(response: Mapping[str, Any]) -> str | None:
    outer = response.get("error")
    payload = response.get("payload")
    nested = payload.get("error") if isinstance(payload, Mapping) else None
    status = payload.get("status") if isinstance(payload, Mapping) else None
    if outer or nested:
        return "Anova rejected the command."
    if status != "ok":
        return "Anova did not return an exact successful acknowledgement."
    return None


class OvenCloudTransport:
    """Own one connection and correlate command responses by request UUID."""

    def __init__(
        self,
        token_provider: FirebaseTokenProvider,
        *,
        endpoint: str = ANOVA_WEBSOCKET_ENDPOINT,
        platform: str = "android",
        command_timeout: float = 12.0,
    ) -> None:
        self._token_provider = token_provider
        self._endpoint = endpoint.rstrip("/")
        self._platform = platform
        self._command_timeout = command_timeout
        self._socket: Any | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._devices_event = asyncio.Event()
        self._devices: dict[str, OvenDevice] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._state_times: dict[str, float] = {}
        self._state_events: dict[str, asyncio.Event] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._closed = False

    @property
    def connected(self) -> bool:
        return (
            self._socket is not None
            and self._receiver_task is not None
            and not self._receiver_task.done()
        )

    async def connect(self) -> None:
        if self.connected:
            return
        async with self._connect_lock:
            if self.connected:
                return
            if self._closed:
                raise ConnectionError("The Anova transport has already been closed.")
            await self._discard_connection()
            id_token = await self._token_provider.get_id_token()
            query = urlencode(
                {
                    "token": id_token,
                    "supportedAccessories": "APO",
                    "platform": self._platform,
                }
            )
            try:
                self._socket = await websocket_connect(
                    f"{self._endpoint}/?{query}",
                    subprotocols=[Subprotocol(ANOVA_WEBSOCKET_SUBPROTOCOL)],
                    open_timeout=15,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=4 * 1024 * 1024,
                )
            except (OSError, TimeoutError, WebSocketException) as error:
                self._socket = None
                raise ConnectionError(
                    "Could not establish an authenticated Anova cloud connection."
                ) from error
            self._devices_event.clear()
            self._receiver_task = asyncio.create_task(
                self._receive_loop(), name="anova-cloud-receiver"
            )

    async def _receive_loop(self) -> None:
        assert self._socket is not None
        try:
            async for raw_message in self._socket:
                if not isinstance(raw_message, str):
                    continue
                try:
                    message = json.loads(raw_message)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(message, dict):
                    self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, OSError, WebSocketException):
            return
        finally:
            failure = ConnectionError("The Anova cloud connection closed.")
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(failure)
            self._pending.clear()
            self._clear_cached_states()

    def _handle_message(self, message: dict[str, Any]) -> None:
        command = message.get("command")
        payload = message.get("payload")
        if command == "EVENT_APO_WIFI_LIST" and isinstance(payload, list):
            devices: dict[str, OvenDevice] = {}
            for raw_device in payload:
                if not isinstance(raw_device, dict):
                    continue
                device_id = raw_device.get("cookerId")
                raw_type = raw_device.get("type")
                if not isinstance(device_id, str) or raw_type not in {
                    OvenGeneration.V1.value,
                    OvenGeneration.V2.value,
                }:
                    continue
                devices[device_id] = OvenDevice(
                    id=device_id,
                    name=str(raw_device.get("name") or "Anova Precision Oven"),
                    generation=OvenGeneration(raw_type),
                    paired_at=(
                        raw_device.get("pairedAt")
                        if isinstance(raw_device.get("pairedAt"), str)
                        else None
                    ),
                )
            self._devices = devices
            self._devices_event.set()

        if command == "EVENT_APO_STATE" and isinstance(payload, dict):
            device_id = payload.get("cookerId")
            if isinstance(device_id, str):
                self._states[device_id] = payload
                self._state_times[device_id] = time.monotonic()
                self._state_events.setdefault(device_id, asyncio.Event()).set()

        request_id = message.get("requestId")
        if command == "RESPONSE" and isinstance(request_id, str):
            future = self._pending.get(request_id)
            if future is not None and not future.done():
                future.set_result(message)

    async def list_devices(self, *, timeout: float = 12.0) -> tuple[OvenDevice, ...]:
        await self.connect()
        if not self._devices_event.is_set():
            try:
                await asyncio.wait_for(self._devices_event.wait(), timeout)
            except TimeoutError as error:
                raise DeviceNotFoundError("Anova did not return a paired-device list.") from error
        return tuple(self._devices.values())

    async def select_device(
        self, device_id: str | None = None, *, timeout: float = 12.0
    ) -> OvenDevice:
        devices = await self.list_devices(timeout=timeout)
        if device_id is not None:
            for device in devices:
                if device.id == device_id:
                    return device
            raise DeviceNotFoundError("The configured oven is not paired to this account.")
        if not devices:
            raise DeviceNotFoundError(
                "Anova returned no paired ovens for the authenticated account."
            )
        if len(devices) > 1:
            raise DeviceNotFoundError("Multiple ovens are paired; set ANOVA_OVEN_ID to select one.")
        return devices[0]

    async def get_state(
        self,
        device_id: str,
        *,
        fresh_within_seconds: float = 35.0,
        timeout: float = 40.0,
    ) -> dict[str, Any]:
        await self.connect()
        event = self._state_events.setdefault(device_id, asyncio.Event())
        event.clear()
        state = self._states.get(device_id)
        state_time = self._state_times.get(device_id, 0.0)
        if state is not None and time.monotonic() - state_time <= fresh_within_seconds:
            return state
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError as error:
            raise ConnectionError(
                "No current state event arrived from the selected oven."
            ) from error
        state = self._states.get(device_id)
        if state is None:
            raise ConnectionError(
                "The Anova cloud connection closed before current state arrived."
            )
        return state

    async def send_command(
        self,
        command: str,
        *,
        device_id: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[str, dict[str, Any]]:
        await self.connect()
        if self._socket is None:
            raise ConnectionError("The Anova cloud connection is unavailable.")
        request_id = str(uuid4())
        envelope: dict[str, Any] = {
            "command": command,
            "requestId": request_id,
            "payload": {"id": device_id, "type": command},
        }
        if payload is not None:
            envelope["payload"]["payload"] = payload
        # A command can change operating mode, setpoints, timers, and sensors.
        # Do not let the next state read return an event cached before the
        # command was sent.
        self._state_times.pop(device_id, None)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with self._send_lock:
                await self._socket.send(json.dumps(envelope, separators=(",", ":")))
            try:
                response = await asyncio.wait_for(
                    future, timeout if timeout is not None else self._command_timeout
                )
            except TimeoutError as error:
                raise CommandError(
                    f"{command} was sent but not acknowledged; it was not retried."
                ) from error
        finally:
            self._pending.pop(request_id, None)

        error_message = _error_from_response(response)
        if error_message:
            raise CommandError(f"{command}: {error_message}")
        return request_id, response

    def _clear_cached_states(self) -> None:
        for event in tuple(self._state_events.values()):
            event.set()
        self._states.clear()
        self._state_times.clear()
        self._state_events.clear()

    async def _discard_connection(self) -> None:
        self._clear_cached_states()
        task = self._receiver_task
        socket = self._socket
        self._receiver_task = None
        self._socket = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if socket is not None:
            try:
                await socket.close()
            except (OSError, WebSocketException):
                return

    async def aclose(self) -> None:
        self._closed = True
        await self._discard_connection()
