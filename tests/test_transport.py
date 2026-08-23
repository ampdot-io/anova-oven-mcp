from __future__ import annotations

import asyncio
import json

import pytest

from anova_oven.exceptions import CommandError, ConnectionError
from anova_oven.models import OvenGeneration
from anova_oven.transport import OvenCloudTransport


class StubTokenProvider:
    async def get_id_token(self, *, force_refresh: bool = False) -> str:
        return "not-used"


async def wait_for_event(event: asyncio.Event) -> None:
    await event.wait()


class AcknowledgingSocket:
    def __init__(self, transport: OvenCloudTransport) -> None:
        self.transport = transport
        self.sent: list[dict[str, object]] = []

    async def send(self, raw: str) -> None:
        envelope = json.loads(raw)
        self.sent.append(envelope)
        self.transport._handle_message(
            {
                "command": "RESPONSE",
                "requestId": envelope["requestId"],
                "payload": {"status": "ok"},
            }
        )

    async def close(self) -> None:
        return


class ResponseSocket(AcknowledgingSocket):
    def __init__(
        self,
        transport: OvenCloudTransport,
        payload: object,
        *,
        outer_error: object | None = None,
    ) -> None:
        super().__init__(transport)
        self.payload = payload
        self.outer_error = outer_error

    async def send(self, raw: str) -> None:
        envelope = json.loads(raw)
        self.sent.append(envelope)
        response = {
            "command": "RESPONSE",
            "requestId": envelope["requestId"],
            "payload": self.payload,
        }
        if self.outer_error is not None:
            response["error"] = self.outer_error
        self.transport._handle_message(response)


class ClosingSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = messages

    def __aiter__(self):  # type: ignore[no-untyped-def]
        async def messages():  # type: ignore[no-untyped-def]
            for message in self.messages:
                yield json.dumps(message)

        return messages()

    async def close(self) -> None:
        return


@pytest.mark.asyncio
async def test_device_discovery_and_exact_command_envelope() -> None:
    transport = OvenCloudTransport(StubTokenProvider())  # type: ignore[arg-type]
    blocker = asyncio.Event()
    transport._receiver_task = asyncio.create_task(wait_for_event(blocker))
    socket = AcknowledgingSocket(transport)
    transport._socket = socket
    transport._handle_message(
        {
            "command": "EVENT_APO_WIFI_LIST",
            "payload": [
                {
                    "cookerId": "device-secret",
                    "name": "Kitchen Oven",
                    "type": "oven_v2",
                    "pairedAt": "2026-01-01T00:00:00Z",
                }
            ],
        }
    )
    device = await transport.select_device()
    assert device.generation is OvenGeneration.V2
    transport._state_times[device.id] = 123.0
    request_id, _ = await transport.send_command("CMD_APO_STOP", device_id=device.id)
    assert socket.sent == [
        {
            "command": "CMD_APO_STOP",
            "requestId": request_id,
            "payload": {"id": "device-secret", "type": "CMD_APO_STOP"},
        }
    ]
    assert device.id not in transport._state_times
    await transport.aclose()


@pytest.mark.asyncio
async def test_only_real_response_resolves_pending_command() -> None:
    transport = OvenCloudTransport(StubTokenProvider())  # type: ignore[arg-type]
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    request_id = "pending-command"
    transport._pending[request_id] = future  # type: ignore[assignment]

    transport._handle_message(
        {
            "command": "EVENT_APO_STATE",
            "requestId": request_id,
            "payload": {
                "cookerId": "device-secret",
                "state": {"state": {"processedCommandIds": [request_id]}},
            },
        }
    )
    assert not future.done()

    transport._handle_message(
        {
            "command": "EVENT_UNRELATED",
            "requestId": request_id,
            "payload": {"status": "ok"},
        }
    )
    assert not future.done()

    response = {
        "command": "RESPONSE",
        "requestId": request_id,
        "payload": {"status": "ok"},
    }
    transport._handle_message(response)
    assert await future == response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"status": None},
        {"status": True},
        {"status": "OK"},
        {"status": "failed"},
    ],
)
async def test_command_rejects_every_non_exact_ok_status(payload: object) -> None:
    transport = OvenCloudTransport(StubTokenProvider())  # type: ignore[arg-type]
    blocker = asyncio.Event()
    transport._receiver_task = asyncio.create_task(wait_for_event(blocker))
    transport._socket = ResponseSocket(transport, payload)

    with pytest.raises(CommandError, match="exact successful acknowledgement"):
        await transport.send_command("CMD_APO_STOP", device_id="device-secret")

    await transport.aclose()


@pytest.mark.asyncio
async def test_command_rejects_error_even_with_ok_status() -> None:
    transport = OvenCloudTransport(StubTokenProvider())  # type: ignore[arg-type]
    blocker = asyncio.Event()
    transport._receiver_task = asyncio.create_task(wait_for_event(blocker))
    transport._socket = ResponseSocket(
        transport,
        {"status": "ok"},
        outer_error="rejected",
    )

    with pytest.raises(CommandError, match="rejected"):
        await transport.send_command("CMD_APO_STOP", device_id="device-secret")

    await transport.aclose()


@pytest.mark.asyncio
async def test_disconnect_clears_cached_state_and_wakes_waiters() -> None:
    transport = OvenCloudTransport(StubTokenProvider())  # type: ignore[arg-type]
    old_event = asyncio.Event()
    transport._states["device-secret"] = {"cookerId": "device-secret"}
    transport._state_times["device-secret"] = 123.0
    transport._state_events["device-secret"] = old_event
    transport._socket = ClosingSocket([])

    await transport._receive_loop()

    assert old_event.is_set()
    assert transport._states == {}
    assert transport._state_times == {}
    assert transport._state_events == {}


@pytest.mark.asyncio
async def test_reconnect_cannot_return_state_from_previous_session() -> None:
    transport = OvenCloudTransport(StubTokenProvider())  # type: ignore[arg-type]
    first_blocker = asyncio.Event()
    transport._receiver_task = asyncio.create_task(wait_for_event(first_blocker))
    transport._socket = AcknowledgingSocket(transport)
    transport._handle_message(
        {
            "command": "EVENT_APO_STATE",
            "payload": {
                "cookerId": "device-secret",
                "state": {"updatedTimestamp": "2026-08-20T12:00:00Z"},
            },
        }
    )

    await transport._discard_connection()
    second_blocker = asyncio.Event()
    transport._receiver_task = asyncio.create_task(wait_for_event(second_blocker))
    transport._socket = AcknowledgingSocket(transport)

    with pytest.raises(ConnectionError, match="No current state event"):
        await transport.get_state(
            "device-secret",
            fresh_within_seconds=10_000,
            timeout=0.01,
        )

    await transport.aclose()
