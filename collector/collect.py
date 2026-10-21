"""One collector run. GitHub Actions starts it every 2 hours (collect.yml); it can also run by hand:

    python -m collector.collect --budget 20

Each run does a slice of work so the load on Google stays small and even:
  1. One-way price calendars (YUL->OPO, OPO->YUL; any stops, one stop or fewer, nonstop),
     when the last sweep is older than --ow-every hours.
  2. Round-trip price calendars, one per trip length of the watched window (trip_watch), the
     oldest ones first, --rt-per-run lengths per run (each length = any + one stop or fewer).
  3. Single-date searches from the database's queue (due_searches), --budget per run.
  4. Once a day, the --rt-checks best round-trip tickets of the watched window in detail.

Three failed Google reads in a row stop the run as "blocked". The run exits with code 1 when it
was blocked, crashed, or more than half of its reads failed, so GitHub mails the failure.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import random
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

from collector import google as google_module
from collector.store import Store

MONTREAL = ZoneInfo("America/Toronto")
ROUTES = {1: ("YUL", "OPO"), 2: ("OPO", "YUL")}
OW_STOPS = ("any", "le1", "nonstop")
RT_STOPS = ("any", "le1")
CALENDAR_DAYS = 300  # Google's calendar answers up to about 300 days ahead (fli caps at 305)
MAX_RT_LENGTHS = 30


class Blocked(RuntimeError):
    pass


@dataclass
class Stats:
    calendar_requests: int = 0
    calendar_rows: int = 0
    searches_ok: int = 0
    searches_failed: int = 0
    searches_empty: int = 0  # empty pages past the published schedule (expected, not failures)
    round_trips_ok: int = 0
    failures_in_a_row: int = 0
    errors: list[str] = field(default_factory=list)

    def failed(self, what: str, error: str | None) -> None:
        self.failures_in_a_row += 1
        self.errors.append(f"{what}: {error}")
        if self.failures_in_a_row >= 3:
            raise Blocked(f"3 failed Google reads in a row, last: {what}: {error}")

    def succeeded(self) -> None:
        self.failures_in_a_row = 0


def montreal_today(now: datetime) -> date:
    return now.astimezone(MONTREAL).date()


def is_stale(last: datetime | None, hours: float, now: datetime) -> bool:
    return last is None or now - last >= timedelta(hours=hours * 0.9)


def parse_ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def price_rows(route_id: int, rows: list, trip: str, nights: int, stops: str) -> list[dict[str, Any]]:
    return [
        {
            "route_id": route_id,
            "depart_date": r.depart_date,
            "trip": trip,
            "nights": nights,
            "stops": stops,
            "source": "calendar",
            "price": r.price,
        }
        for r in rows
    ]


def sweep(
    store: Store,
    google: ModuleType,
    run_id: int,
    stats: Stats,
    route_id: int,
    trip: str,
    nights: int,
    stops_list: tuple[str, ...],
    start: date,
    end: date,
    pause: Callable[[], None],
) -> None:
    """One calendar per stops filter over [start, end]. Filtered calendars get explicit "no fare"
    days inside the range the unfiltered one covered (google.fill_missing)."""
    origin, dest = ROUTES[route_id]
    unfiltered: list = []
    for stops in stops_list:
        stats.calendar_requests += 1
        try:
            rows = google.calendar_prices(origin, dest, start, end, stops=stops, nights=nights)
        except Exception as exc:  # a failed request is logged, never stored as prices
            store.insert("calendar_sweeps", {
                "run_id": run_id, "route_id": route_id, "trip": trip, "nights": nights, "stops": stops,
                "range_start": start.isoformat(), "range_end": end.isoformat(), "rows": 0, "ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            })
            stats.failed(f"calendar {origin}-{dest} {trip}{nights or ''} {stops}", str(exc)[:200])
            pause()
            continue
        stats.succeeded()
        if stops == "any":
            unfiltered = rows
        elif unfiltered:
            rows = google.fill_missing(unfiltered, rows)
        seen_at = datetime.now(timezone.utc).isoformat()
        if rows:
            store.rpc("record_prices", p_run_id=run_id, p_seen_at=seen_at,
                      p_rows=price_rows(route_id, rows, trip, nights, stops))
        stats.calendar_rows += len(rows)
        store.insert("calendar_sweeps", {
            "run_id": run_id, "route_id": route_id, "trip": trip, "nights": nights, "stops": stops,
            "range_start": start.isoformat(), "range_end": end.isoformat(), "rows": len(rows), "ok": True,
        })
        pause()


def run(store: Store, google: ModuleType, args: argparse.Namespace, now: Callable[[], datetime],
        sleep: Callable[[float], None] = time.sleep) -> tuple[int, Stats, str | None, bool]:
    stats = Stats()
    run_id = store.insert("collector_runs", {
        "host": args.host, "trigger": args.trigger, "fli_version": _fli_version(),
    })["id"]
    error: str | None = None
    blocked = False

    def pause(low: float = 1.0, high: float = 2.5) -> Callable[[], None]:
        return lambda: sleep(random.uniform(low, high))

    try:
        started = now()
        today = montreal_today(started)
        start = today + timedelta(days=1)
        watch = store.select("trip_watch", "id=eq.1")[0]
        last = {
            (r["route_id"], r["trip"], r["nights"], r["stops"]): parse_ts(r["swept_at"])
            for r in store.rpc("last_sweeps")
        }

        # 1. one-way calendars
        for route_id in ROUTES:
            if args.force_calendar or any(is_stale(last.get((route_id, "ow", 0, s)), args.ow_every, started) for s in OW_STOPS):
                sweep(store, google, run_id, stats, route_id, "ow", 0, OW_STOPS, start,
                      today + timedelta(days=CALENDAR_DAYS), pause())

        # 2. round-trip calendars, oldest trip lengths first
        lengths = list(range(int(watch["min_nights"]), int(watch["max_nights"]) + 1))[:MAX_RT_LENGTHS]
        def rt_last(n: int) -> datetime | None:
            seen = [last.get((1, "rt", n, s)) for s in RT_STOPS]
            return None if any(t is None for t in seen) else min(seen)  # type: ignore[type-var]
        due = [n for n in lengths if args.force_calendar or is_stale(rt_last(n), args.rt_every, started)]
        due.sort(key=lambda n: rt_last(n) or datetime.min.replace(tzinfo=timezone.utc))
        for n in due[: args.rt_per_run]:
            sweep(store, google, run_id, stats, 1, "rt", n, RT_STOPS, start,
                  today + timedelta(days=CALENDAR_DAYS - n), pause())

        # 3. single-date searches
        for row in store.rpc("due_searches", p_budget=args.budget):
            origin, dest = row["origin"], row["destination"]
            day = date.fromisoformat(row["depart_date"])
            res = google.search_date(origin, dest, day)
            payload = {"route_id": row["route_id"], "depart_date": row["depart_date"],
                       "searched_at": datetime.now(timezone.utc).isoformat(), **res.to_json()}
            store.rpc("record_search", p_run_id=run_id, p=payload)
            if res.ok:
                stats.searches_ok += 1
                stats.succeeded()
            elif res.error == google.EMPTY and row.get("calendar_price") is None:
                # past the published schedule: an empty page is the expected answer
                stats.searches_empty += 1
            else:
                # includes an empty page on a date the calendar prices: Google throttling
                stats.searches_failed += 1
                stats.failed(f"search {origin}-{dest} {day}", res.error)
            pause(2.0, 4.0)()

        # 4. the best round-trip tickets in detail, once a day
        if args.rt_checks:
            latest = store.select("round_trip_checks", "select=checked_at&ok=eq.true&order=checked_at.desc&limit=1")
            if args.force_calendar or is_stale(parse_ts(latest[0]["checked_at"]) if latest else None, 20, started):
                for trip in store.rpc("top_round_trips", p_n=args.rt_checks):
                    out_day = date.fromisoformat(trip["depart_date"])
                    ret_day = out_day + timedelta(days=int(trip["nights"]))
                    rt = google.search_round_trip("YUL", "OPO", out_day, ret_day)
                    store.insert("round_trip_checks", {
                        "run_id": run_id, "out_date": out_day.isoformat(), "ret_date": ret_day.isoformat(),
                        "ok": rt.ok, "price": rt.price, "airlines": rt.airlines or None,
                        "routing_out": rt.routing_out, "routing_ret": rt.routing_ret,
                        "legs": [asdict(leg) for leg in rt.legs] or None, "error": rt.error,
                    })
                    if rt.ok:
                        stats.round_trips_ok += 1
                        stats.succeeded()
                    else:
                        stats.failed(f"round trip {out_day}/{ret_day}", rt.error)
                    pause(2.0, 4.0)()
    except Blocked as exc:
        blocked = True
        error = str(exc)
    except Exception as exc:  # recorded on the run, then the run fails loudly
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        reads = stats.calendar_requests + stats.searches_ok + stats.searches_failed
        failed_reads = len(stats.errors)
        ok = error is None and not (reads and failed_reads * 2 > reads)
        store.update("collector_runs", run_id, {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "calendar_requests": stats.calendar_requests,
            "calendar_rows": stats.calendar_rows,
            "searches_ok": stats.searches_ok,
            "searches_failed": stats.searches_failed,
            "round_trips_ok": stats.round_trips_ok,
            "blocked": blocked,
            "ok": ok,
            "error": error or ("; ".join(stats.errors[:5]) if stats.errors else None),
        })
    return run_id, stats, error, ok


def _fli_version() -> str:
    try:
        return importlib.metadata.version("flights")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--budget", type=int, default=20, help="single-date searches this run")
    p.add_argument("--ow-every", type=float, default=6, help="hours between one-way calendar sweeps")
    p.add_argument("--rt-every", type=float, default=20, help="hours between sweeps of one trip length")
    p.add_argument("--rt-per-run", type=int, default=4, help="trip lengths swept per run")
    p.add_argument("--rt-checks", type=int, default=3, help="best round trips searched in detail, daily")
    p.add_argument("--force-calendar", action="store_true", help="sweep every calendar now")
    p.add_argument("--host", default="local", choices=["github", "local"])
    p.add_argument("--trigger", default="manual")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_id, stats, error, ok = run(Store.from_env(), google_module, args, lambda: datetime.now(timezone.utc))
    print(f"run {run_id}: ok={ok} calendars={stats.calendar_requests} rows={stats.calendar_rows} "
          f"searches={stats.searches_ok} ok / {stats.searches_failed} failed / {stats.searches_empty} past the schedule, "
          f"round trips={stats.round_trips_ok}")
    if error:
        print(f"error: {error}", file=sys.stderr)
    for e in stats.errors[:10]:
        print(f"  failed read: {e}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
