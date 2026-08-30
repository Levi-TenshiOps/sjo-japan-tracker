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
    # Added 2026-08-23 when `ban_reason` stopped treating silence as
    # approval. Same Schengen basis as the entries above; all of them carry
    # or feed Europe-Japan services, and Helsinki in particular is one of
    # the main Europe-Japan gateways, so leaving it out would have quietly
    # cost real fares.
    Hub("HEL", "Helsinki", "Finland", "FREE", "Schengen. Finnair HEL-NRT/HND/KIX."),
    Hub("VIE", "Vienna", "Austria", "FREE", "Schengen. Austrian VIE-NRT/HND."),
    Hub("CPH", "Copenhagen", "Denmark", "FREE", "Schengen. SAS feed."),
    Hub("ARN", "Stockholm", "Sweden", "FREE", "Schengen. SAS feed."),
    Hub("OSL", "Oslo", "Norway", "FREE", "Schengen. SAS feed."),
    Hub("BRU", "Brussels", "Belgium", "FREE", "Schengen. Feeds LH/AF hubs."),
    Hub("MXP", "Milan", "Italy", "FREE", "Schengen."),
    Hub("WAW", "Warsaw", "Poland", "FREE", "Schengen. LOT WAW-NRT."),
    Hub("PRG", "Prague", "Czechia", "FREE", "Schengen."),
    Hub("ATH", "Athens", "Greece", "FREE", "Schengen."),
    Hub("DUS", "Dusseldorf", "Germany", "FREE", "Schengen."),
    Hub("GVA", "Geneva", "Switzerland", "FREE", "Schengen."),
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
    # --- Observed in live results but never listed here -------------------
    # Found 2026-08-23 by tallying every connecting airport the tracker has
    # actually seen: these four appeared 523 times between them and were
    # accepted only because nothing had banned them, not because anything
    # had cleared them.
    Hub("MTY", "Monterrey", "Mexico", "FREE", "Mexico, as MEX. Aeromexico feed to MEX."),
    Hub("PVR", "Puerto Vallarta", "Mexico", "FREE", "Mexico, as MEX."),
    Hub("LIR", "Liberia", "Costa Rica", "FREE", "Domestic. The trip owner's own country."),
    Hub("SAL", "San Salvador", "El Salvador", "FREE", "Visa-free. Avianca hub feeding MEX/BOG."),
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
ANC ABQ TUL BHM GEG ROC SYR TYS ALB HSV ICT ISP LEX MHT MYR PNS PSP SBA SRQ
TLH TVC XNA FAI JNU KTN SIT BET OTZ OME ADQ
""".split())

CANADA_AIRPORTS = frozenset("""
YYZ YUL YVR YYC YOW YEG YHZ YWG YQB YXE YQR YYJ YLW YXX YHM YKF YTZ
YQT YZF YXY YFB YQM YSJ YYT YDF YQX YZV YBG YQY YAM YTS YXU YQG YXC YXS YPR
YZP
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


# Every code this project has a researched opinion about: the hubs above,
# the origin, Japan, and the metro codes Google may hand back.
KNOWN_AIRPORTS: frozenset[str] = frozenset(
    {h.code for h in HUBS}
    | set(ORIGINS) | set(JAPAN_AIRPORTS) | set(JAPAN_DESTINATIONS)
    | {a for members in METRO_AIRPORTS.values() for a in members}
    | BANNED_AIRPORTS
)


UNRESEARCHED_REASON = ("not a researched connection, so its transit-visa "
                       "rules are unverified - add it to HUBS if it is "
                       "genuinely visa-free")


def ban_reason(code: str) -> str | None:
    """Why this airport is disallowed, or None if it is fine.

    **Unknown means no, not yes.** This used to be a pure deny list, so any
    airport nobody had thought to add came back clean. Audited 2026-08-23
    against a list of real US and Canadian airports: 50 of them - including
    Anchorage and Fairbanks - were not on it and would have been treated as
    visa-free transits. A Costa Rican passport needs a C-1 for every one of
    them, and the traveller would find that out at the SJO gate.

    A hand-kept deny list can never be complete, so an unrecognised code is
    now a rejection with a reason that says why, rather than silence that
    reads as approval. The cost is a fare lost to an unlisted-but-legal hub;
    the alternative is recommending a flight that cannot legally be taken.
    That trade only goes one way.

    Adding a hub is deliberate: give it a researched tier and a note in
    HUBS, exactly as the eight-year-old comment at the top of this file
    already asked for.
    """
    # Fail closed on anything that is not a usable code, including None.
    # Both callers guard their inputs today, but they guard them by raising:
    # `browser.banned_reason` runs a regex that throws on None, and
    # `itinerary.validate` passes whatever the parser produced. An exception
    # in the visa check is not a rejection - it is an unhandled error whose
    # outcome depends on who catches it, and this is the one function in the
    # project that must never depend on that.
    if not isinstance(code, str) or not code.strip():
        return UNRESEARCHED_REASON
    code = code.strip().upper()
    for group, reason in BAN_REASONS.items():
        if code in group:
            return reason
    if code not in KNOWN_AIRPORTS:
        return UNRESEARCHED_REASON
    return None


def is_unresearched(code: str) -> bool:
    """True when a code is refused only for want of research.

    The distinction is the whole point of recording it. A US or Canadian
    hub is refused for ever and there is nothing to learn. A hub that is
    merely *unlisted* may be perfectly legal - Costa Rica has visa-free
    Schengen access, yet CDG is on the list and Orly is not, and Frankfurt
    and Munich are on it while Berlin and Hamburg are not. Immigration is
    national; the allow list is per-airport, and that gap costs fares.

    Nothing here changes what is allowed. It only separates "refused for
    ever" from "refused until somebody looks it up", so the cost of the
    second can be measured instead of guessed at.
    """
    code = code.upper()
    for group in BAN_REASONS:
        if code in group:
            return False
    return code not in KNOWN_AIRPORTS


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
