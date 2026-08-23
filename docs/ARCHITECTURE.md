# Architecture and Pi-extension boundary

The device implementation flows through narrow, injectable layers:

```text
RefreshTokenStore
  -> FirebaseTokenProvider
    -> OvenCloudTransport
      -> PrecisionOvenClient
        -> MCPServer adapter or a future Pi extension
```

## Core boundary

`anova_oven` has no dependency on MCP. Its public facade is
`PrecisionOvenClient`, which is an async context manager and owns the cloud and
camera lifecycles. A native Pi extension can call it directly:

```python
from pathlib import Path

from anova_oven import FileRefreshTokenStore, PrecisionOvenClient

store = FileRefreshTokenStore(Path("/run/credentials/anova-refresh-token"))
oven = PrecisionOvenClient(refresh_token_store=store)
```

The client exposes:

- `get_temperatures()`
- `start_cook()` and `start_simple_cook()`
- `configure_stages()` and `start_stage()`
- `stop_cook()`
- `capture_frame()`
- `frames()` as an async iterator

## Secret adapter

The `RefreshTokenStore` protocol has only `load()` and `save()`. A Pi-extension host
can implement those methods using its own encrypted secret API without changing any
device or MCP code. The included file store rejects symlinks and group/world-readable
files, and uses an atomic mode-0600 replacement for Firebase token rotation.

On macOS, the included adapter addresses one exact generic-password item by service
and the fixed account label `anova-oven-mcp`. It calls Security.framework directly,
so credential values are never passed on a subprocess command line. The portable
interface remains independent of this platform-specific implementation.

## MCP adapter

`create_server(client_factory=...)` accepts an injected client factory. This is used
by tests and lets another host provide custom credentials, telemetry, or policy while
retaining the exact same MCP schemas.

The desktop entry point uses stdio. Streamable HTTP exists for a supervised Pi
service, but intentionally refuses a non-loopback bind unless `--allow-lan` is given.
That flag is not authentication; place an authorization-aware reverse proxy or MCP
auth layer in front of any network-visible deployment.

## Camera dependency boundary

WebRTC imports are lazy. Non-camera uses install only `httpx` and `websockets`.
Installing the `camera` or `server` extra adds `aiortc`, PyAV, and Pillow. Current
packages publish ARM wheels suitable for modern 64-bit Raspberry Pi OS.

The library keeps one WebRTC session open for repeated captures, serializes calls to
the video track, and sends the mobile app's periodic live-stream keepalive. MCP returns
one JPEG per call; a native extension can consume `frames()` continuously.

The oven may return its signed WHEP playback URL before its video publisher is ready.
Session setup therefore mirrors the current mobile client: allow three seconds for
publisher startup, then retry the same SDP offer up to three times with short bounded
delays. Signed URLs and SDP remain internal.

## Safety and command semantics

Oven commands are correlated by UUID and are never retried automatically. A timed-out
start may still have reached the oven, so callers should read current state before
deciding what to do next. WHEP HTTP negotiation is the one retrying path because it
does not repeat the physical start command.

Stopping sends `CMD_APO_STOP` before attempting WHEP, peer-connection, or live-stream
cleanup. Camera teardown then stops the oven's video publisher before optional WHEP
cleanup; it is bounded, joinable, and continues if its caller is cancelled. Media
cleanup therefore cannot prevent the physical stop command or strand a half-closed
session. MCP mutating tools validate settings and require an explicit physical-action
acknowledgement. The core library still validates device limits but assumes direct
callers own their confirmation UX.
