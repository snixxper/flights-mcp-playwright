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
Run as an MCP server (what Claude Desktop does):
    uv run --script flights_pw.py
"""
import asyncio
import re
import subprocess
import sys
import urllib.parse

from mcp.server.fastmcp import FastMCP
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
RESULT_SEL = '[aria-label*="Total duration"]'


def build_url(origin: str, destination: str, date: str, return_date: str | None, currency: str) -> str:
    q = f"Flights to {destination.upper()} from {origin.upper()} on {date}"
    q += f" through {return_date}" if return_date else " oneway"
    params = {"q": q, "curr": currency.upper(), "hl": "en"}
    return "https://www.google.com/travel/flights?" + urllib.parse.urlencode(params)


def parse(label: str) -> dict:
    """Turn Google's aria-label for one result into a compact dict."""

    def m(pattern: str) -> str | None:
        r = re.search(pattern, label)
        return r.group(1).strip() if r else None

    d = {
        "price": m(r"^From (.+?)\.") or ("unavailable" if label.startswith("Total price is unavailable") else None),
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


async def scrape(
    origin: str,
    destination: str,
    date: str,
    return_date: str | None = None,
    currency: str = "USD",
    max_results: int = 10,
    headless: bool = True,
) -> dict:
    url = build_url(origin, destination, date, return_date, currency)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        try:
            ctx = await browser.new_context(
                user_agent=UA, locale="en-US", viewport={"width": 1280, "height": 900}
            )
            page = await ctx.new_page()
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
            await browser.close()

    seen, flights = set(), []
    for label in labels:
        label = (label or "").replace("Select flight", "").strip()
        if label and "Leaves " in label and label not in seen:
            seen.add(label)
            flights.append(parse(label))
        if len(flights) >= max_results:
            break
    return {"count": len(flights), "flights": flights, "url": url}


mcp = FastMCP("flights-pw")


@mcp.tool()
async def search_flights(
    origin: str,
    destination: str,
    date: str,
    return_date: str | None = None,
    currency: str = "USD",
    max_results: int = 10,
) -> dict:
    """Search Google Flights and return the top results as compact data.

    origin/destination: IATA airport codes (e.g. "LIM", "MIA").
    date / return_date: YYYY-MM-DD. Omit return_date for one-way.
    For round trips, prices are the round-trip total and the list shows outbound options.
    """
    return await scrape(origin, destination, date, return_date, currency, max_results)


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--install-browser"]:
        sys.exit(subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"]))
    if args[:1] == ["--test"]:
        rest = args[1:]
        show = "--show" in rest
        rest = [a for a in rest if a != "--show"]
        if len(rest) < 3:
            sys.exit("usage: --test ORIGIN DEST YYYY-MM-DD [RETURN_DATE] [--show]")
        res = asyncio.run(
            scrape(rest[0], rest[1], rest[2], rest[3] if len(rest) > 3 else None, headless=not show)
        )
        for f in res["flights"]:
            print(" | ".join(str(v) for v in f.values()))
        print(f"\n{res['count']} flights · {res['url']}")
        return
    mcp.run()


if __name__ == "__main__":
    main()
