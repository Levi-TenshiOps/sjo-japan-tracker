"""Visa policy for a Costa Rican passport, expressed as airport allow/deny sets.

This is the safety-critical module. Every itinerary is validated against
BANNED_AIRPORTS before it is ever shown to the user, regardless of what
filters were sent to Google. Google's `connecting_airports` is an *include*
hint, not a guarantee, so we never rely on it alone.

Tiers
-----
FREE     no paperwork at all for a Costa Rican passport
LIGHT    short online authorisation (minutes to a couple of days, ~$10-25)
BANNED   requires a real consular visa with an appointment / long process

Sources checked 2026-08: Mexico visa-free 180d; Schengen visa-free 90/180
(ETIAS delayed, and when it lands it is a ~10 min, EUR 20 online form);
UK ETA and Korea K-ETA are short online forms; US (C-1) and Canada
(transit visa) both require consular appointments -> BANNED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Tier = Literal["FREE", "LIGHT", "BANNED"]


@dataclass(frozen=True)
class Hub:
    code: str
    city: str
    country: str
    tier: Tier
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.tier != "BANNED"


# --- Connecting hubs that are actually reachable from SJO on one ticket ----
# Ordered roughly by how plausible they are as a SJO -> Japan connection.
HUBS: tuple[Hub, ...] = (
    # --- Americas -----------------------------------------------------------
    Hub("MEX", "Mexico City", "Mexico", "FREE", "Visa-free 180d. Direct MEX-NRT."),
    Hub("PTY", "Panama City", "Panama", "FREE", "Visa-free. No direct Asia flights; feeds other hubs."),
    Hub("BOG", "Bogota", "Colombia", "FREE", "Visa-free. Feeds IST/MAD."),
    Hub("GRU", "Sao Paulo", "Brazil", "FREE", "Visa-free. Feeds DOH/DXB/IST."),
    Hub("LIM", "Lima", "Peru", "FREE", "Visa-free. Feeds MAD/AMS."),
    Hub("SCL", "Santiago", "Chile", "FREE", "Visa-free. Long detour, rarely competitive."),
    # --- Europe (Schengen: visa-free 90/180) --------------------------------
    Hub("MAD", "Madrid", "Spain", "FREE", "Schengen. Iberia MAD-NRT."),
    Hub("ZRH", "Zurich", "Switzerland", "FREE", "Schengen. Edelweiss SJO-ZRH + SWISS ZRH-NRT/HND."),
    Hub("AMS", "Amsterdam", "Netherlands", "FREE", "Schengen. KLM AMS-NRT/KIX."),
    Hub("CDG", "Paris", "France", "FREE", "Schengen. AF CDG-HND/KIX."),
    Hub("FRA", "Frankfurt", "Germany", "FREE", "Schengen. LH/ANA FRA-HND/NRT."),
    Hub("MUC", "Munich", "Germany", "FREE", "Schengen."),
    Hub("LIS", "Lisbon", "Portugal", "FREE", "Schengen. Feeds other EU hubs."),
    Hub("FCO", "Rome", "Italy", "FREE", "Schengen."),
    Hub("BCN", "Barcelona", "Spain", "FREE", "Schengen."),
    # --- Europe (non-Schengen) ----------------------------------------------
    Hub("IST", "Istanbul", "Turkiye", "FREE", "Visa-free 90d. TK IST-NRT/HND."),
    Hub("LHR", "London Heathrow", "United Kingdom", "LIGHT", "UK ETA: online, ~GBP 16, usually minutes."),
    # --- Middle East ---------------------------------------------------------
    Hub("DOH", "Doha", "Qatar", "FREE", "Visa-free/VOA; airside transit needs nothing."),
    Hub("DXB", "Dubai", "United Arab Emirates", "FREE", "Visa-free. EK DXB-NRT/HND/KIX."),
    Hub("AUH", "Abu Dhabi", "United Arab Emirates", "FREE", "Visa-free. EY AUH-NRT."),
    # --- Asia ----------------------------------------------------------------
    Hub("ICN", "Seoul", "South Korea", "LIGHT", "K-ETA: online, ~USD 10. Also the open-jaw exit point."),
    Hub("SIN", "Singapore", "Singapore", "FREE", "Visa-free."),
    Hub("HKG", "Hong Kong", "Hong Kong", "FREE", "Visa-free."),
    Hub("TPE", "Taipei", "Taiwan", "FREE", "Visa-exempt entry."),
    Hub("KUL", "Kuala Lumpur", "Malaysia", "FREE", "Visa-free."),
    Hub("BKK", "Bangkok", "Thailand", "FREE", "Visa-free/VOA."),
)

HUBS_BY_CODE: dict[str, Hub] = {h.code: h for h in HUBS}

# --- Hard deny list -------------------------------------------------------
# A Costa Rican passport needs a consular appointment for a US C-1 transit
# visa and for a Canadian transit visa. Even a 60-minute airside connection
# at these airports is not allowed, so any itinerary touching one is dropped.

US_AIRPORTS = frozenset("""
ATL AUS BDL BNA BOI BOS BUF BUR BWI BZN CHS CLE CLT CMH CVG DAL DCA DEN DFW
DSM DTW ELP EWR FLL GRR GSP HNL HOU IAD IAH IND JAX JFK LAS LAX LGA LGB LIT
MCI MCO MDW MEM MIA MKE MSP MSY OAK OGG OKC OMA ONT ORD ORF PBI PDX PHL PHX
PIT PVD RDU RIC RNO RSW SAN SAT SAV SDF SEA SFO SJC SJU SLC SMF SNA STL TPA
TUS BQN STT STX GUM SPN PPG
""".split())

CANADA_AIRPORTS = frozenset("""
YYZ YUL YVR YYC YOW YEG YHZ YWG YQB YXE YQR YYJ YLW YXX YHM YKF YTZ
""".split())

# Mainland China: Costa Rica is not on the 240h visa-free-transit list, and
# the 24h direct-transit rule is airport-dependent. Excluded by default;
# flip ALLOW_MAINLAND_CHINA in config if you confirm your own eligibility.
CHINA_AIRPORTS = frozenset("""
PEK PKX PVG SHA CAN SZX CTU TFU XIY HGH CKG KMG WUH CSX NKG TAO XMN URC
""".split())

# Routing through Russia is visa-free for Costa Ricans but is impractical
# (sanctions, airspace, insurance) — excluded by default.
RUSSIA_AIRPORTS = frozenset("SVO DME VKO LED KJA VVO".split())

BANNED_AIRPORTS: frozenset[str] = (
    US_AIRPORTS | CANADA_AIRPORTS | CHINA_AIRPORTS | RUSSIA_AIRPORTS
)

BAN_REASONS: dict[frozenset[str], str] = {
    US_AIRPORTS: "US C-1 transit visa required",
    CANADA_AIRPORTS: "Canadian transit visa required",
    CHINA_AIRPORTS: "Chinese transit visa; CR not on visa-free transit list",
    RUSSIA_AIRPORTS: "Russia routing excluded by policy",
}

# Costa Rican origins and Japanese destinations.
ORIGINS = ("SJO",)
JAPAN_AIRPORTS = ("NRT", "HND", "KIX")

# Google accepts metro codes as well as airport codes, and one metro search
# returns options for every airport in it. Searching TYO instead of NRT and
# HND separately therefore costs one request instead of two and still
# surfaces the cheapest fare - verified live against three date pairs, where
# the metro search matched the cheapest legal fare of the two airport
# searches every time and dropped only pricier duplicates.
METRO_AIRPORTS: dict[str, frozenset[str]] = {
    "TYO": frozenset({"NRT", "HND"}),
    "OSA": frozenset({"KIX", "ITM"}),
}


# What the tracker actually searches. Metro codes, not airport codes: one
# TYO request covers Narita and Haneda together for the price of one.
JAPAN_DESTINATIONS = ("TYO", "OSA")


def destination_codes(code: str) -> frozenset[str]:
    """Every airport that counts as arriving at `code`.

    An airport code resolves to itself; a metro code resolves to the airports
    it covers. This is what lets an itinerary landing at HND satisfy a search
    for TYO.
    """
    code = code.upper()
    return METRO_AIRPORTS.get(code, frozenset({code}))


def is_metro(code: str) -> bool:
    return code.upper() in METRO_AIRPORTS


def ban_reason(code: str) -> str | None:
    """Why this airport is disallowed, or None if it is fine."""
    code = code.upper()
    for group, reason in BAN_REASONS.items():
        if code in group:
            return reason
    return None


def is_banned(code: str) -> bool:
    return code.upper() in BANNED_AIRPORTS


def usable_hubs(max_tier: Tier = "LIGHT") -> tuple[Hub, ...]:
    """Hubs at or below the given paperwork tier.

    max_tier="FREE" restricts to zero-paperwork hubs; "LIGHT" also allows
    short online authorisations (UK ETA, K-ETA), which the trip owner has
    said are acceptable.
    """
    allowed: set[Tier] = {"FREE"} if max_tier == "FREE" else {"FREE", "LIGHT"}
    return tuple(h for h in HUBS if h.tier in allowed)


METRO_NAMES = {"TYO": "Tokyo", "OSA": "Osaka"}
AIRPORT_NAMES = {
    "NRT": "Tokyo Narita", "HND": "Tokyo Haneda",
    "KIX": "Osaka Kansai", "ITM": "Osaka Itami",
}


def describe_hub(code: str) -> str:
    hub = HUBS_BY_CODE.get(code.upper())
    return f"{hub.city} ({hub.code})" if hub else code.upper()


def describe_destination(code: str) -> str:
    """'Tokyo' for a metro code, 'Tokyo Haneda (HND)' for an airport."""
    code = code.upper()
    if code in METRO_NAMES:
        return METRO_NAMES[code]
    name = AIRPORT_NAMES.get(code)
    return f"{name} ({code})" if name else code
