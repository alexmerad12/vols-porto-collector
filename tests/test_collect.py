from datetime import datetime, timezone
from types import SimpleNamespace

from collector import collect
from collector.google import CalendarPrice, DateSearch, RoundTrip, fill_missing

NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, due=None):
        self.rows: dict[str, list] = {}
        self.rpcs: list[tuple[str, dict]] = []
        self.updates: list[dict] = []
        self.due = due or []

    def insert(self, table, row):
        self.rows.setdefault(table, []).append(row)
        return {"id": 7, **row}

    def update(self, table, row_id, fields):
        self.updates.append(fields)

    def select(self, table, query):
        if table == "trip_watch":
            return [{"min_nights": 7, "max_nights": 8}]
        return []

    def rpc(self, fn, **params):
        self.rpcs.append((fn, params))
        if fn == "last_sweeps":
            return []
        if fn == "due_searches":
            return self.due
        if fn == "top_round_trips":
            return [{"depart_date": "2027-01-20", "nights": 14, "price": 728}]
        return {}


def fake_google(search_ok=True, calendar_raises=False):
    def calendar_prices(origin, dest, start, end, stops="any", nights=0):
        if calendar_raises:
            raise RuntimeError("HTTP 429")
        if stops == "nonstop":
            return [CalendarPrice("2026-11-02", 900.0, "CAD")]
        return [CalendarPrice("2026-11-01", 500.0, "CAD"), CalendarPrice("2026-11-02", 510.0, "CAD")]

    def search_date(origin, dest, day):
        return DateSearch(ok=True, currency="CAD", n_flights=2, min_any=500.0) if search_ok else DateSearch(ok=False, error="SearchParseError")

    def search_round_trip(origin, dest, out_day, ret_day):
        return RoundTrip(ok=True, price=728.0, airlines=["TP"], routing_out="YUL-LIS-OPO", routing_ret="OPO-LIS-YUL")

    return SimpleNamespace(calendar_prices=calendar_prices, search_date=search_date,
                           search_round_trip=search_round_trip, fill_missing=fill_missing)


def args(**kw):
    base = dict(budget=20, ow_every=6, rt_every=20, rt_per_run=4, rt_checks=3, force_calendar=False, host="local", trigger="test")
    base.update(kw)
    return SimpleNamespace(**base)


def due_rows(n):
    return [{"route_id": 1, "origin": "YUL", "destination": "OPO", "depart_date": f"2027-01-{i + 10:02d}"} for i in range(n)]


def test_a_normal_run_sweeps_every_calendar_searches_the_queue_and_finishes_ok():
    store = FakeStore(due=due_rows(4))
    _, stats, error, ok = collect.run(store, fake_google(), args(), lambda: NOW, sleep=lambda s: None)
    assert ok and error is None
    # 2 routes x 3 one-way filters + 2 trip lengths x 2 round-trip filters
    assert stats.calendar_requests == 6 + 4
    assert stats.searches_ok == 4
    recorded = [p for fn, p in store.rpcs if fn == "record_prices"]
    nonstop = [r for call in recorded for r in call["p_rows"] if r["stops"] == "nonstop"]
    assert {r["depart_date"]: r["price"] for r in nonstop if r["route_id"] == 1} == {"2026-11-01": None, "2026-11-02": 900.0}
    rt = [r for call in recorded for r in call["p_rows"] if r["trip"] == "rt"]
    assert {r["nights"] for r in rt} == {7, 8}
    assert store.updates[-1]["ok"] is True and store.updates[-1]["blocked"] is False
    assert len(store.rows["round_trip_checks"]) == 1


def test_three_failed_reads_in_a_row_stop_the_run_as_blocked():
    store = FakeStore(due=due_rows(10))
    _, stats, error, ok = collect.run(store, fake_google(search_ok=False), args(rt_checks=0), lambda: NOW, sleep=lambda s: None)
    assert not ok
    assert stats.searches_failed == 3
    assert store.updates[-1]["blocked"] is True
    # every failed search is still recorded, as a failure
    searches = [p["p"] for fn, p in store.rpcs if fn == "record_search"]
    assert len(searches) == 3 and all(s["ok"] is False for s in searches)


def test_a_failed_calendar_is_logged_and_never_written_as_prices():
    store = FakeStore()
    _, stats, error, ok = collect.run(store, fake_google(calendar_raises=True), args(rt_checks=0), lambda: NOW, sleep=lambda s: None)
    assert not ok and store.updates[-1]["blocked"] is True
    assert not [fn for fn, _ in store.rpcs if fn == "record_prices"]
    assert all(s["ok"] is False for s in store.rows["calendar_sweeps"])


def test_fresh_calendars_are_not_swept_again():
    store = FakeStore()
    fresh = NOW.isoformat()
    store.rpc = lambda fn, **p: (
        [{"route_id": r, "trip": "ow", "nights": 0, "stops": s, "swept_at": fresh} for r in (1, 2) for s in ("any", "le1", "nonstop")]
        + [{"route_id": 1, "trip": "rt", "nights": n, "stops": s, "swept_at": fresh} for n in (7, 8) for s in ("any", "le1")]
        if fn == "last_sweeps" else [] if fn in ("due_searches", "top_round_trips") else {}
    )
    _, stats, error, ok = collect.run(store, fake_google(), args(rt_checks=0), lambda: NOW, sleep=lambda s: None)
    assert ok and stats.calendar_requests == 0
