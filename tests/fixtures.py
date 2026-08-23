"""Fake fast-flights objects so the whole pipeline runs offline.

Shapes mirror fast_flights.model exactly (Flights / SingleFlight / Airport /
SimpleDatetime), and the numbers come from the two real Google Flights
screenshots for Jan 2027 SJO-Tokyo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class FakeAirport:
    code: str
    name: str = ""


@dataclass
class FakeDT:
    date: tuple
    time: tuple


@dataclass
class FakeSingleFlight:
    from_airport: FakeAirport
    to_airport: FakeAirport
    departure: FakeDT
    arrival: FakeDT
    duration: int
    plane_type: str = "Boeing 777"


@dataclass
class FakeFlights:
    price: int
    airlines: list = field(default_factory=list)
    flights: list = field(default_factory=list)
    type: str = "multi"
    carbon: object = None


def leg(frm, to, dep, arr, minutes):
    """leg('SJO','ZRH',(2027,1,15,21,0),(2027,1,16,14,30), 690)"""
    return FakeSingleFlight(
        from_airport=FakeAirport(frm),
        to_airport=FakeAirport(to),
        departure=FakeDT(dep[:3], dep[3:]),
        arrival=FakeDT(arr[:3], arr[3:]),
        duration=minutes,
    )


# --- The real Edelweiss/SWISS option: $1,658, 46 hr 20 min via ZRH --------
# 690 min flown + 1350 min layover + 740 min flown = 2780 min = 46 hr 20 min.
ZRH_OPTION = FakeFlights(
    price=1658,
    airlines=["Edelweiss Air", "SWISS"],
    flights=[
        leg("SJO", "ZRH", (2027, 1, 15, 21, 0), (2027, 1, 16, 14, 30), 690),
        leg("ZRH", "NRT", (2027, 1, 17, 13, 0), (2027, 1, 18, 10, 20), 740),
        leg("NRT", "ZRH", (2027, 1, 24, 11, 0), (2027, 1, 24, 17, 30), 870),
        leg("ZRH", "SJO", (2027, 1, 25, 10, 0), (2027, 1, 25, 15, 30), 690),
    ],
)

# Air Canada via YYZ: must be rejected (Canadian transit visa).
YYZ_OPTION = FakeFlights(
    price=1897,
    airlines=["Air Canada"],
    flights=[
        leg("SJO", "YYZ", (2027, 1, 15, 8, 45), (2027, 1, 15, 16, 20), 455),
        leg("YYZ", "NRT", (2027, 1, 16, 13, 55), (2027, 1, 17, 16, 30), 815),
        leg("NRT", "YYZ", (2027, 1, 24, 10, 0), (2027, 1, 24, 9, 30), 780),
        leg("YYZ", "SJO", (2027, 1, 24, 12, 0), (2027, 1, 24, 17, 0), 300),
    ],
)

# American via DFW: must be rejected (US C-1 transit visa).
DFW_OPTION = FakeFlights(
    price=1950,
    airlines=["American"],
    flights=[
        leg("SJO", "DFW", (2027, 1, 15, 7, 16), (2027, 1, 15, 11, 30), 254),
        leg("DFW", "HND", (2027, 1, 15, 13, 9), (2027, 1, 16, 18, 25), 800),
        leg("HND", "DFW", (2027, 1, 24, 10, 0), (2027, 1, 24, 8, 0), 700),
        leg("DFW", "SJO", (2027, 1, 24, 11, 0), (2027, 1, 24, 15, 0), 240),
    ],
)

# Aeromexico via MEX: cheap and clean. 1 stop each way.
MEX_OPTION = FakeFlights(
    price=1290,
    airlines=["Aeromexico"],
    flights=[
        leg("SJO", "MEX", (2027, 2, 10, 9, 0), (2027, 2, 10, 12, 15), 195),
        leg("MEX", "NRT", (2027, 2, 10, 23, 55), (2027, 2, 12, 5, 30), 875),
        leg("NRT", "MEX", (2027, 2, 24, 15, 0), (2027, 2, 24, 13, 30), 745),
        leg("MEX", "SJO", (2027, 2, 24, 18, 0), (2027, 2, 24, 21, 15), 195),
    ],
)

# A standout fare, used to exercise the GREAT path.
MEX_GREAT = FakeFlights(
    price=1040,
    airlines=["Aeromexico"],
    flights=list(MEX_OPTION.flights),
)

# Tight 35-minute layover — should trip the min-layover guard.
TIGHT_OPTION = FakeFlights(
    price=1200,
    airlines=["Iberia"],
    flights=[
        leg("SJO", "MAD", (2027, 2, 10, 14, 0), (2027, 2, 11, 8, 0), 600),
        leg("MAD", "NRT", (2027, 2, 11, 8, 35), (2027, 2, 12, 6, 0), 830),
    ],
)

# Malformed: no legs at all.
EMPTY_OPTION = FakeFlights(price=999, airlines=["Ghost Air"], flights=[])

# Malformed: price is missing.
NO_PRICE_OPTION = FakeFlights(price=0, airlines=["Nope"], flights=list(MEX_OPTION.flights))


DEPART = date(2027, 1, 15)
RETURN = date(2027, 1, 24)
DEPART_FEB = date(2027, 2, 10)
RETURN_FEB = date(2027, 2, 24)


class FakeFetcher:
    """Stands in for get_flights. Records queries; returns canned results."""

    def __init__(self, results=None, *, fail_times: int = 0, exc=None):
        self.results = results if results is not None else [MEX_OPTION]
        self.calls: list[object] = []
        self.fail_times = fail_times
        self.exc = exc or RuntimeError("throttled")

    def __call__(self, query):
        self.calls.append(query)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc
        return list(self.results)
