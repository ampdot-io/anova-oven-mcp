# Anova Precision Oven library + MCP server

This project contains two deliberately separate layers:

- `anova_oven`: an async Python library for Anova authentication, device discovery,
  temperatures, cook control, stages, and APO 2.0 WebRTC camera frames.
- `anova_oven_mcp`: a thin MCP 2.x adapter over that library.

The separation keeps the device code reusable in a future Raspberry Pi service or
extension without making MCP a core dependency.

## Features

- Capture one JPEG camera frame or consume frames as an async iterator.
- Read the four physical cooking sensors: dry top, dry bottom, wet bulb, and food
  probe. The active dry control value is returned separately.
- Start and stop cooks. Timed stages support 1 second through the oven's maximum of
  359,940 seconds (99h 59m); an omitted duration runs until stopped.
- Use probe-terminated cooks and sequential, timed, preheat-delayed, or manually
  advanced stages.
- Replace the stage list of an active cook.
- Return UTC time-of-day as hours, minutes, and seconds.
- Load credentials from macOS Keychain, a private mode-0600 file, or an injected
  environment secret.

## Important status and safety notes

Cook commands physically activate an appliance. The MCP tools that start or alter a
cook require `acknowledge_physical_action=true`, and validate the oven's documented
temperature, timer, probe, heating-element, steam, fan, and rack limits.

The camera path is a private mobile-app protocol, not part of Anova's documented
Personal Access Token command surface. It currently requires:

- Anova Precision Oven 2.0
- an active cook
- compatible/current firmware
- an eligible Anova subscription

The live-video command or response shape may change with a future Anova app/backend
release. Signed playback URLs are kept internal and are never returned by the library
or MCP server.

See [VALIDATION.md](VALIDATION.md) for the completed live sensor, cook, camera, and
post-test safety checks.

## Installation

Python 3.11 or newer is required. Python 3.12 is recommended.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[server]"
```

For library use without MCP or video:

```bash
python -m pip install -e .
```

Optional extras are `camera`, `mcp`, `server`, and `test`. A base install keeps the
core library lightweight; the `anova-oven-mcp` command will direct you to install the
`mcp` or `server` extra if its optional runtime is absent.

## Credentials

The default credential search order is:

1. `ANOVA_FIREBASE_REFRESH_TOKEN`
2. the file named by `ANOVA_FIREBASE_REFRESH_TOKEN_FILE`
3. macOS Keychain generic-password item with:
   - service `com.codex.anova-camera.firebase-refresh-token`
   - account `anova-oven-mcp`

The fixed Keychain account label prevents an older credential under the same service
from being selected accidentally. Keychain reads and token rotations use Apple's
Security framework directly, so the secret is not placed in process arguments.

Do not put a refresh token in source, MCP configuration, shell history, or logs.
For a Pi, a host secret manager or a root-owned mode-0600 credential file is the
recommended adapter:

```bash
chmod 600 /run/credentials/anova-refresh-token
export ANOVA_FIREBASE_REFRESH_TOKEN_FILE=/run/credentials/anova-refresh-token
```

If more than one oven is paired, set `ANOVA_OVEN_ID` in the process environment. The
MCP device-list tool deliberately returns only redacted IDs; configure the full ID
outside model-visible context.

## MCP server

The local default is stdio, so it opens no listening port:

```bash
.venv/bin/anova-oven-mcp
```

A generic desktop MCP configuration looks like:

```json
{
  "mcpServers": {
    "anova-oven": {
      "command": "/absolute/path/to/anova-oven-mcp/.venv/bin/anova-oven-mcp"
    }
  }
}
```

Available tools:

| Tool | Purpose |
| --- | --- |
| `oven_list_devices` | List paired ovens with redacted IDs |
| `oven_get_temperatures` | Read all four physical sensors and dry-control value |
| `oven_get_camera_frame` | Return one `image/jpeg` MCP image |
| `oven_start_cook` | Start a one-stage timed, probe, or unbounded cook |
| `oven_start_staged_cook` | Start a multi-stage cook |
| `oven_configure_stages` | Replace stages in the active cook |
| `oven_start_stage` | Advance to a configured stage UUID |
| `oven_stop_cook` | Stop cooking and close live video |
| `get_utc_time` | Return UTC hours, minutes, and seconds |

### Claude Code and Claude Desktop

The included configurator can register this local stdio server with Claude Code,
Claude Desktop, or both. It does not copy Anova credentials into either client.

Preview the changes first:

```bash
python scripts/configure_claude.py --dry-run
```

Configure both clients (Claude Code uses user scope by default):

```bash
python scripts/configure_claude.py --target both
```

Useful alternatives:

```bash
# One client only
python scripts/configure_claude.py --target code
python scripts/configure_claude.py --target desktop

# Update an existing same-named Claude Code entry
python scripts/configure_claude.py --target code --replace

# Remove the entry from both clients
python scripts/configure_claude.py --remove
```

Claude Desktop configuration is merged atomically, preserving other servers and
creating a timestamped mode-0600 backup. Quit and reopen Claude Desktop afterward.
For distributing this beyond a local checkout, Anthropic's current preferred format
is an installable MCP Bundle (`.mcpb`).

For a future Pi service, the same adapter can use Streamable HTTP:

```bash
anova-oven-mcp --transport streamable-http
```

That binds to `127.0.0.1:8766`. A non-loopback bind requires `--allow-lan`; do not
expose it until an authenticated reverse proxy or MCP authorization layer is in
place.

## Library example

```python
import asyncio

from anova_oven import CookPlan, CookingStage, PrecisionOvenClient


async def main() -> None:
    async with PrecisionOvenClient() as oven:
        temperatures = await oven.get_temperatures()
        print(temperatures.as_dict())

        plan = CookPlan(
            title="Two-stage example",
            stages=(
                CookingStage(
                    title="Warm",
                    target_celsius=60,
                    duration_seconds=600,
                ),
                CookingStage(
                    title="Finish",
                    target_celsius=180,
                    duration_seconds=300,
                ),
            ),
        )

        # This physically starts the oven:
        receipt = await oven.start_cook(plan)
        print(receipt.stage_ids)

        try:
            frame = await oven.capture_frame(timeout=60)
            with open("oven-frame.jpg", "wb") as output:
                output.write(frame.jpeg_bytes)
        finally:
            await oven.stop_cook()


asyncio.run(main())
```

For repeated frames, use `oven.frames()` as an async iterator. MCP intentionally
exposes one-shot images because indefinitely streaming tool calls are poorly portable
between hosts.

## Verification

```bash
python -m pip install -e ".[server,test]"
ruff check .
mypy src
pytest
python -m pip check
```

The read-only account check is:

```bash
python scripts/live_read_only.py
```

The guarded camera smoke test starts a minimum-temperature three-minute cook, waits
for an independent state event confirming that cook, captures one frame, and issues
the physical stop before best-effort media cleanup:

```bash
python scripts/live_camera_smoke.py --acknowledge-empty-oven-and-start-cook
```

That test completed successfully against an APO 2.0. The captured validation frame is
included as [oven-camera-smoke.jpg](oven-camera-smoke.jpg).

## Protocol sources

- [Anova Wi-Fi authentication](https://developer.anovaculinary.com/docs/devices/wifi/authentication)
- [Anova device discovery](https://developer.anovaculinary.com/docs/devices/wifi/device-discovery)
- [Anova oven commands and v2 stage examples](https://developer.anovaculinary.com/docs/devices/wifi/oven-commands)
- [Official Anova Wi-Fi reference project](https://github.com/anova-culinary/developer-project-wifi)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
