"""Can this machine read Google Flights prices, in CAD, without being blocked?

Run it on a new host (a GitHub runner, a laptop) before trusting the collector there:

    python diagnostics/check_google.py

It prints one JSON summary: a 120-day calendar per direction, three single-date searches,
and a burst of 20 single-date searches with the pause the collector uses. Nothing is written
anywhere.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.google import calendar_prices, search_date  # noqa: E402

ROUTES = [("YUL", "OPO"), ("OPO", "YUL")]


def main() -> None:
    today = date.today()
    summary: dict = {"host_checked_on": today.isoformat(), "calendar": {}, "dates": {}, "burst": {}}

    for origin, dest in ROUTES:
        t = time.time()
        rows = calendar_prices(origin, dest, today + timedelta(days=1), today + timedelta(days=120), "any")
        prices = [r.price for r in rows if r.price is not None]
        summary["calendar"][f"{origin}-{dest}"] = {
            "days": len(rows),
            "min": min(prices) if prices else None,
            "max": max(prices) if prices else None,
            "currencies": sorted({r.currency for r in rows}),
            "seconds": round(time.time() - t, 1),
        }

    for d in ["2027-01-13", "2027-02-10", "2027-03-10"]:
        for origin, dest in ROUTES:
            t = time.time()
            res = search_date(origin, dest, date.fromisoformat(d))
            summary["dates"][f"{origin}-{dest} {d}"] = {
                "ok": res.ok,
                "flights": res.n_flights,
                "min": res.min_any,
                "currency": res.currency,
                "seconds": round(time.time() - t, 1),
                "error": res.error,
            }

    ok = failed = 0
    t = time.time()
    start = today + timedelta(days=30)
    for i in range(20):
        res = search_date("YUL", "OPO", start + timedelta(days=i))
        ok += res.ok
        failed += not res.ok
        time.sleep(1.5)
    summary["burst"] = {"ok": ok, "failed": failed, "seconds": round(time.time() - t, 1)}

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
