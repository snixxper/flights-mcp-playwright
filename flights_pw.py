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

from mcp.server.fastmcp import Context, FastMCP
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
MAX_TRIP_SEARCHES = 24  # page loads per find_trip_deals call (plus up to 5 for price history)


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


def num(text: str) -> float:
    return float(text.replace(",", ""))


HISTORY_RE = re.compile(r"^(today|\d+ days? ago)\s*[-–]\s*\D*?([\d,.]+)\s*$", re.I)


async def read_insights(page) -> dict | None:
    """Google's price insights for the open search: rating, typical range, price history."""
    btn = page.get_by_role("button", name=re.compile(r"view price history", re.I))
    try:
        if await btn.count():
            await btn.first.click()
            await page.wait_for_timeout(2500)
    except Exception:
        pass
    text = await page.inner_text("body")
    labels = await page.eval_on_selector_all(
        "[aria-label]", "els => els.map(e => e.getAttribute('aria-label'))"
    )
    ins: dict = {}
    m = re.search(r"Prices are currently (low|typical|high)", text, re.I)
    if m:
        ins["google_rating"] = m.group(1).lower()
    m = re.search(r"usually cost between\s*\D*?([\d,.]+)\s*[–-]\s*\D*?([\d,.]+)", text, re.I)
    if m:
        ins["typical_range"] = [num(m.group(1)), num(m.group(2))]
    points = set()
    for label in labels:
        h = HISTORY_RE.match((label or "").strip())
        if h:
            days = 0 if h.group(1).lower() == "today" else int(re.match(r"\d+", h.group(1)).group())
            points.add((days, num(h.group(2))))
    if points:
        hist = sorted(points, reverse=True)  # oldest first
        prices = [p for _, p in hist]
        ins["history"] = {
            "days": hist[0][0],
            "min": min(prices),
            "max": max(prices),
            "average": round(sum(prices) / len(prices)),
            "weekly": [[d, p] for d, p in hist if d % 7 == hist[0][0] % 7],
        }
    return ins or None


async def read_labels(ctx, url: str, with_insights: bool = False) -> tuple[list[str], dict | None]:
    """Open one results page; return the flight rows' aria-labels (and price insights)."""
    page = await ctx.new_page()
    insights = None
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
        if with_insights:
            insights = await read_insights(page)
    finally:
        await page.close()
    out, seen = [], set()
    for label in labels:
        label = (label or "").replace("Select flight", "").strip()
        if label and "Leaves " in label and label not in seen:
            seen.add(label)
            out.append(label)
    return out, insights


def rate_deal(price: float | None, ins: dict | None = None, median: float | None = None) -> dict | None:
    """Say how cheap a price is: against this search's price history, Google's rating,
    or (fallback) the middle price of the other dates checked."""
    if price is None:
        return None
    ins = ins or {}
    hist, rating, rng = ins.get("history"), ins.get("google_rating"), ins.get("typical_range")
    notes, pct, lowest = [], None, False
    if hist:
        pct = round((price - hist["average"]) / hist["average"] * 100)
        lowest = price <= hist["min"]
        if lowest:
            notes.append(f"lowest price for these dates in the last {hist['days']} days")
        notes.append(
            f"{abs(pct)}% {'below' if pct < 0 else 'above'} its {hist['days']}-day average of {hist['average']:g}"
        )
    elif median:
        pct = round((price - median) / median * 100)
        notes.append(f"{abs(pct)}% {'below' if pct < 0 else 'above'} the middle price of the dates checked ({median:g})")
    if rating:
        notes.append(f"Google rates it {rating}")
    if rng:
        notes.append(f"similar trips usually cost {rng[0]:g}–{rng[1]:g}")

    if lowest or (pct is not None and pct <= -20) or (rating == "low" and (pct is None or pct <= -5)):
        label = "🔥 great deal"
    elif (pct is not None and pct <= -5) or rating == "low":
        label = "✅ good price"
    elif rating == "high" or (pct is not None and pct >= 10):
        label = "⚠️ expensive"
    else:
        label = "➖ normal price"
    out = {"label": label, "why": notes}
    if pct is not None:
        out["percent_vs_usual"] = pct
    return out


def median(values: list[float]) -> float | None:
    v = sorted(values)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


# ---------------------------------------------------------------- searches


def best_flight(labels: list[str], max_stops: int | None) -> dict | None:
    flights = [f for f in map(parse, labels) if keep(f, None, max_stops, None)]
    priced = [f for f in flights if price_value(f) is not None]
    return min(priced, key=price_value, default=None)


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
    price_insights: bool = True,
    headless: bool = True,
) -> dict:
    url = build_url(origin, destination, date, return_date, currency, cabin, adults, max_stops == 0)
    async with browser_context(headless) as ctx:
        labels, ins = await read_labels(ctx, url, with_insights=price_insights)
    flights = [f for f in map(parse, labels) if keep(f, max_price, max_stops, airlines)]
    out = {"count": len(flights[:max_results]), "flights": flights[:max_results]}
    priced = [f for f in flights if price_value(f) is not None]
    if priced:
        cheapest = min(priced, key=price_value)
        out["cheapest_deal"] = {"price": cheapest["price"], **(rate_deal(price_value(cheapest), ins) or {})}
    if ins:
        out["price_insights"] = ins
    out["google_flights_link"] = url
    return out


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

    def url_for(d: Date) -> tuple[str, str | None]:
        ret = (d + timedelta(days=trip_length)).isoformat() if trip_length else None
        return build_url(origin, destination, d.isoformat(), ret, currency, cabin, adults, max_stops == 0), ret

    async def one(ctx, d: Date) -> dict:
        url, ret = url_for(d)
        row = {"date": d.isoformat(), "weekday": d.strftime("%a")}
        if ret:
            row["return_date"] = ret
        async with sem:
            try:
                labels, _ = await read_labels(ctx, url)
            except Exception as e:  # one bad day shouldn't sink the whole range
                return {**row, "error": str(e)[:120]}
        best = best_flight(labels, max_stops)
        if not best:
            return {**row, "cheapest": None}
        return {**row, "cheapest": best["price"], "airline": best.get("airline"),
                "stops": best.get("stops"), "google_flights_link": url}

    async with browser_context(headless) as ctx:
        rows = await asyncio.gather(*(one(ctx, d) for d in dates))
        priced = [r for r in rows if r.get("cheapest") and price_value({"price": r["cheapest"]}) is not None]
        best = min(priced, key=lambda r: price_value({"price": r["cheapest"]}), default=None)
        if best:
            ins = None
            try:
                _, ins = await read_labels(ctx, best["google_flights_link"], with_insights=True)
            except Exception:
                pass
            mid = median([price_value({"price": r["cheapest"]}) for r in priced])
            best = {**best, "deal": rate_deal(price_value({"price": best["cheapest"]}), ins, mid)}
    return {"cheapest_day": best, "by_date": rows}


def spread(items: list, k: int) -> list:
    """Pick k items evenly spread over a list (keeps first and last)."""
    if len(items) <= k:
        return items
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


async def scrape_trip_deals(
    origin: str,
    destinations: list[str],
    earliest_departure: str,
    latest_return: str,
    min_nights: int,
    max_nights: int,
    adults: int = 1,
    cabin: str = "economy",
    max_stops: int | None = None,
    budget_per_person: float | None = None,
    currency: str = "USD",
    progress=None,
    headless: bool = True,
) -> dict:
    first, last = Date.fromisoformat(earliest_departure), Date.fromisoformat(latest_return)
    dests = list(dict.fromkeys(d.strip().upper() for d in destinations if d.strip()))[:5]
    if not dests:
        raise ValueError("Give at least one destination airport code")
    min_nights, max_nights = sorted((max(1, min_nights), max(1, max_nights)))
    window = (last - first).days
    if window < min_nights:
        raise ValueError(f"The travel window ({window} nights) is shorter than min_nights ({min_nights})")
    max_nights = min(max_nights, window)
    mid = (min_nights + max_nights) // 2

    def departures(nights: int) -> list[Date]:
        return [first + timedelta(days=i) for i in range(window - nights + 1)]

    # Stage 1: the middle trip length, departure dates spread over the window.
    per_dest = max(2, (MAX_TRIP_SEARCHES * 2 // 3) // len(dests))
    stage1 = [(d, dep, mid) for d in dests for dep in spread(departures(mid), per_dest)]
    results: dict = {}
    sem = asyncio.Semaphore(PARALLEL_PAGES)
    done = 0

    def url_for(d: str, dep: Date, n: int) -> str:
        ret = (dep + timedelta(days=n)).isoformat()
        return build_url(origin, d, dep.isoformat(), ret, currency, cabin, adults, max_stops == 0)

    async def run(ctx, key) -> None:
        nonlocal done
        d, dep, n = key
        url = url_for(d, dep, n)
        async with sem:
            try:
                labels, _ = await read_labels(ctx, url)
            except Exception:
                labels = []
        results[key] = (best_flight(labels, max_stops), url)
        done += 1
        if progress:
            await progress(done, MAX_TRIP_SEARCHES)

    async with browser_context(headless) as ctx:
        await asyncio.gather(*(run(ctx, k) for k in stage1))

        # Stage 2: around each destination's 2 cheapest departures, try the shortest and longest trips.
        extra = []
        for d in dests:
            found = [k for k, (f, _) in results.items() if k[0] == d and f]
            for _, dep, _ in sorted(found, key=lambda k: price_value(results[k][0]))[:2]:
                for n in sorted({min_nights, max_nights} - {mid}):
                    key = (d, dep, n)
                    if dep + timedelta(days=n) <= last and key not in results and key not in extra:
                        extra.append(key)
        extra = extra[: max(0, MAX_TRIP_SEARCHES - len(stage1))]
        await asyncio.gather(*(run(ctx, k) for k in extra))

        # Rank every option found.
        options = []
        for (d, dep, n), (f, url) in results.items():
            if not f:
                continue
            total = price_value(f)
            options.append({
                "destination": d,
                "depart": f"{dep.isoformat()} ({dep.strftime('%a')})",
                "return": f"{(dep + timedelta(days=n)).isoformat()} ({(dep + timedelta(days=n)).strftime('%a')})",
                "nights": n,
                "price": f["price"],
                "_total": total,
                "_per_person": total / adults,
                "airline": f.get("airline"),
                "stops": f.get("stops"),
                "google_flights_link": url,
            })
        options.sort(key=lambda o: o["_per_person"])
        within = [o for o in options if budget_per_person is None or o["_per_person"] <= budget_per_person]
        over_budget = bool(options) and not within
        # Best option for each destination first, so every place can be compared; then fill by price.
        pool = within or options
        top = []
        for d in dests:
            best = next((o for o in pool if o["destination"] == d), None)
            if best:
                top.append(best)
        top += [o for o in pool if o not in top][: max(0, 5 - len(top))]
        top.sort(key=lambda o: o["_per_person"])

        # How cheap? Price history for every option shown (fallback: the other dates checked).
        medians = {d: median([o["_total"] for o in options if o["destination"] == d]) for d in dests}

        async def insight(o):
            async with sem:
                return await read_labels(ctx, o["google_flights_link"], with_insights=True)

        insights = await asyncio.gather(*(insight(o) for o in top), return_exceptions=True)
        for i, o in enumerate(top):
            ins = insights[i][1] if i < len(insights) and not isinstance(insights[i], Exception) else None
            o["deal"] = rate_deal(o["_total"], ins, medians.get(o["destination"]))
            if adults > 1:
                o["price_per_person"] = round(o["_per_person"])

    seen = {}
    for o in options:
        lo, hi = seen.get(o["destination"], (o["_total"], o["_total"]))
        seen[o["destination"]] = (min(lo, o["_total"]), max(hi, o["_total"]))
    for o in top:
        o.pop("_total"), o.pop("_per_person")
    rated = [o for o in top if o.get("deal") and "percent_vs_usual" in o["deal"]]
    best_deal = min(rated, key=lambda o: o["deal"]["percent_vs_usual"], default=None)
    out = {
        "combinations_checked": len(results),
        "best_options": top,
        "biggest_discount": (
            {"destination": best_deal["destination"], "depart": best_deal["depart"],
             "price": best_deal["price"], "percent_vs_usual": best_deal["deal"]["percent_vs_usual"]}
            if best_deal else None
        ),
        "price_range_seen": {d: {"min": lo, "max": hi} for d, (lo, hi) in seen.items()},
    }
    if over_budget:
        out["note"] = f"Nothing fit the budget of {budget_per_person:g} per person; showing the cheapest options instead."
    missing = [d for d in dests if d not in seen]
    if missing:
        out["no_results_for"] = missing
    return out


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
    cheapest_deal says how cheap the cheapest flight is: against the price history for these
    exact dates (last ~60 days), Google's low/typical/high rating and its usual price range.
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
    the cheapest flight for each day plus the overall cheapest_day, with a deal rating.
    trip_length: for round trips, nights between departure and return (e.g. 7).
    Omit it for one-way. Takes roughly 10-40 seconds depending on the range.
    """
    return await scrape_dates(
        origin, destination, start_date, end_date, trip_length,
        currency, cabin, adults, max_stops,
    )


@mcp.tool()
async def find_trip_deals(
    origin: str,
    destinations: list[str],
    earliest_departure: str,
    latest_return: str,
    min_nights: int,
    max_nights: int,
    adults: int = 1,
    cabin: str = "economy",
    max_stops: int | None = None,
    budget_per_person: float | None = None,
    currency: str = "USD",
    ctx: Context | None = None,
) -> dict:
    """Find the best-value round trips that fit inside a travel window, across up to 5 destinations.

    Use this when someone knows WHEN they can travel (e.g. approved vacation days) but not the
    exact dates, or is choosing between destinations.
    earliest_departure / latest_return: the travel window, YYYY-MM-DD.
    min_nights / max_nights: trip length range (use the same number for an exact length).
    destinations: IATA codes, e.g. ["CUZ", "CTG", "EZE"].
    budget_per_person: optional, in `currency`.
    Checks up to 24 date combinations, then rates the options shown against the price history
    for those dates. The best option for every destination is always included.
    best_options are sorted by price; biggest_discount is the option furthest below its usual
    price, which can differ from the cheapest one, so mention both when they differ. Each option has a deal label (great deal / good price / normal / expensive)
    with the reasons. Takes about 30-60 seconds.
    """

    async def progress(done: int, total: int) -> None:
        if ctx:
            try:
                await ctx.report_progress(done, total)
            except Exception:
                pass

    return await scrape_trip_deals(
        origin, destinations, earliest_departure, latest_return, min_nights, max_nights,
        adults, cabin, max_stops, budget_per_person, currency, progress,
    )


@mcp.prompt(title="Plan a trip", description="Answer a few questions and catch the cheapest flights for your trip")
def plan_trip() -> str:
    return """Help me catch a cheap flight for a trip. Ask me these questions ONE AT A TIME, in the language I write in, and wait for each answer:

1. When do you want to travel? (for example "Jan 5-20, about a week", "any weekend in January", or "exactly Jan 7 to 14")
2. Where are you flying from?
3. Where to? One place, a few ideas, or "surprise me" plus a vibe (beach, mountains, cheap international...). If I say surprise me, suggest up to 5 destinations that make sense from my city and let me confirm.
4. Optional, all in one message (I can skip it): how many people, budget per person, and must-haves (nonstop only, an airline, no overnight flights).

Then turn my answers into a travel window (earliest_departure, latest_return), a trip length (min_nights, max_nights: "about a week" means 6-8; exact dates mean the same number for both) and IATA airport codes, and call find_trip_deals. If I gave exact dates for one destination, use search_flights instead.

Show the best options as a short table: destination, dates, nights, price, airline, and deal label. Then explain in one or two sentences HOW cheap the best option is, using the deal reasons (compared to the price history for those dates, lowest in N days, Google's usual range). If the biggest discount is not the cheapest option, say so: the cheapest trip and the best deal can be different. Include the Google Flights link for each option. If I asked for no overnight flights, skip options that depart between midnight and 5 AM."""


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
        if res.get("cheapest_deal"):
            d = res["cheapest_deal"]
            print(f"\nCheapest: {d['price']}  {d['label']}  ({'; '.join(d['why'])})")
        print(f"\n{res['count']} flights · {res['google_flights_link']}")
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
        b = res["cheapest_day"]
        if b:
            print(f"\nCheapest: {b['date']} · {b['cheapest']}  {b['deal']['label']}  ({'; '.join(b['deal']['why'])})")
        return
    if args[:1] == ["--deals"]:
        rest = args[1:]
        if len(rest) < 6:
            sys.exit("usage: --deals ORIGIN DEST1,DEST2 EARLIEST LATEST MIN_NIGHTS MAX_NIGHTS [--show]")

        async def progress(done, total):
            print(f"  searched {done}...", end="\r")

        res = asyncio.run(
            scrape_trip_deals(rest[0], rest[1].split(","), rest[2], rest[3], int(rest[4]), int(rest[5]),
                              progress=progress, headless=not show)
        )
        print(f"Checked {res['combinations_checked']} combinations\n")
        for o in res["best_options"]:
            print(f"{o['destination']}  {o['depart']} -> {o['return']}  {o['nights']} nights  "
                  f"{o['price']}  {o['airline']}  {o['stops']}")
            print(f"     {o['deal']['label']}: {'; '.join(o['deal']['why'])}")
        if res.get("biggest_discount"):
            b = res["biggest_discount"]
            print(f"\nBiggest discount: {b['destination']} {b['depart']} {b['price']} ({b['percent_vs_usual']}% vs usual)")
        print(f"Range seen: {res['price_range_seen']}")
        return
    mcp.run()


if __name__ == "__main__":
    main()
