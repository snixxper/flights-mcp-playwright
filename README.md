# flights-mcp-playwright ✈️

Search Google Flights from **Claude Desktop**, in plain language.

A small local [MCP](https://modelcontextprotocol.io) server. It opens Google Flights in a hidden Chromium browser using [Playwright](https://playwright.dev/python/), reads the results, and gives Claude a short, clean list of flights. Claude gets about 30 lines of data, not whole web pages, so searches use very few tokens.

> "Find me a round trip from Lima to Cusco, Jan 10–14"

```json
{
  "price": "117 US dollars round trip total",
  "airline": "LATAM",
  "stops": "Nonstop",
  "depart": "12:20 PM on Sunday, January 10",
  "arrive": "1:45 PM on Sunday, January 10",
  "duration": "1 hr 25 min"
}
```

## Why

Google Flights now loads its results with JavaScript inside the browser. Tools that read the raw page, like [fli](https://github.com/punitarani/fli), come back empty. This server uses a real browser, so it sees the same results you do.

## Features

- One-way and round-trip searches
- Price, airline, stops, departure and arrival times, duration, and layovers
- Any currency (`USD`, `PEN`, `EUR`, …)
- One file, no project setup: [uv](https://docs.astral.sh/uv/) installs the dependencies for you
- Runs on macOS and Windows

## Setup

### 1. Install uv

**macOS / Linux**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Restart your terminal afterwards.

### 2. Install the browser (one time, ~150 MB)

```bash
uv run --script https://raw.githubusercontent.com/snixxper/flights-mcp-playwright/main/flights_pw.py --install-browser
```

### 3. Test it

```bash
uv run --script https://raw.githubusercontent.com/snixxper/flights-mcp-playwright/main/flights_pw.py --test LIM MIA 2026-12-20
```

Add a return date for a round trip. Add `--show` to watch the browser work.

### 4. Add it to Claude Desktop

In Claude Desktop, open **Settings → Developer → Edit Config**, and add this inside `"mcpServers"`:

**macOS** (replace `YOUR_USER`)
```json
"flights-pw": {
  "command": "/Users/YOUR_USER/.local/bin/uv",
  "args": ["run", "--script", "https://raw.githubusercontent.com/snixxper/flights-mcp-playwright/main/flights_pw.py"]
}
```

**Windows** (replace `YOUR_USER`)
```json
"flights-pw": {
  "command": "C:\\Users\\YOUR_USER\\.local\\bin\\uv.exe",
  "args": ["run", "--script", "https://raw.githubusercontent.com/snixxper/flights-mcp-playwright/main/flights_pw.py"]
}
```

Use the full path to `uv`. Claude Desktop often can't find it otherwise. Run `which uv` (macOS) or `where uv` (Windows) to get yours.

Save the file, quit Claude Desktop completely, and reopen it. **flights-pw** should show as **Running** in Settings → Developer. Then just ask Claude for flights.

> Pointing the config at the GitHub URL means you always run the latest version. To pin a version, download `flights_pw.py` and use its local path instead.

## Tool reference

`search_flights(origin, destination, date, return_date=None, currency="USD", max_results=10)`

| Parameter | Example | Notes |
|---|---|---|
| `origin` | `"LIM"` | IATA airport code |
| `destination` | `"CUZ"` | IATA airport code |
| `date` | `"2027-01-10"` | `YYYY-MM-DD` |
| `return_date` | `"2027-01-14"` | Leave it out for one-way |
| `currency` | `"PEN"` | ISO 4217 code |
| `max_results` | `10` | Top N results |

For round trips, prices are the total for both ways, and the list shows outbound flights.

## How it works

1. Builds a Google Flights URL from your search.
2. Opens it in headless Chromium and declines the cookie banner if one appears.
3. Waits for the results, then reads each result's accessibility label (`aria-label`). Google writes those labels in plain sentences, so they change less often than its CSS class names.
4. Turns each label into a small JSON object.

## Limitations

- **It can break.** It depends on how Google Flights builds its page. If the results come back empty, Google probably changed something. Please open an issue.
- **Personal, low-volume use only.** This reads Google Flights pages, which Google's terms of service don't allow. Too many searches may get you a captcha or a temporary block.
- Each search takes about 5–10 seconds while the browser loads.
- Multi-city searches aren't supported yet.

## License

[MIT](LICENSE) © snixxper
