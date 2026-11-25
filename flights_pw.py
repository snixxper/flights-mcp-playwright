# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.2,<2", "playwright>=1.45"]
# ///
"""flights-pw: a tiny local MCP server that reads Google Flights with Playwright.

Setup (once):
    uv run --script flights_pw.py --install-browser
Test in Terminal:
    uv run --script flights_pw.py --test LIM MIA 2026-12-20
    uv run --script flights_pw.py --test LIM MIA 2026-12-20 2027-01-05 --show   # visible browser
    uv run --script flights_pw.py --dates LIM CUZ 2027-01-05 2027-01-12           # cheapest day
Run as an MCP server (what Claude Desktop does):
    uv run --script flights_pw.py
"""
import asyncio
import re
import subprocess
import sys
import urllib.parse
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import timedelta

from mcp.server.fastmcp import FastMCP
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

# Report the real OS: a Mac user agent on a Windows machine looks like a bot.
OS_TOKEN = {
    "win32": "Windows NT 10.0; Win64; x64",
    "darwin": "Macintosh; Intel Mac OS X 10_15_7",
}.get(sys.platform, "X11; Linux x86_64")


def user_agent(browser_version: str) -> str:
    major = browser_version.split(".")[0]
    return (
        f"Mozilla/5.0 ({OS_TOKEN}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )

RESULT_SEL = '[aria-label*="Total duration"]'
CABINS = {
    "economy": "",
    "premium_economy": " in premium economy",
    "business": " in business class",
    "first": " in first class",
}
MAX_DATE_SPAN = 14
PARALLEL_PAGES = 4


# ---------------------------------------------------------------- query + parsing


def build_url(
    origin: str,
    destination: str,
    date: str,
    return_date: str | None = None,
    currency: str = "USD",
    cabin: str = "economy",
    adults: int = 1,
    nonstop: bool = False,
) -> str:
    """Google Flights understands a plain-English query in the `q` parameter."""
    cabin_key = cabin.lower().replace(" ", "_").replace("-", "_")
    if cabin_key not in CABINS:
        raise ValueError(f"cabin must be one of {', '.join(CABINS)}")
    q = f"Flights to {destination.upper()} from {origin.upper()} on {date}"
    q += f" through {return_date}" if return_date else " oneway"
    q += CABINS[cabin_key]
    if adults > 1:
        q += f" for {adults} adults"
    if nonstop:
        q += " nonstop"
    params = {"q": q, "curr": currency.upper(), "hl": "en"}
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode(params)


def parse(label: str) -> dict:
    """Turn Google's aria-label for one result into a compact dict."""

    def m(pattern: str) -> str | None:
        r = re.search(pattern, label)
        return r.group(1).strip() if r else None

    price = m(r"^From (.+?)\.") or (
        "unavailable" if label.startswith("Total price is unavailable") else None
    )
    d = {
        "price": price,
        "airline": m(r"flight with (.+?)\."),
        "stops": "Nonstop" if "Nonstop" in label else m(r"(\d+ stops?)"),
        "depart": m(r"Leaves .+? at (.+? on .+?) and arrives"),
        "arrive": m(r"arrives at .+? at (.+? on .+?)\."),
        "duration": m(r"Total duration (.+?)\."),
        "layovers": re.findall(r"is a (.+? layover at .+?)\.", label) or None,
    }
    if not d["price"] or not d["duration"]:
        return {"raw": label}
    return {k: v for k, v in d.items() if v}


def price_value(flight: dict) -> float | None:
    """Numeric price, e.g. '1,417 US dollars' -> 1417.0. None if unknown."""
    r = re.match(r"([\d,.]+)", flight.get("price", ""))
    return float(r.group(1).replace(",", "")) if r else None


def stop_count(flight: dict) -> int | None:
    s = flight.get("stops", "")
    if s == "Nonstop":
        return 0
    r = re.match(r"(\d+)", s)
    return int(r.group(1)) if r else None


def keep(flight: dict, max_price: float | None, max_stops: int | None, airlines: list[str] | None) -> bool:
    if "raw" in flight:
        return False
    if max_price is not None:
        p = price_value(flight)
        if p is None or p > max_price:
            return False
    if max_stops is not None:
        s = stop_count(flight)
        if s is None or s > max_stops:
            return False
    if airlines:
        name = flight.get("airline", "").lower()
        if not any(a.lower() in name for a in airlines):
            return False
    return True


# ---------------------------------------------------------------- browser


@asynccontextmanager
async def browser_context(headless: bool = True):
    async with async_playwright() as p:
        # channel="chromium" runs the full browser in Chrome's "new" headless mode,
        # which Google treats like a normal browser (the old headless shell gets no results).
        browser = await p.chromium.launch(headless=headless, channel="chromium")
        try:
            yield await browser.new_context(
                user_agent=user_agent(browser.version),
                locale="en-US",
                viewport={"width": 1280, "height": 900},
            )
        finally:
            await browser.close()


async def read_labels(ctx, url: str) -> list[str]:
    """Open one results page and return the aria-labels of the flight rows."""
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Cookie/consent screen (shown in some regions): decline and continue.
        for name in ("Reject all", "Accept all"):
            btn = page.get_by_role("button", name=name)
            if await btn.count():
                await btn.first.click()
                await page.wait_for_load_state("domcontentloaded")
                break
        try:
            await page.wait_for_selector(RESULT_SEL, timeout=25000)
        except PWTimeout:
            body = (await page.inner_text("body"))[:300].replace("\n", " ")
            raise RuntimeError(f"No flight results appeared. Page starts with: {body!r}")
        await page.wait_for_timeout(1500)  # let the list finish rendering
        labels = await page.eval_on_selector_all(
            RESULT_SEL, "els => els.map(e => e.getAttribute('aria-label'))"
        )
    finally:
        await page.close()
    out, seen = [], set()
    for label in labels:
        label = (label or "").replace("Select flight", "").strip()
        if label and "Leaves " in label and label not in seen:
            seen.add(label)
            out.append(label)
    return out


# ---------------------------------------------------------------- searches


async def scrape(
    origin: str,
    destination: str,
    date: str,
    return_date: str | None = None,
    currency: str = "USD",
    max_results: int = 10,
    cabin: str = "economy",
    adults: int = 1,
    max_stops: int | None = None,
    max_price: float | None = None,
    airlines: list[str] | None = None,
    headless: bool = True,
) -> dict:
    url = build_url(origin, destination, date, return_date, currency, cabin, adults, max_stops == 0)
    async with browser_context(headless) as ctx:
        labels = await read_labels(ctx, url)
    flights = [f for f in map(parse, labels) if keep(f, max_price, max_stops, airlines)]
    return {
        "count": len(flights[:max_results]),
        "flights": flights[:max_results],
        "google_flights_link": url,
    }


async def scrape_dates(
    origin: str,
    destination: str,
    start_date: str,
    end_date: str,
    trip_length: int | None = None,
    currency: str = "USD",
    cabin: str = "economy",
    adults: int = 1,
    max_stops: int | None = None,
    headless: bool = True,
) -> dict:
    start, end = Date.fromisoformat(start_date), Date.fromisoformat(end_date)
    days = (end - start).days + 1
    if days < 1:
        raise ValueError("end_date must be on or after start_date")
    if days > MAX_DATE_SPAN:
        raise ValueError(f"Date range is limited to {MAX_DATE_SPAN} days per search")
    dates = [start + timedelta(days=i) for i in range(days)]
    sem = asyncio.Semaphore(PARALLEL_PAGES)

    async def one(ctx, d: Date) -> dict:
        ret = (d + timedelta(days=trip_length)).isoformat() if trip_length else None
        url = build_url(origin, destination, d.isoformat(), ret, currency, cabin, adults, max_stops == 0)
        row = {"date": d.isoformat(), "weekday": d.strftime("%a")}
        if ret:
            row["return_date"] = ret
        async with sem:
            try:
                flights = [f for f in map(parse, await read_labels(ctx, url)) if keep(f, None, max_stops, None)]
            except Exception as e:  # one bad day shouldn't sink the whole range
                return {**row, "error": str(e)[:120]}
        priced = [f for f in flights if price_value(f) is not None]
        if not priced:
            return {**row, "cheapest": None}
        best = min(priced, key=price_value)
        return {
            **row,
            "cheapest": best["price"],
            "airline": best.get("airline"),
            "stops": best.get("stops"),
            "google_flights_link": url,
        }

    async with browser_context(headless) as ctx:
        rows = await asyncio.gather(*(one(ctx, d) for d in dates))

    priced = [r for r in rows if r.get("cheapest")]
    best = min(priced, key=lambda r: price_value({"price": r["cheapest"]}), default=None)
    return {"cheapest_day": best, "by_date": rows}


# ---------------------------------------------------------------- price insights (diagnostic)


async def probe_insights(url: str, headless: bool = True) -> None:
    """Print whatever price-insight / price-history info Google shows for a search."""
    keys = re.compile(
        r"usually cost|prices are currently|cheaper than usual|more expensive than usual|"
        r"typical|price history|low for|high for|days ago|lowest price",
        re.I,
    )
    async with browser_context(headless) as ctx:
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_selector(RESULT_SEL, timeout=25000)
        await page.wait_for_timeout(2500)

        def show(title, lines):
            print(f"\n=== {title} ({len(lines)}) ===")
            for line in lines[:25]:
                print(" ", line[:220])

        text = await page.inner_text("body")
        show("Page text mentioning prices/history", [l.strip() for l in text.splitlines() if keys.search(l)])

        labels = await page.eval_on_selector_all(
            "[aria-label]", "els => els.map(e => e.getAttribute('aria-label'))"
        )
        show("aria-labels mentioning prices/history", [l for l in labels if l and keys.search(l)])

        buttons = page.get_by_role("button", name=re.compile("price history|price graph|date grid", re.I))
        n = await buttons.count()
        print(f"\n=== Price history buttons found: {n} ===")
        for i in range(n):
            print("  ", await buttons.nth(i).get_attribute("aria-label") or await buttons.nth(i).inner_text())
        if n:
            await buttons.first.click()
            await page.wait_for_timeout(3000)
            text2 = await page.inner_text("body")
            new = [l.strip() for l in text2.splitlines() if l.strip() and l not in text]
            show("New text after opening price history", new)
            labels2 = await page.eval_on_selector_all(
                "[aria-label]", "els => els.map(e => e.getAttribute('aria-label'))"
            )
            show("New aria-labels after opening", [l for l in labels2 if l and l not in labels])
        await page.close()


# ---------------------------------------------------------------- MCP tools

mcp = FastMCP("flights-pw")


@mcp.tool()
async def search_flights(
    origin: str,
    destination: str,
    date: str,
    return_date: str | None = None,
    cabin: str = "economy",
    adults: int = 1,
    max_stops: int | None = None,
    max_price: float | None = None,
    airlines: list[str] | None = None,
    currency: str = "USD",
    max_results: int = 10,
) -> dict:
    """Search Google Flights for one date and return the top results as compact data.

    origin/destination: IATA airport codes (e.g. "LIM", "MIA").
    date / return_date: YYYY-MM-DD. Omit return_date for one-way.
    cabin: economy, premium_economy, business or first.
    adults: number of adult passengers. Prices are the total for all passengers.
    max_stops: 0 for nonstop only, 1 for at most one stop.
    max_price: drop flights above this total price (in `currency`).
    airlines: keep only these airlines, by name (e.g. ["LATAM", "Avianca"]).
    For round trips, prices are the round-trip total and the list shows outbound options.
    The response includes google_flights_link: open it to see the same search and book.
    """
    return await scrape(
        origin, destination, date, return_date, currency, max_results,
        cabin, adults, max_stops, max_price, airlines,
    )


@mcp.tool()
async def find_cheapest_dates(
    origin: str,
    destination: str,
    start_date: str,
    end_date: str,
    trip_length: int | None = None,
    cabin: str = "economy",
    adults: int = 1,
    max_stops: int | None = None,
    currency: str = "USD",
) -> dict:
    """Find the cheapest day to fly within a date range (up to 14 days).

    Searches every departure date from start_date to end_date (YYYY-MM-DD) and returns
    the cheapest flight for each day plus the overall cheapest_day.
    trip_length: for round trips, nights between departure and return (e.g. 7).
    Omit it for one-way. Takes roughly 10-40 seconds depending on the range.
    """
    return await scrape_dates(
        origin, destination, start_date, end_date, trip_length,
        currency, cabin, adults, max_stops,
    )


# ---------------------------------------------------------------- CLI


def main() -> None:
    args = sys.argv[1:]
    show = "--show" in args
    args = [a for a in args if a != "--show"]
    if args[:1] == ["--install-browser"]:
        sys.exit(subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"]))
    if args[:1] == ["--test"]:
        rest = args[1:]
        if len(rest) < 3:
            sys.exit("usage: --test ORIGIN DEST YYYY-MM-DD [RETURN_DATE] [--show]")
        res = asyncio.run(
            scrape(rest[0], rest[1], rest[2], rest[3] if len(rest) > 3 else None, headless=not show)
        )
        for f in res["flights"]:
            print(" | ".join(str(v) for v in f.values()))
        print(f"\n{res['count']} flights · {res['google_flights_link']}")
        return
    if args[:1] == ["--insights"]:
        rest = args[1:]
        url = build_url(rest[0], rest[1], rest[2], rest[3] if len(rest) > 3 else None)
        print(url)
        asyncio.run(probe_insights(url, headless=not show))
        return
    if args[:1] == ["--dates"]:
        rest = args[1:]
        if len(rest) < 4:
            sys.exit("usage: --dates ORIGIN DEST START END [TRIP_LENGTH] [--show]")
        res = asyncio.run(
            scrape_dates(rest[0], rest[1], rest[2], rest[3],
                         int(rest[4]) if len(rest) > 4 else None, headless=not show)
        )
        for r in res["by_date"]:
            print(f"{r['date']} {r['weekday']}  {r.get('cheapest') or r.get('error') or '-'}  {r.get('airline', '')}")
        if res["cheapest_day"]:
            print(f"\nCheapest: {res['cheapest_day']['date']} · {res['cheapest_day']['cheapest']}")
        return
    mcp.run()


if __name__ == "__main__":
    main()
