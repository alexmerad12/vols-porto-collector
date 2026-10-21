"""Records real fli answers as test fixtures (tests/fixtures/*.json).

    python diagnostics/record_fixture.py 2027-02-10 2027-01-20 2027-02-03

Arguments: a one-way date (YUL->OPO), then a round-trip pair (out, back). Re-record when fli's
models change; the tests read these files with FlightResult.model_validate.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fli.models import FlightSearchFilters, MaxStops, PassengerInfo, SeatType, SortBy, TripType  # noqa: E402
from fli.search import SearchFlights  # noqa: E402

from collector.google import COUNTRY, CURRENCY, LANGUAGE, _segment  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def main() -> None:
    one_way, rt_out, rt_back = (date.fromisoformat(a) for a in sys.argv[1:4])
    OUT.mkdir(parents=True, exist_ok=True)

    ow = FlightSearchFilters(
        trip_type=TripType.ONE_WAY, passenger_info=PassengerInfo(adults=1),
        flight_segments=[_segment("YUL", "OPO", one_way)], seat_type=SeatType.ECONOMY,
        stops=MaxStops.ANY, sort_by=SortBy.CHEAPEST,
    )
    flights = SearchFlights().search(ow, top_n=0, currency=CURRENCY, language=LANGUAGE, country=COUNTRY) or []
    path = OUT / f"oneway_YUL-OPO_{one_way}.json"
    path.write_text(json.dumps([f.model_dump(mode="json") for f in flights], indent=1), encoding="utf-8")
    print(f"{path.name}: {len(flights)} flights")

    rt = FlightSearchFilters(
        trip_type=TripType.ROUND_TRIP, passenger_info=PassengerInfo(adults=1),
        flight_segments=[_segment("YUL", "OPO", rt_out), _segment("OPO", "YUL", rt_back)],
        seat_type=SeatType.ECONOMY, stops=MaxStops.ANY, sort_by=SortBy.CHEAPEST,
    )
    pairs = SearchFlights().search(rt, top_n=1, currency=CURRENCY, language=LANGUAGE, country=COUNTRY) or []
    path = OUT / f"roundtrip_YUL-OPO_{rt_out}_{rt_back}.json"
    path.write_text(json.dumps([[f.model_dump(mode="json") for f in pair] for pair in pairs], indent=1), encoding="utf-8")
    print(f"{path.name}: {len(pairs)} pairs")


if __name__ == "__main__":
    main()
