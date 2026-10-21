import json
from pathlib import Path

from fli.models import FlightResult

from collector import google
from collector.google import CalendarPrice, cheapest_round_trip, fill_missing, summarize

FIXTURES = Path(__file__).parent / "fixtures"


def load_oneway():
    data = json.loads((FIXTURES / "oneway_YUL-OPO_2027-02-10.json").read_text(encoding="utf-8"))
    return [FlightResult.model_validate(d) for d in data]


def load_roundtrip():
    data = json.loads((FIXTURES / "roundtrip_YUL-OPO_2027-01-20_2027-02-03.json").read_text(encoding="utf-8"))
    return [tuple(FlightResult.model_validate(f) for f in pair) for pair in data]


def test_summary_minimums_match_a_brute_force_read_of_every_flight():
    flights = load_oneway()
    res = summarize(flights)
    assert res.ok and res.currency == "CAD"
    assert res.n_flights == len(flights) == 87
    # Google lists some flights without a price; they are never a minimum.
    priced = [f for f in flights if f.price]
    assert priced
    assert res.min_any == min(f.price for f in priced)
    le1 = [f.price for f in priced if f.stops <= 1]
    assert res.min_le1 == (min(le1) if le1 else None)
    nonstop = [f.price for f in priced if f.stops == 0]
    assert res.min_nonstop == (min(nonstop) if nonstop else None)


def test_one_itinerary_per_routing_cheapest_first_and_the_one_stop_kept():
    res = summarize(load_oneway())
    routings = [i.routing for i in res.itineraries]
    assert len(routings) == len(set(routings))
    assert all(r.startswith("YUL-") and r.endswith("-OPO") for r in routings)
    ranked = res.itineraries[: google.MAX_ROUTINGS]
    assert [i.price for i in ranked] == sorted(i.price for i in ranked)
    assert any(i.stops <= 1 and i.price == res.min_le1 for i in res.itineraries)
    assert len(res.itineraries) <= google.MAX_ROUTINGS + 2


def test_each_routing_keeps_its_cheapest_flight():
    flights = load_oneway()
    res = summarize(flights)
    for it in res.itineraries:
        same = [f.price for f in flights if f.price and "-".join([f.legs[0].departure_airport.name] + [leg.arrival_airport.name for leg in f.legs]) == it.routing]
        assert it.price == min(same)


def test_legs_carry_airports_and_times():
    res = summarize(load_oneway())
    first = res.itineraries[0]
    assert first.legs[0].origin == "YUL" and first.legs[-1].destination == "OPO"
    assert len(first.legs) == first.stops + 1
    assert first.legs[0].departs.startswith("2027-02-10")


def test_a_price_in_another_currency_fails_the_read():
    flights = load_oneway()
    flights[3] = flights[3].model_copy(update={"currency": "USD"})
    res = summarize(flights)
    assert not res.ok and "USD" in (res.error or "")
    assert res.min_any is None


def test_an_empty_answer_is_a_failed_read_never_no_flight():
    res = summarize([])
    assert not res.ok and res.error == google.EMPTY
    assert res.n_flights == 0 and res.min_any is None and res.itineraries == []


def test_an_empty_round_trip_answer_is_a_failed_read():
    rt = cheapest_round_trip([])
    assert not rt.ok and rt.error == google.EMPTY and rt.price is None


def test_round_trip_takes_the_cheapest_ticket_and_both_routings():
    pairs = load_roundtrip()
    rt = cheapest_round_trip(pairs)
    assert rt.ok
    assert rt.price == min(p[0].price for p in pairs)
    assert rt.routing_out.startswith("YUL-") and rt.routing_out.endswith("-OPO")
    assert rt.routing_ret.startswith("OPO-") and rt.routing_ret.endswith("-YUL")


def test_filtered_calendar_days_missing_inside_the_covered_range_become_no_fare():
    full = [CalendarPrice(d, p, "CAD") for d, p in [("2027-05-01", 600), ("2027-05-02", 610), ("2027-05-03", 620)]]
    nonstop = [CalendarPrice("2027-05-02", 900, "CAD")]
    out = {r.depart_date: r.price for r in fill_missing(full, nonstop)}
    assert out == {"2027-05-01": None, "2027-05-02": 900, "2027-05-03": None}


def test_nothing_is_invented_when_the_unfiltered_calendar_is_empty():
    nonstop = [CalendarPrice("2027-05-02", 900, "CAD")]
    assert fill_missing([], nonstop) == nonstop
