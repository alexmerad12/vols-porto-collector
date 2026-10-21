"""Google Flights reads, through fli (PyPI `flights`).

Two reads, both in CAD for a Canadian point of sale (gl=CA):

- `calendar_prices`: Google's price calendar, the lowest one-way fare per departure date, for a
  date range (fli splits ranges over 61 days into several requests). Google serves it up to
  about 9 months ahead.
- `search_date`: every flight Google lists for one date, grouped by routing
  (YUL-OPO, YUL-LIS-OPO, ...), keeping the cheapest itinerary of each routing.

Nothing here writes anywhere. A failed or non-CAD read comes back with ok=False and is never
turned into "no flight".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from fli.models import (
    Airport,
    DateSearchFilters,
    FlightResult,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
    TripType,
)
from fli.search import SearchDates, SearchFlights

CURRENCY = "CAD"
COUNTRY = "CA"
LANGUAGE = "en"

STOPS = {
    "any": MaxStops.ANY,
    "le1": MaxStops.ONE_STOP_OR_FEWER,
    "nonstop": MaxStops.NON_STOP,
}

# How many routings of one date we keep (the cheapest ones). The cheapest nonstop and the
# cheapest one-stop are always kept on top of these, so the globe never loses the direct arc.
MAX_ROUTINGS = 10


@dataclass
class CalendarPrice:
    depart_date: str  # YYYY-MM-DD
    price: float | None  # None = Google returned the date with no fare
    currency: str


@dataclass
class Leg:
    airline: str
    flight_number: str
    origin: str
    destination: str
    departs: str  # local time at the origin, ISO
    arrives: str  # local time at the destination, ISO
    duration_min: int
    operating_airline: str | None = None


@dataclass
class Itinerary:
    routing: str
    price: float
    stops: int
    duration_min: int
    airlines: list[str]
    self_transfer: bool
    legs: list[Leg] = field(default_factory=list)


@dataclass
class DateSearch:
    ok: bool
    error: str | None = None
    currency: str | None = None
    n_flights: int = 0
    min_any: float | None = None
    min_le1: float | None = None
    min_nonstop: float | None = None
    itineraries: list[Itinerary] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def _airport(code: str) -> Airport:
    return getattr(Airport, code)


def _code(value) -> str | None:
    """fli enums carry the IATA code as their name; plain strings pass through."""
    if value is None:
        return None
    return getattr(value, "name", str(value))


def _segment(origin: str, dest: str, day: date) -> FlightSegment:
    return FlightSegment(
        departure_airport=[[_airport(origin), 0]],
        arrival_airport=[[_airport(dest), 0]],
        travel_date=day.isoformat(),
    )


def calendar_prices(origin: str, dest: str, start: date, end: date, stops: str = "any") -> list[CalendarPrice]:
    filters = DateSearchFilters(
        trip_type=TripType.ONE_WAY,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[_segment(origin, dest, start)],
        from_date=start.isoformat(),
        to_date=end.isoformat(),
        stops=STOPS[stops],
        seat_type=SeatType.ECONOMY,
    )
    rows = SearchDates().search(filters, currency=CURRENCY, language=LANGUAGE, country=COUNTRY) or []
    out: list[CalendarPrice] = []
    for row in rows:
        day = row.date[0].date().isoformat()
        out.append(CalendarPrice(depart_date=day, price=float(row.price) if row.price else None, currency=row.currency))
    return out


def _itinerary(flight: FlightResult) -> Itinerary:
    legs = [
        Leg(
            airline=_code(leg.airline) or "",
            flight_number=str(leg.flight_number),
            origin=_code(leg.departure_airport) or "",
            destination=_code(leg.arrival_airport) or "",
            departs=leg.departure_datetime.isoformat(),
            arrives=leg.arrival_datetime.isoformat(),
            duration_min=int(leg.duration),
            operating_airline=_code(leg.operating_airline),
        )
        for leg in flight.legs
    ]
    airports = [legs[0].origin] + [leg.destination for leg in legs] if legs else []
    airlines: list[str] = []
    for leg in legs:
        if leg.airline not in airlines:
            airlines.append(leg.airline)
    return Itinerary(
        routing="-".join(airports),
        price=float(flight.price),
        stops=int(flight.stops),
        duration_min=int(flight.duration),
        airlines=airlines,
        self_transfer=bool(getattr(flight, "self_transfer", False)),
        legs=legs,
    )


def summarize(flights: list[FlightResult] | None) -> DateSearch:
    """Turn fli's flight list into one DateSearch. Pure, so it is tested on recorded results."""
    if not flights:
        return DateSearch(ok=True, currency=CURRENCY, n_flights=0)
    currencies = {f.currency for f in flights if getattr(f, "currency", None)}
    if currencies and currencies != {CURRENCY}:
        return DateSearch(ok=False, error=f"currency {sorted(currencies)}", n_flights=len(flights))

    itineraries = sorted((_itinerary(f) for f in flights if f.price), key=lambda i: (i.price, i.duration_min))
    best_by_routing: dict[str, Itinerary] = {}
    for it in itineraries:
        best_by_routing.setdefault(it.routing, it)
    ranked = sorted(best_by_routing.values(), key=lambda i: (i.price, i.duration_min))

    def cheapest(max_stops: int) -> Itinerary | None:
        return next((i for i in ranked if i.stops <= max_stops), None)

    kept = ranked[:MAX_ROUTINGS]
    for must in (cheapest(0), cheapest(1)):
        if must is not None and must not in kept:
            kept.append(must)

    nonstop, le1 = cheapest(0), cheapest(1)
    return DateSearch(
        ok=True,
        currency=CURRENCY,
        n_flights=len(flights),
        min_any=ranked[0].price if ranked else None,
        min_le1=le1.price if le1 else None,
        min_nonstop=nonstop.price if nonstop else None,
        itineraries=kept,
    )


def search_date(origin: str, dest: str, day: date) -> DateSearch:
    filters = FlightSearchFilters(
        trip_type=TripType.ONE_WAY,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[_segment(origin, dest, day)],
        seat_type=SeatType.ECONOMY,
        stops=MaxStops.ANY,
        sort_by=SortBy.CHEAPEST,
    )
    try:
        flights = SearchFlights().search(filters, top_n=0, currency=CURRENCY, language=LANGUAGE, country=COUNTRY)
    except Exception as exc:  # fli raises on HTTP failures and unparseable pages
        return DateSearch(ok=False, error=f"{type(exc).__name__}: {str(exc)[:300]}")
    return summarize(flights)  # type: ignore[arg-type]
