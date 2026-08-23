from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest
from av import VideoFrame
from PIL import Image

import anova_oven.camera as camera_module
from anova_oven.camera import (
    LIVE_STREAM_START,
    LIVE_STREAM_STOP,
    CameraSession,
    _playback_url,
)


class NoCommandTransport:
    async def send_command(self, *args: object, **kwargs: object):
        raise AssertionError("No command should be sent by this test")


class OneFrameTrack:
    async def recv(self) -> VideoFrame:
        return VideoFrame.from_image(Image.new("RGB", (8, 6), color=(20, 30, 40)))


class RecordingTransport:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send_command(
        self,
        command: str,
        *,
        device_id: str,
        payload: dict | None = None,
        timeout: float | None = None,
    ) -> tuple[str, dict]:
        self.commands.append(command)
        if command == LIVE_STREAM_START:
            return (
                "request-id",
                {"data": {"webRTCPlayback": {"url": "https://camera.test/whep"}}},
            )
        return "request-id", {}


class FakeRTCConfiguration:
    def __init__(self, *, iceServers: list[FakeRTCIceServer]) -> None:
        self.ice_servers = iceServers


class FakeRTCIceServer:
    def __init__(self, *, urls: list[str]) -> None:
        self.urls = urls


class FakeRTCSessionDescription:
    def __init__(self, *, sdp: str, type: str) -> None:
        self.sdp = sdp
        self.type = type


class FakeVideoTrack:
    kind = "video"


class FakeRTCPeerConnection:
    offer_sdp = "v=0\r\no=fake-offer\r\n"

    def __init__(self, *, configuration: FakeRTCConfiguration) -> None:
        self.configuration = configuration
        self.localDescription: FakeRTCSessionDescription | None = None
        self._track_handler: Callable[[FakeVideoTrack], None] | None = None

    def on(self, event: str):
        assert event == "track"

        def register(handler: Callable[[FakeVideoTrack], None]):
            self._track_handler = handler
            return handler

        return register

    def addTransceiver(self, kind: str, *, direction: str) -> None:
        assert (kind, direction) == ("video", "recvonly")

    async def createOffer(self) -> FakeRTCSessionDescription:
        return FakeRTCSessionDescription(sdp=self.offer_sdp, type="offer")

    async def setLocalDescription(self, offer: FakeRTCSessionDescription) -> None:
        self.localDescription = offer

    async def setRemoteDescription(self, answer: FakeRTCSessionDescription) -> None:
        assert answer.type == "answer"
        assert self._track_handler is not None
        self._track_handler(FakeVideoTrack())

    async def close(self) -> None:
        return


class RecordingPeerConnection:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class BlockingDeleteClient:
    def __init__(self) -> None:
        self.delete_started = asyncio.Event()
        self.allow_delete = asyncio.Event()
        self.delete_finished = False

    async def delete(self, _url: str) -> httpx.Response:
        self.delete_started.set()
        await self.allow_delete.wait()
        self.delete_finished = True
        return httpx.Response(204)


def test_extracts_private_whep_url_from_both_response_shapes() -> None:
    assert (
        _playback_url({"data": {"webRTCPlayback": {"url": "https://whep/one"}}})
        == "https://whep/one"
    )
    assert (
        _playback_url({"payload": {"data": {"webRTCPlayback": {"url": "https://whep/two"}}}})
        == "https://whep/two"
    )


@pytest.mark.asyncio
async def test_encodes_received_video_frame_as_jpeg() -> None:
    session = CameraSession(NoCommandTransport(), "device")
    session._video_track = OneFrameTrack()
    try:
        frame = await session.next_frame(jpeg_quality=80)
    finally:
        await session.close()
    assert frame.width == 8
    assert frame.height == 6
    assert frame.jpeg_bytes.startswith(b"\xff\xd8")


@pytest.mark.asyncio
async def test_open_retries_whep_conflict_without_restarting_live_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        if delay >= 50:
            await real_sleep(delay)
            return
        sleep_delays.append(delay)

    monkeypatch.setattr(camera_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        CameraSession,
        "_webrtc_types",
        staticmethod(
            lambda: (
                FakeRTCConfiguration,
                FakeRTCIceServer,
                FakeRTCPeerConnection,
                FakeRTCSessionDescription,
            )
        ),
    )

    requests: list[httpx.Request] = []
    statuses = iter((409, 201))

    def whep_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(next(statuses), text="v=0\r\no=fake-answer\r\n")

    transport = RecordingTransport()
    http = httpx.AsyncClient(transport=httpx.MockTransport(whep_handler))
    session = CameraSession(transport, "device", http_client=http)
    try:
        await session.open()
    finally:
        await session.close()
        await http.aclose()

    assert sleep_delays == [3.0, 0.5]
    assert [request.content for request in requests] == [
        FakeRTCPeerConnection.offer_sdp.encode(),
        FakeRTCPeerConnection.offer_sdp.encode(),
    ]
    assert [request.headers["Content-Type"] for request in requests] == [
        "application/sdp",
        "application/sdp",
    ]
    assert transport.commands == [LIVE_STREAM_START, LIVE_STREAM_STOP]


@pytest.mark.asyncio
async def test_open_follows_temporary_whep_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        if delay >= 50:
            await real_sleep(delay)
            return
        sleep_delays.append(delay)

    monkeypatch.setattr(camera_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        CameraSession,
        "_webrtc_types",
        staticmethod(
            lambda: (
                FakeRTCConfiguration,
                FakeRTCIceServer,
                FakeRTCPeerConnection,
                FakeRTCSessionDescription,
            )
        ),
    )

    requested_paths: list[str] = []

    def whep_handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/whep":
            return httpx.Response(307, headers={"Location": "/ready"})
        return httpx.Response(201, text="v=0\r\no=fake-answer\r\n")

    transport = RecordingTransport()
    http = httpx.AsyncClient(transport=httpx.MockTransport(whep_handler))
    session = CameraSession(transport, "device", http_client=http)
    try:
        await session.open()
    finally:
        await session.close()
        await http.aclose()

    assert requested_paths == ["/whep", "/ready"]
    assert sleep_delays == [3.0]
    assert transport.commands == [LIVE_STREAM_START, LIVE_STREAM_STOP]


@pytest.mark.asyncio
async def test_cancelled_close_continues_cleanup_and_can_be_joined() -> None:
    transport = RecordingTransport()
    http = BlockingDeleteClient()
    peer = RecordingPeerConnection()
    session = CameraSession(transport, "device", http_client=http)  # type: ignore[arg-type]
    session._stream_started = True
    session._peer_connection = peer
    session._whep_resource_url = "https://camera.test/resource"

    closing = asyncio.create_task(session.close())
    await asyncio.wait_for(http.delete_started.wait(), timeout=1)
    assert transport.commands == [LIVE_STREAM_STOP]
    assert peer.closed is True

    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert http.delete_finished is False

    http.allow_delete.set()
    await asyncio.wait_for(session.close(), timeout=1)
    assert http.delete_finished is True
    assert transport.commands == [LIVE_STREAM_STOP]
