"""Private Anova live-video protocol and WHEP/WebRTC frame capture."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, Protocol, Self
from urllib.parse import urljoin

import httpx

from .exceptions import (
    AnovaError,
    CameraError,
    CameraUnavailableError,
    MissingCameraDependencyError,
)
from .models import CameraFrame

LIVE_STREAM_START = "CMD_APO_START_LIVE_STREAM"
LIVE_STREAM_STOP = "CMD_APO_STOP_LIVE_STREAM"
ICE_SERVERS = (
    "stun:stun.cloudflare.com:3478",
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:19302",
)
LIVE_STREAM_STOP_TIMEOUT = 9.0
MEDIA_CLEANUP_TIMEOUT = 2.0


class CommandTransport(Protocol):
    async def send_command(
        self,
        command: str,
        *,
        device_id: str,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[str, dict[str, Any]]: ...


def _playback_url(response: dict[str, Any]) -> str | None:
    candidates: list[Any] = [response.get("data")]
    payload = response.get("payload")
    if isinstance(payload, dict):
        candidates.extend([payload.get("data"), payload])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        playback = candidate.get("webRTCPlayback")
        if isinstance(playback, dict) and isinstance(playback.get("url"), str):
            return playback["url"]
    return None


class CameraSession:
    """A receive-only WebRTC camera session with Anova keepalives."""

    def __init__(
        self,
        transport: CommandTransport,
        device_id: str,
        *,
        keepalive_seconds: float = 55.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._transport = transport
        self._device_id = device_id
        self._keepalive_seconds = keepalive_seconds
        self._http = http_client or httpx.AsyncClient(timeout=20.0)
        self._owns_http = http_client is None
        self._peer_connection: Any | None = None
        self._video_track: Any | None = None
        self._whep_resource_url: str | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._open_lock = asyncio.Lock()
        self._frame_lock = asyncio.Lock()
        self._stream_started = False
        self._closed = False

    @staticmethod
    def _webrtc_types() -> tuple[Any, Any, Any, Any]:
        try:
            from aiortc import (
                RTCConfiguration,
                RTCIceServer,
                RTCPeerConnection,
                RTCSessionDescription,
            )
        except ImportError as error:
            raise MissingCameraDependencyError(
                "Install the 'camera' or 'server' extra to capture video frames."
            ) from error
        return RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

    async def open(self, *, timeout: float = 25.0) -> None:
        if self._video_track is not None:
            return
        async with self._open_lock:
            if self._video_track is not None:
                return
            if self._closed:
                raise CameraError("This camera session has already been closed.")
            (
                RTCConfiguration,
                RTCIceServer,
                RTCPeerConnection,
                RTCSessionDescription,
            ) = self._webrtc_types()

            try:
                _, stream_response = await self._transport.send_command(
                    LIVE_STREAM_START,
                    device_id=self._device_id,
                    timeout=12.0,
                )
            except AnovaError as error:
                raise CameraUnavailableError(
                    "Anova did not start live video. APO 2.0, an active cook, current "
                    "firmware, and an eligible subscription are required."
                ) from error
            self._stream_started = True
            playback_url = _playback_url(stream_response)
            if not playback_url:
                await self.close()
                raise CameraUnavailableError(
                    "Anova acknowledged live video but did not return a WHEP playback URL."
                )

            configuration = RTCConfiguration(iceServers=[RTCIceServer(urls=list(ICE_SERVERS))])
            peer = RTCPeerConnection(configuration=configuration)
            self._peer_connection = peer
            track_future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

            @peer.on("track")
            def on_track(track: Any) -> None:
                if track.kind == "video" and not track_future.done():
                    track_future.set_result(track)

            peer.addTransceiver("video", direction="recvonly")
            try:
                offer = await peer.createOffer()
                await peer.setLocalDescription(offer)
                # The mobile app intentionally allows the cloud stream to come
                # online for three seconds after ICE gathering before posting
                # its offer. Posting immediately can receive a transient 409.
                await asyncio.sleep(3.0)
                local_description = peer.localDescription
                if local_description is None:
                    raise CameraError("WebRTC did not create a local SDP offer.")
                whep_response: httpx.Response | None = None
                for attempt in range(3):
                    try:
                        whep_response = await self._http.post(
                            playback_url,
                            content=local_description.sdp.encode("utf-8"),
                            headers={"Content-Type": "application/sdp"},
                            follow_redirects=True,
                        )
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise
                    else:
                        if whep_response.status_code == 201:
                            break
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
                if whep_response is None or whep_response.status_code != 201:
                    status = (
                        whep_response.status_code if whep_response is not None else "unknown"
                    )
                    raise CameraUnavailableError(
                        f"The WHEP endpoint rejected playback (HTTP {status})."
                    )
                location = whep_response.headers.get("Location")
                if location:
                    self._whep_resource_url = urljoin(playback_url, location)
                answer_sdp = whep_response.text
                if not answer_sdp.strip():
                    raise CameraError("The WHEP endpoint returned an empty SDP answer.")
                await peer.setRemoteDescription(
                    RTCSessionDescription(sdp=answer_sdp, type="answer")
                )
                self._video_track = await asyncio.wait_for(track_future, timeout)
            except Exception as error:
                await self.close()
                if isinstance(error, CameraError):
                    raise
                raise CameraError("Could not establish the WebRTC camera session.") from error

            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(), name="anova-camera-keepalive"
            )

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(self._keepalive_seconds)
            try:
                await self._transport.send_command(
                    LIVE_STREAM_START,
                    device_id=self._device_id,
                    timeout=12.0,
                )
            except AnovaError:
                # The media path may continue through a transient command
                # failure; frame receipt remains the authoritative signal.
                continue

    async def next_frame(
        self,
        *,
        timeout: float = 20.0,
        jpeg_quality: int = 85,
    ) -> CameraFrame:
        if not 1 <= jpeg_quality <= 95:
            raise ValueError("jpeg_quality must be between 1 and 95.")
        await self.open(timeout=timeout)
        if self._video_track is None:
            raise CameraError("The WebRTC session did not provide a video track.")
        async with self._frame_lock:
            try:
                video_frame = await asyncio.wait_for(self._video_track.recv(), timeout)
                image = video_frame.to_image()
                output = BytesIO()
                image.save(output, format="JPEG", quality=jpeg_quality, optimize=True)
            except TimeoutError as error:
                raise CameraError("Timed out waiting for an oven camera frame.") from error
            except Exception as error:
                raise CameraError("The oven camera stream ended before a frame arrived.") from error
        return CameraFrame(
            jpeg_bytes=output.getvalue(),
            width=int(video_frame.width),
            height=int(video_frame.height),
            captured_at=datetime.now(UTC),
        )

    async def frames(
        self,
        *,
        timeout: float = 20.0,
        jpeg_quality: int = 85,
        minimum_interval_seconds: float = 0.0,
    ) -> AsyncIterator[CameraFrame]:
        while not self._closed:
            yield await self.next_frame(timeout=timeout, jpeg_quality=jpeg_quality)
            if minimum_interval_seconds > 0:
                await asyncio.sleep(minimum_interval_seconds)

    async def _close_impl(self) -> None:
        keepalive = self._keepalive_task
        self._keepalive_task = None
        if keepalive is not None and not keepalive.done():
            keepalive.cancel()
            await asyncio.gather(keepalive, return_exceptions=True)

        # Stop the oven publisher before optional viewer-resource cleanup. Each
        # operation is independently bounded so a stalled WHEP request cannot
        # prevent the live-stream stop.
        if self._stream_started:
            self._stream_started = False
            try:
                await asyncio.wait_for(
                    self._transport.send_command(
                        LIVE_STREAM_STOP,
                        device_id=self._device_id,
                        timeout=8.0,
                    ),
                    timeout=LIVE_STREAM_STOP_TIMEOUT,
                )
            except Exception:
                pass

        peer = self._peer_connection
        self._peer_connection = None
        self._video_track = None
        if peer is not None:
            try:
                await asyncio.wait_for(peer.close(), timeout=MEDIA_CLEANUP_TIMEOUT)
            except Exception:
                pass

        resource_url = self._whep_resource_url
        self._whep_resource_url = None
        if resource_url:
            try:
                await asyncio.wait_for(
                    self._http.delete(resource_url),
                    timeout=MEDIA_CLEANUP_TIMEOUT,
                )
            except Exception:
                pass

        if self._owns_http:
            try:
                await asyncio.wait_for(
                    self._http.aclose(),
                    timeout=MEDIA_CLEANUP_TIMEOUT,
                )
            except Exception:
                pass

    async def close(self) -> None:
        task = self._close_task
        if task is None:
            self._closed = True
            task = asyncio.create_task(self._close_impl(), name="anova-camera-close")
            self._close_task = task
        # A caller timeout must not cancel the publisher-stop task. Calling
        # close() again joins the same cleanup rather than becoming a no-op.
        await asyncio.shield(task)

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()
