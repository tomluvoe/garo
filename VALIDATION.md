# Validation on 21 November 2026

## Automated tests

All 18 tests passed on Python 3.12.13 with the MCP Python SDK 1.29.0. The 16 bridge and dashboard tests also passed on Python 3.9.6. The SDK is optional for the dashboard.

The suite covers unit conversion, firmware state labels, the unavailable-meter sentinel, clock freshness, retained readings, sample isolation, serial mismatch, oversized JSON numbers, truncated responses, cache reuse and failure recovery. Loopback tests exercise the static assets and API, Host and Origin checks, and read-only requests. The stdio integration tests exercise initialization, tool discovery, structured results, and error propagation. The SDK emitted a Pydantic settings warning on stderr during startup.

## Physical charger

The dashboard API and both MCP tools returned live status from a charger on the local network.

The firmware's original HTML confirmed the display mapping for `SEARCH_COMM` and `SCHEMA`, conversions for power, current and energy, and the missing-meter sentinel. The charger was idle during verification. Active-charging behavior was exercised with controlled test data.

## Browser

Headless Chrome rendered the live dashboard at 1280 pixels and the sample at 390 and 320 pixels. The layout fit all three widths. Browser checks covered sample labels, reported values, clearing readings after a failed refresh, recovery after reconnection, and the raw response disclosure.

## Docker

Built `garo:local` for Linux arm64 on Docker Desktop and ran the tests inside the image with external networking disabled. Compose published the dashboard on host loopback. The published dashboard served its page and live status, and the generated Docker MCP configuration initialized and called both tools against the physical charger with the dashboard stopped.

A second Compose run used host port 18765 and an unavailable charger endpoint. It stayed healthy while the status API returned HTTP 502, and it rejected a Host or Origin on the wrong port. The dashboard ran as UID 10001 with a read-only root filesystem. A read-only sample mount worked without networking.

The rebuilt image excludes `.env` and requires runtime device configuration for live operation. The native CLI, Compose dashboard, and generated Docker MCP configuration reached the charger using local settings. The generated sample MCP configuration worked with networking disabled.
