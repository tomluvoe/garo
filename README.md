# GARO local dashboard and MCP server

Read your GARO charger's status and use its availability and cable-lock controls from your home network. The integration runs on your computer.

## Local charger configuration

For a fresh checkout, copy `.env.example` to `.env` and replace the example address and serial. Use unquoted `NAME=value` lines. Git and the Docker build ignore `.env`.

| Variable | Setting |
| --- | --- |
| `GARO_CHARGER_HOST` | Private IPv4 address of your charger |
| `GARO_CHARGER_PORT` | Charger HTTP port, defaults to `8080` |
| `GARO_CHARGER_SERIAL` | Expected charger serial number |
| `GARO_DASHBOARD_PORT` | Optional Docker Compose host port, defaults to `8765` |
| `GARO_DASHBOARD_REFRESH_SECONDS` | Optional Docker Compose dashboard refresh interval, defaults to `60` |
| `GARO_ENERGY_TIMEZONE` | IANA time zone for the Docker energy archive, defaults to `UTC` |
| `GARO_ENERGY_INTERVAL_SECONDS` | Docker archive collection interval, defaults to `3600` |

## Run with Docker

From this checkout, build the shared `garo:local` image and start the dashboard with Docker Compose.

```sh
docker compose up -d --build --wait
```

Open [the local dashboard](http://127.0.0.1:8765/). The dashboard listens on the container network, and Compose publishes it on your computer's loopback interface. `/healthz` checks that the dashboard is serving requests. `/api/status` reports charger connectivity and returns HTTP 502 if the charger read fails. Stop the dashboard with `docker compose down`.

### Energy archive

Compose stores daily energy readings in the local `energy-data` volume. Collection runs at startup and hourly by default, including when the dashboard is closed. The collector queries the current month and also the previous month when the recent seven-day window crosses a month boundary. During a failed collection it retries within five minutes.

Set `GARO_ENERGY_TIMEZONE` before the first run so dates match the charger's calendar. Use the same time zone when restarting an existing archive. Regular container rebuilds and `docker compose down` preserve the volume.

The Energy panel shows which days have readings and when background collection last succeeded. Previously saved days survive empty or failed charger responses. A prolonged outage can leave gaps once those days have rolled off the charger.

### Docker MCP client

If you only want MCP, build the image with `docker build -t garo:local .`. Generate configuration with an absolute path to your local `.env`.

```sh
python3 garo_bridge.py config --docker
```

Copy the generated `mcpServers.garo` entry into your client. If the client cannot find `docker` on its PATH, use the absolute path from `command -v docker` for `command`. [Docker MCP configuration template](mcp.docker.json)

The client runs an on-demand stdio container from the same image with a read-only filesystem. It can read the charger with the dashboard stopped. Both containers need a route to the charger's private IP.

To check the container's connection directly or preview the synthetic fixture, run the corresponding command below. The sample file is mounted from this checkout.

```sh
docker run --rm --pull=never --read-only --env-file .env garo:local status
docker run --rm --pull=never --read-only \
  --mount "type=bind,src=$PWD/sample_status.json,dst=/sample_status.json,readonly" \
  garo:local status --sample /sample_status.json
```

## Dashboard and command line

Run from this checkout with Python 3.9 or later. The dashboard uses the standard library.

```sh
python3 garo_bridge.py dashboard
```

Open [the local dashboard](http://127.0.0.1:8765/). Press Ctrl+C in Terminal to stop it. For one status reading or an offline preview, run the corresponding command below.

```sh
python3 garo_bridge.py status
python3 garo_bridge.py dashboard --sample sample_status.json
```

The native commands load `.env` from the checkout. Exported charger settings override the file, and `--host`, `--port`, and `--serial` override both. The bridge checks the charger serial against the configured value. The dashboard listens at `127.0.0.1` with an optional `--listen-port`. Use `--refresh-seconds` to set the native dashboard refresh interval in seconds.

For a native energy archive, pass `--energy-archive data/energy.sqlite3` and optionally `--energy-timezone` or `--energy-interval-seconds` to the dashboard command.

## MCP client setup

The MCP server uses the Python SDK over stdio. Use Python 3.10 or later for the virtual environment. The commands below use Python 3.12. [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.29.0) · [MCP stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-mcp.txt
.venv/bin/python garo_bridge.py config
```

Add the generated `mcpServers.garo` entry to a local MCP client that supports stdio. The config points to the active Python interpreter and this checkout with absolute paths. The client starts its own process, so it can work with the dashboard closed.

| Tool | Returns |
| --- | --- |
| `get_charger_status` | Structured status with connector state, reported readings, limits, and freshness evidence |
| `get_charger_diagnostics` | Structured status plus the original firmware payload |
| `set_charger_availability(available)` | Confirmed availability setting after the requested boolean change |
| `set_cable_lock(locked)` | Confirmed outlet cable-lock setting after the requested boolean change |

Run `config --sample sample_status.json` to generate a separate offline MCP configuration. Sample mode reads a JSON file and labels its readings as sample data.

## Reading the dashboard

- **Connector and operating mode.** The dashboard uses labels from the installed firmware and shows the raw connector code under Source details. For example, this firmware shows `SEARCH_COMM` as “Vehicle not connected” and `SCHEMA` as scheduled mode. `ALWAYS_ON` means charging is allowed. A connector state of `CHARGING` identifies reported active charging.
- **API response and controller freshness.** API response time is when the bridge received the payload. Controller freshness uses `mainCharger.lastContact`. A timestamp more than five minutes old or more than two minutes ahead falls outside the freshness window. These thresholds are bridge heuristics. The controller's online flag is shown separately since its semantics are unverified.
- **Refresh cadence.** The dashboard polls for status every 60 seconds by default while its tab is visible. It pauses automatic requests in the background and refreshes when you return if a read is due. Select **Refresh status** for an immediate request. The minimum configured interval is five seconds, matching the bridge's cache window.
- **Reported measurements.** The installed interface divides power, current, and energy counters by 1,000 to display kW, A, and kWh. A current of `-1` marks current and power unavailable. Positive power or current can persist after a session. The bridge considers power usable as live only when the connector reports `CHARGING`, the timestamp is recent, power is available, and the controller-online flag is not false.
- **Current limits.** The configured, hardware switch, factory, and pilot limits are separate readings. The pilot is the advertised current.
- **Energy history.** Open **Energy history** and select a month to see daily kWh, the recorded monthly total, and the cumulative meter reading at the end of the period. Expand **Daily readings** for exact values and days with no reading. Dates use the archive's configured time zone when an archive is enabled and the browser's time zone otherwise.
- **Sample.** The bundled fixture uses a fictional charger and session. It shows how the dashboard handles a retained 2.3 kW reading after `CHARGING_FINISHED`.

## Charger controls

Open **Charger controls** to make the charger available or unavailable and lock or unlock the outlet cable. Availability control targets a single locally managed charger. Selecting either availability state sets a fixed operating mode and replaces an active schedule mode. Cable locking requires a supported socket outlet and firmware. Its readback confirms the lock setting, and the charger governs when the physical latch can move.

The dashboard and MCP use the same commands and confirm each requested setting with a fresh status read. If a command fails or readback disagrees, refresh the charger's status before retrying. Sample mode offers status and diagnostics for the synthetic fixture.

## Architecture

The CLI, dashboard API, and MCP tools share `ChargerClient` and the same normalization logic. The client makes GET requests to the configured status endpoint with an explicit timeout, no proxy, no redirects, and a bounded response size. Successful and failed reads are deduplicated for up to five seconds per process. A failed read clears the previous result.

The dashboard serves static assets plus `/api/status` and `/api/energy` and accepts same-origin requests on loopback. The Energy panel uses saved readings when available and can request a month from the configured charger and default meter. Firmware responses are cached for five minutes. **Refresh history** rechecks the charger after the five-second duplicate-request window and saves any returned readings when archiving is enabled. Sample mode runs without a charger connection.

Raw status diagnostics are available in a disclosure and in the dedicated MCP tool. The MCP server performs blocking charger I/O off the protocol event loop.

## Validation

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The suite uses the synthetic fixture and a loopback mock charger. Install the MCP SDK to run both stdio integration tests. [Verification results](VALIDATION.md)
