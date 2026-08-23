# Validation record

Date: 2026-08-20 (America/Los_Angeles)

## Live checks completed

- The password-provider Firebase refresh credential was migrated to the library's
  fixed, non-PII macOS Keychain account and loaded without printing it or placing it
  in process arguments.
- Firebase issued a valid ID token, and the library connected to the current Anova
  cloud endpoint with the `ANOVA_V2` WebSocket subprotocol.
- Device discovery returned one paired, online Anova Precision Oven 2.0. Its full
  device ID was never logged or included in this record.
- A live read returned all four physical cooking sensors: dry top, dry bottom, wet
  bulb, and the connected food probe. The controller's active dry reading was also
  returned separately. The sensors were near room temperature, as expected.
- The guarded smoke test started a 25 °C cook capped at 180 seconds and confirmed the
  active cook from a fresh, independent oven-state event.
- The private live-video path established WebRTC and captured a valid 600×950 JPEG:
  [oven-camera-smoke.jpg](oven-camera-smoke.jpg).
- Cleanup sent the physical `CMD_APO_STOP` first. A final fresh state check confirmed
  the oven was online and idle, with no active cook, an idle timer, and camera
  streaming disabled.

## Offline checks completed

- Forty-eight tests cover Firebase token refresh/caching, exact-account Keychain access,
  mode-0600 file credentials, command correlation and cache invalidation, v1/v2 stage
  encoding, all four sensors, camera/WHEP behavior, JPEG conversion, MCP schemas and
  image content, physical-action acknowledgements, and stop-before-camera-cleanup
  ordering.
- Ruff, mypy, dependency-integrity, and package-build checks pass.

## Scope and limitations

- Start and stop were exercised live. Multi-stage encoding, active-stage replacement,
  and explicit stage advancement are covered by protocol fixtures and unit tests but
  were not separately exercised on the physical appliance.
- The camera commands are private mobile-app behavior rather than Anova's documented
  Personal Access Token API. They can change without notice and currently require an
  eligible APO 2.0 account, compatible firmware, and an active cook.
- Two older, email-labelled Keychain entries were preserved. The library ignores them
  because it addresses only the canonical `anova-oven-mcp` account.
