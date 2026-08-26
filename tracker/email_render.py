"""Render the alert email.

Layout deliberately mirrors the Google Flights price digest: greeting,
a titled section, one row per option (dates / price / View button /
airline - stops - route - duration), then the cheap-typical-expensive bar
and a footnote about the usual price range.

Email-client constraints that shape the markup:
* Tables, not flexbox or grid. Outlook renders neither.
* Inline styles on every element. Gmail strips <style> in several contexts.
* The price bar is three solid table cells rather than a CSS gradient,
  because gradient support is patchy; the marker is positioned with a
  percentage-width spacer cell, which works everywhere.
* A <style> block still ships, but only for progressive enhancement
  (dark mode, small screens). Nothing essential depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timezone
from html import escape as _escape


def escape(value: object, quote: bool = False) -> str:
    """Escape for element text by default; pass quote=True for attributes."""
    return _escape(str(value), quote=quote)
from typing import Sequence

from .airports import describe_destination
from .itinerary import Itinerary, format_duration, format_price
from .ranking import Selection, priority_checker, select_top
from .pricing import (
    BAND_COLOR,
    BAND_LABEL,
    SOURCE_NOTE,
    PriceBands,
    savings_vs_usual,
)

DEFAULT_ROWS = 20      # the email ranks this many options

# The trip owner is greeted by name every time, in both the HTML and the
# plain-text part. The kanji is non-ASCII, so anything that renders or
# transports this string has to be UTF-8 clean end to end.
GREETING = "Hello Nakama (仲間),"

INK = "#202124"
MUTED = "#5f6368"
LINE = "#dadce0"
GREEN = "#1e8e3e"
AMBER = "#e37400"
RED = "#d93025"
BLUE = "#1a73e8"

FONT = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)


def rank_for_email(
    itineraries: Sequence[Itinerary],
    *,
    threshold: int,
    count: int = DEFAULT_ROWS,
    priority_months: Sequence[int] = (),
    priority_share: float = 0.5,
) -> tuple[Selection, int]:
    """The ranked list for the email, and how many clear the threshold.

    Always `count` rows if that many options exist, cheapest first, with
    priority months guaranteed their share of the slots. Rows above the
    threshold are still shown, marked, so the email gives a picture of the
    whole market rather than a lonely single row.
    """
    selection = select_top(
        itineraries,
        count=count,
        is_priority=priority_checker(priority_months) if priority_months else None,
        priority_share=priority_share,
    )
    n_under = sum(1 for i in itineraries if i.price_usd <= threshold)
    return selection, n_under


@dataclass
class EmailContent:
    subject: str
    html: str
    text: str


def _fmt_date(d: Date) -> str:
    if not hasattr(d, "strftime"):
        return str(d)
    # %-d is glibc-only; Windows strftime rejects it. Build the day separately.
    return f"{d.strftime('%a, %b')} {d.day}"


def _date_range(itin: Itinerary) -> str:
    out = _fmt_date(itin.outbound_date)
    if itin.return_date is None:
        return out
    return f"{out} \u2013 {_fmt_date(itin.return_date)}"


def _trip_nights(itin: Itinerary) -> int | None:
    if itin.return_date is None:
        return None
    return (itin.return_date - itin.outbound_date).days


def _headline(itineraries: Sequence[Itinerary], verified: Sequence):
    """The fare the email is about, from whichever source has one.

    The HTTP grid used to be assumed non-empty, and that assumption gave
    the *weakest* source a veto over the whole product: the grid is
    floored at 8 requests, ~74% of what it returns is visa-rejected, and
    it cannot see stays over 30 nights at all. A run where it happened to
    find nothing threw away a Chrome-verified $1,347 and 400 sweep
    findings and sent no email.

    Returns (price, destination, is_from_grid) or None when there is
    genuinely nothing to say.
    """
    grid = min(itineraries, key=lambda i: i.price_usd) if itineraries else None
    ver = min(verified, key=lambda o: o.price_usd) if verified else None
    if grid is None and ver is None:
        return None
    if grid is None:
        return ver.price_usd, ver.destination, False
    if ver is None or grid.price_usd <= ver.price_usd:
        return grid.price_usd, grid.destination, True
    return ver.price_usd, ver.destination, False


def build_subject(
    itineraries: Sequence[Itinerary], bands: PriceBands, *, is_great: bool,
    verified: Sequence = (),
) -> str:
    """The subject is the only part read on a locked phone screen.

    It must quote the cheapest fare that actually exists, which is Chrome's
    when it beat the grid - otherwise the phone says $1,635 for a day the
    email itself is about a $1,347 seat.
    """
    head = _headline(itineraries, verified)
    if head is None:
        return "✈ SJO–Japan — nothing to report"
    if itineraries:
        best = (min(itineraries, key=lambda i: i.price_usd) if itineraries
            else min(verified, key=lambda o: o.price_usd))
    else:
        best = min(verified, key=lambda o: o.price_usd)
    if verified:
        cheapest_verified = min(verified, key=lambda o: o.price_usd)
        if cheapest_verified.price_usd < best.price_usd:
            price = format_price(cheapest_verified.price_usd)
            band = bands.classify(cheapest_verified.price_usd)
            dest = cheapest_verified.destination
            tail = ("book now" if is_great
                    else f"{len(itineraries) + len(verified)} options")
            return (f"✈ {price} SJO–{dest} — "
                    f"{BAND_LABEL[band]}, {tail}")
    band = bands.classify(best.price_usd)
    price = format_price(best.price_usd)
    dest = best.destination
    if is_great:
        return f"\u2708 {price} SJO\u2013{dest} \u2014 {BAND_LABEL[band]}, book now"
    # Count every option the email actually shows. Counting the grid alone
    # printed "(0 options)" beside a $1,347 headline on a run where the
    # grid had found nothing and Chrome had carried the whole email.
    n = len(itineraries) + len(verified)
    return (f"\u2708 {price} SJO\u2013{dest} \u2014 {BAND_LABEL[band]} "
            f"({n} option{'s' if n != 1 else ''})")


# --- HTML ------------------------------------------------------------------


def _nb(text: str) -> str:
    """Escape, and keep a price range on one line.

    Only the dash is protected. Replacing every space would turn the
    fallback "under $2,213" into "under&nbsp;$2,213", which reads fine in
    a browser and is noise to anything that greps the message.
    """
    return escape(text).replace(" – ", "&nbsp;&ndash;&nbsp;")


def band_ranges(bands: PriceBands) -> tuple[str, str, str]:
    """(cheap, typical, expensive) as ranges of real numbers.

    The cut-offs alone give "under $2,213" and "over $3,202" - open at both
    ends, so the bar never says what cheap actually reaches. The trip owner
    asked for that directly, having read "$1,641 is cheap" above a green
    zone starting below any fare that exists.

    So each zone is closed with an observed value where there is one: the
    cheapest fare ever recorded at the bottom, the dearest at the top. Both
    come from every Chrome observation the project holds - 2,804 of them on
    2026-08-25, and 2,210 of those from the background sweep, which is the
    only thing that prices the whole calendar.

    Falls back to the open form when nothing has been observed yet, because
    inventing an end would be worse than not drawing one.
    """
    lo, hi = format_price(bands.low), format_price(bands.high)
    typical = f"{lo} – {hi}"
    if bands.seen_low is not None and bands.seen_low < bands.low:
        cheap = f"{format_price(bands.seen_low)} – {lo}"
    else:
        cheap = f"under {lo}"
    if bands.seen_high is not None and bands.seen_high > bands.high:
        dear = f"{hi} – {format_price(bands.seen_high)}"
    else:
        dear = f"over {hi}"
    return cheap, typical, dear


def _price_bar(price: int, bands: PriceBands) -> str:
    """Google-style cheap/typical/expensive bar with a marker."""
    band = bands.classify(price)
    _cheap_txt, _typical_txt, _dear_txt = band_ranges(bands)
    color = BAND_COLOR[band]
    pos = max(0.0, min(1.0, bands.position(price)))
    left_pct = round(pos * 100, 2)
    right_pct = round(100 - left_pct, 2)

    label = f"{format_price(price)} is {BAND_LABEL[band]}"

    # Marker row: spacer cell of left_pct, then the dot.
    marker = f"""
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             border="0" style="width:100%;border-collapse:collapse;">
        <tr>
          <td style="width:{left_pct}%;"></td>
          <td style="width:0;white-space:nowrap;font-size:0;line-height:0;">
            <div style="width:13px;height:13px;border-radius:50%;
                        background:{color};border:2px solid #ffffff;
                        box-shadow:0 0 0 1px {color};margin-left:-7px;"></div>
          </td>
          <td style="width:{right_pct}%;"></td>
        </tr>
      </table>"""

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           border="0" style="width:100%;border-collapse:collapse;margin:4px 0 0;">
      <tr>
        <td align="center" style="padding:0 0 10px;">
          <span style="display:inline-block;background:{color};color:#ffffff;
                       font:600 13px/1.4 {FONT};padding:7px 14px;
                       border-radius:6px;">{escape(label)}</span>
        </td>
      </tr>
      <tr><td style="padding:0 0 7px;">{marker}</td></tr>
      <tr>
        <td style="padding:0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 border="0" style="width:100%;border-collapse:collapse;">
            <tr style="height:5px;">
              <td width="25%" style="background:{GREEN};height:5px;
                  border-radius:3px 0 0 3px;font-size:0;line-height:0;">&nbsp;</td>
              <td width="50%" style="background:{AMBER};height:5px;
                  font-size:0;line-height:0;">&nbsp;</td>
              <td width="25%" style="background:{RED};height:5px;
                  border-radius:0 3px 3px 0;font-size:0;line-height:0;">&nbsp;</td>
            </tr>
          </table>
        </td>
      </tr>
      <tr>
        <td style="padding:7px 0 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 border="0" style="width:100%;border-collapse:collapse;">
            <tr>
              <td width="25%" align="left"
                  style="font:400 11px/1.4 {FONT};color:{GREEN};">
                cheap<br><span style="color:{MUTED};">{_nb(_cheap_txt)}</span></td>
              <td width="50%" align="center"
                  style="font:400 11px/1.4 {FONT};color:{AMBER};">
                typical<br><span style="color:{MUTED};">
                {_nb(_typical_txt)}</span></td>
              <td width="25%" align="right"
                  style="font:400 11px/1.4 {FONT};color:{RED};">
                expensive<br><span style="color:{MUTED};">{_nb(_dear_txt)}</span></td>
            </tr>
          </table>
        </td>
      </tr>
    </table>"""


def _row(
    itin: Itinerary, bands: PriceBands, *, first: bool,
    rank: int = 0, over_threshold: bool = False, priority: bool = False,
) -> str:
    band = bands.classify(itin.price_usd)
    price_color = MUTED if over_threshold else (GREEN if band == "CHEAP" else INK)
    border = "" if first else f"border-top:1px solid {LINE};"
    nights = _trip_nights(itin)
    nights_txt = f" \u00b7 {nights} nights" if nights else ""

    meta = " \u00b7 ".join(
        x
        for x in (
            escape(itin.airlines_label),
            escape(itin.stops_label),
            escape(itin.route_label),
            escape(format_duration(itin.outbound_duration_min)),
        )
        if x
    )
    via = escape(itin.via_label) if itin.hubs else ""

    priority_tag = (
        f'<span style="font:600 11px/1 {FONT};color:{BLUE};background:#e8f0fe;'
        f'border-radius:4px;padding:3px 7px;margin-left:7px;'
        f'vertical-align:middle;">priority month</span>'
        if priority else ""
    )

    over_tag = (
        f'<span style="font:500 11px/1 {FONT};color:{MUTED};background:#f1f3f4;'
        f'border-radius:4px;padding:3px 7px;margin-left:7px;'
        f'vertical-align:middle;">over budget</span>'
        if over_threshold else ""
    )

    link = escape(itin.deep_link, quote=True)
    button = (
        f'<a href="{link}" style="display:inline-block;font:500 14px/1 {FONT};'
        f'color:{BLUE};text-decoration:none;border:1px solid {LINE};'
        f'border-radius:6px;padding:10px 20px;white-space:nowrap;">View</a>'
        if link
        else ""
    )

    return f"""
    <tr>
      <td style="padding:18px 0;{border}">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               border="0" style="width:100%;border-collapse:collapse;">
          <tr>
            <td style="vertical-align:top;">
              <div style="font:600 16px/1.4 {FONT};color:{INK};">
                <span style="color:{MUTED};font-weight:400;">{rank}.</span>
                {escape(_date_range(itin))}{priority_tag}{over_tag}</div>
              <div style="font:700 16px/1.5 {FONT};color:{price_color};
                          padding-top:3px;">
                {escape(format_price(itin.price_usd))}
                <span style="font:400 13px/1.5 {FONT};color:{MUTED};">
                  round trip{escape(nights_txt)}</span>
              </div>
              <div style="font:400 13px/1.6 {FONT};color:{MUTED};padding-top:5px;">
                {meta}</div>
              {f'<div style="font:400 13px/1.6 {FONT};color:{MUTED};">{via}</div>'
               if via else ''}
            </td>
            <td align="right" style="vertical-align:top;padding-left:14px;
                                     white-space:nowrap;">{button}</td>
          </tr>
        </table>
      </td>
    </tr>"""


def render_html(
    itineraries: Sequence[Itinerary],
    bands: PriceBands,
    *,
    threshold: int,
    is_great: bool,
    generated_at: str,
    dashboard_url: str | None = None,
    count: int = DEFAULT_ROWS,
    priority_months: Sequence[int] = (),
    priority_share: float = 0.5,
    priority_label: str = "",
    verified: Sequence = (),
) -> str:
    verified_html = verified_block_html(
        select_verified(verified, priority_months=priority_months,
                        share=priority_share), threshold)
    verified_note = (
        " — the block above is the checked, accurate one."
        if verified else ".")
    selection, n_under = rank_for_email(
        itineraries, threshold=threshold, count=count,
        priority_months=priority_months, priority_share=priority_share,
    )
    shown = selection.items
    in_priority = priority_checker(priority_months) if priority_months else None
    rows = [
        _row(i, bands, first=(n == 0), rank=n + 1,
             over_threshold=i.price_usd > threshold,
             priority=bool(in_priority and in_priority(i)))
        for n, i in enumerate(shown)
    ]
    best = (min(itineraries, key=lambda i: i.price_usd) if itineraries
            else min(verified, key=lambda o: o.price_usd))
    # The fare the email is actually about. The grid's `best` is the dear
    # one, so measuring against it produced "$165 below the $1,800
    # travellers usually pay" in an email whose own headline fare was
    # $1,347 - understating the saving by $288 and contradicting itself.
    cheapest_seen = min([best.price_usd] + [o.price_usd for o in verified])
    band = bands.classify(cheapest_seen)
    saving = savings_vs_usual(cheapest_seen, bands)

    # Name the city, not the code we happened to search. A metro-code search
    # reads as "TYO" everywhere unless it is translated here.
    dests = (sorted({i.destination for i in itineraries})
             or sorted({o.destination for o in verified}))
    dest_txt = "Japan" if len(dests) > 1 else describe_destination(dests[0])

    # The headline must count the browser-verified fares too. Built from
    # the HTTP grid alone it announced "Nothing under $1,400 today"
    # directly above a block listing five fares at $1,347 and $1,390 -
    # the email contradicting itself in its first sentence.
    verified_under = [o for o in verified if o.price_usd <= threshold]
    total_under = n_under + len(verified_under)

    if total_under:
        headline = (
            f"Found {total_under} visa-free option"
            f"{'s' if total_under != 1 else ''} from San Jos\u00e9 to {dest_txt} "
            f"at or under {format_price(threshold)}."
        )
    else:
        headline = (
            f"Nothing under {format_price(threshold)} today \u2014 the "
            f"cheapest visa-free option from San Jos\u00e9 to {dest_txt} "
            f"is {format_price(cheapest_seen)}."
        )
    if is_great:
        headline = (
            f"{format_price(cheapest_seen)} is a standout price \u2014 "
            f"{headline[0].lower()}{headline[1:]}"
        )

    saving_line = (
        f"<p style=\"margin:0 0 4px;font:400 14px/1.6 {FONT};color:{GREEN};\">"
        f"That is {format_price(saving)} below the {format_price(bands.usual)} "
        f"median visa-free fare seen for these dates.</p>"
        if saving and bands.usual
        else ""
    )

    notes: list[str] = []
    if n_under > len(shown):
        notes.append(
            f"+ {n_under - len(shown)} more under {format_price(threshold)} "
            f"not shown."
        )
    over_shown = sum(1 for i in shown if i.price_usd > threshold)
    if over_shown:
        notes.append(
            f"{over_shown} of these are above your {format_price(threshold)} "
            f"budget, listed so you can see where the market sits."
        )
    if priority_months and priority_label:
        notes.append(
            f"{selection.priority_count} of {len(shown)} depart in "
            f"{priority_label}."
        )
    more = (
        f'<p style="margin:14px 0 0;font:400 13px/1.6 {FONT};color:{MUTED};">'
        f'{escape(" ".join(notes))}</p>'
        if notes else ""
    )

    dash = ""
    if dashboard_url:
        dash = (
            f'<p style="margin:10px 0 0;font:400 13px/1.6 {FONT};color:{MUTED};">'
            f'<a href="{escape(dashboard_url, quote=True)}" '
            f'style="color:{BLUE};text-decoration:none;">Full price history</a></p>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>Flight price alert</title>
<style>
  @media (prefers-color-scheme: dark) {{
    .bg   {{ background:#1f1f1f !important; }}
    .card {{ background:#2d2e30 !important; }}
    .ink  {{ color:#e8eaed !important; }}
    .mut  {{ color:#9aa0a6 !important; }}
    .hr   {{ border-color:#3c4043 !important; }}
  }}
  @media only screen and (max-width:600px) {{
    .card {{ padding:22px 18px !important; }}
  }}
</style>
</head>
<body class="bg" style="margin:0;padding:0;background:#f1f3f4;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">
  {escape(headline)}
</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
       class="bg" style="width:100%;background:#f1f3f4;border-collapse:collapse;">
  <tr>
    <td align="center" style="padding:26px 12px;">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             border="0" style="width:600px;max-width:100%;border-collapse:collapse;">

        <tr>
          <td class="card" align="center"
              style="background:#ffffff;border-radius:10px 10px 0 0;
                     padding:22px 32px;border-bottom:1px solid {LINE};">
            <span style="font:400 20px/1.2 {FONT};color:{INK};letter-spacing:-.2px;">
              <b style="color:{BLUE};">SJO</b><span
                style="color:{RED};">\u2708</span><b
                style="color:{AMBER};">JPN</b>
              <span class="mut" style="color:{MUTED};">Flight Tracker</span>
            </span>
          </td>
        </tr>

        <tr>
          <td class="card" style="background:#ffffff;padding:28px 32px 8px;">
            <p class="ink" style="margin:0 0 14px;font:400 15px/1.6 {FONT};
                                  color:{INK};">{escape(GREETING)}</p>
            <p class="ink" style="margin:0 0 4px;font:400 15px/1.6 {FONT};
                                  color:{INK};">{escape(headline)}</p>
            {saving_line}
          </td>
        </tr>
        {verified_html}

        <tr>
          <td class="card" style="background:#ffffff;padding:14px 32px 0;">
            <h2 class="ink" style="margin:0;font:600 19px/1.35 {FONT};color:{INK};">
              Visa-free routes to {escape(dest_txt)}</h2>
            <p class="mut" style="margin:4px 0 0;font:400 13px/1.5 {FONT};
                                  color:{MUTED};">
              Top {len(shown)}, cheapest first \u00b7 Round trip \u00b7 1 adult
              \u00b7 Economy \u00b7 No US, Canada or China transit</p>
            <p class="mut" style="margin:4px 0 0;font:400 12px/1.5 {FONT};
                                  color:{MUTED};">
              Broad sweep, quick method. It cannot see some of Google's
              cheaper routings, so treat these as an upper bound{verified_note}</p>
          </td>
        </tr>

        <tr>
          <td class="card" style="background:#ffffff;padding:2px 32px 8px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                   border="0" style="width:100%;border-collapse:collapse;">
              {''.join(rows)}
            </table>
            {more}
          </td>
        </tr>

        <tr>
          <td class="card" style="background:#ffffff;padding:20px 32px 6px;
                                  border-top:1px solid {LINE};">
            <h3 class="ink" style="margin:0 0 12px;font:600 16px/1.4 {FONT};
                                   color:{INK};">
              Prices are currently
              <span style="color:{BAND_COLOR[band]};">{BAND_LABEL[band]}</span>
              for this route</h3>
            {_price_bar(best.price_usd, bands)}
            <p class="mut" style="margin:14px 0 0;font:400 13px/1.6 {FONT};
                                  color:{MUTED};">
              The least expensive flights for similar trips usually cost between
              {escape(format_price(bands.low))} and
              {escape(format_price(bands.high))}
              {f", with travellers typically booking at "
                f"{escape(format_price(bands.usual))}" if bands.usual else ""}.
              {escape(SOURCE_NOTE[bands.source])}</p>
          </td>
        </tr>

        <tr>
          <td class="card" style="background:#ffffff;border-radius:0 0 10px 10px;
                                  padding:18px 32px 26px;">
            <p class="mut" style="margin:0;font:400 12px/1.6 {FONT};color:{MUTED};">
              Checked {escape(generated_at)}. You get two of these a day; the
              second is held until the evening so it carries the day's cheapest
              fare. Fares move fast \u2014 a listed price is what Google
              showed at check time, not a held quote.</p>
            {dash}
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


# --- plain text ------------------------------------------------------------


def render_text(
    itineraries: Sequence[Itinerary],
    bands: PriceBands,
    *,
    threshold: int,
    is_great: bool,
    generated_at: str,
    count: int = DEFAULT_ROWS,
    priority_months: Sequence[int] = (),
    priority_share: float = 0.5,
    priority_label: str = "",
    verified: Sequence = (),
) -> str:
    best = (min(itineraries, key=lambda i: i.price_usd) if itineraries
            else min(verified, key=lambda o: o.price_usd))
    cheapest_seen = min([best.price_usd] + [o.price_usd for o in verified])
    band = bands.classify(cheapest_seen)
    n_under_all = (sum(1 for i in itineraries if i.price_usd <= threshold)
                   + sum(1 for o in verified if o.price_usd <= threshold))
    lines = [
        "SJO -> JAPAN FLIGHT TRACKER",
        "=" * 46,
        "",
        GREETING,
        "",
        *verified_block_text(
            select_verified(verified, priority_months=priority_months,
                            share=priority_share), threshold),
        # Both numbers must span the browser block as well. Counting the
        # grid alone printed "0 visa-free option(s) at or under $1,400.
        # Cheapest: $2,509" immediately beneath a verified $1,347 - and
        # the band beside it was already computed from `cheapest_seen`,
        # so the label read "cheap" while pointing at the dear fare.
        f"{n_under_all} "
        f"visa-free option(s) at or under {format_price(threshold)}.",
        f"Cheapest: {format_price(cheapest_seen)} "
        f"({BAND_LABEL[band]} for this route).",
        "",
    ]
    saving = savings_vs_usual(cheapest_seen, bands)
    if saving and bands.usual:
        lines.append(
            f"That is {format_price(saving)} below the "
            f"{format_price(bands.usual)} median visa-free fare "
            f"seen for these dates."
        )
        lines.append("")

    selection, n_under = rank_for_email(
        itineraries, threshold=threshold, count=count,
        priority_months=priority_months, priority_share=priority_share,
    )
    shown = selection.items
    in_priority = priority_checker(priority_months) if priority_months else None
    for n, itin in enumerate(shown, 1):
        tags = ""
        if in_priority and in_priority(itin):
            tags += "  [priority month]"
        if itin.price_usd > threshold:
            tags += "  [over budget]"
        lines.append(f"{n}. {_date_range(itin)}{tags}")
        lines.append(
            f"   {format_price(itin.price_usd)} round trip \u00b7 "
            f"{format_duration(itin.outbound_duration_min)} \u00b7 "
            f"{itin.stops_label}"
        )
        lines.append(f"   {itin.airlines_label} \u00b7 {itin.route_label}")
        if itin.hubs:
            lines.append(f"   {itin.via_label}")
        if itin.deep_link:
            lines.append(f"   {itin.deep_link}")
        lines.append("")

    if n_under > len(shown):
        lines.append(f"+ {n_under - len(shown)} more under the threshold.")
        lines.append("")
    if priority_months and priority_label:
        lines.append(
            f"{selection.priority_count} of {len(shown)} depart in "
            f"{priority_label}.")
        lines.append("")

    usual = (
        f", typically booked at {format_price(bands.usual)}" if bands.usual else ""
    )
    _cheap_r, _typical_r, _dear_r = band_ranges(bands)
    lines += [
        "-" * 46,
        # Spell out where each band starts and stops. The bar used to print
        # the two boundary numbers bare, so it showed $1,052 and $3,765
        # without ever saying those *were* the cut-offs.
        # Closed at both ends where there is an observation to close them
        # with, so "cheap" says what cheap reaches rather than trailing off.
        f"CHEAP     {_cheap_r.replace(chr(0x2013), 'to')}",
        f"TYPICAL   {_typical_r.replace(chr(0x2013), 'to')}{usual}",
        f"EXPENSIVE {_dear_r.replace(chr(0x2013), 'to')}",
        SOURCE_NOTE[bands.source],
        "",
        f"Checked {generated_at}. Two emails a day; the second is held "
        f"until the evening so it carries the day's cheapest fare.",
    ]
    return "\n".join(lines)


def checked_ago(option, now=None) -> str:
    """"checked 6 hr ago" for a swept fare, "" for one verified this run.

    The verified block sorts this-run Chrome results together with sweep
    findings up to `sweep_max_age_hours` (10) old, then puts a book link on
    every row. Without this the reader cannot tell a price checked minutes
    ago from one that was true before breakfast - which is exactly the "lie
    by omission" the age cap exists to prevent, only at a ten-hour
    granularity rather than a day.
    """
    stamp = getattr(option, "checked_at", "") or ""
    if not stamp:
        return ""
    try:
        seen = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return ""
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    mins = int((now - seen).total_seconds() / 60)
    if mins < 15:
        return "just checked"
    if mins < 90:
        return f"checked {mins} min ago"
    return f"checked {round(mins / 60)} hr ago"


# How many browser-verified fares the email shows. Six was arbitrary and
# became wrong on 2026-08-25, when `hot_list_size` rose to 18: the run
# verifies about eleven windows live and showed barely half of them.
VERIFIED_ROWS = 10


def select_verified(verified, *, count: int = VERIFIED_ROWS,
                    priority_months=(), share: float = 0.5) -> list:
    """Cheapest first, with the priority months guaranteed a share.

    The same contract `ranking.select_top` gives the grid table, which this
    block could not reuse: that works on `Itinerary` (`outbound_date`,
    `outbound_duration_min`) and these are `BrowserOption` (`depart_date`,
    `total_minutes`).

    It matters here more than there. This block is the part of the email
    that carries *live* prices, and it is sorted purely by price - so a
    month that happens to be cheap can fill every visible row and hide the
    months the trip owner actually asked about. On 2026-08-25 November
    already held two of six, on a month barely explored.

    The quota shapes membership, never order: the list returned is always
    strictly cheapest-first, and the single cheapest fare always appears
    whatever month it is in.
    """
    ranked = sorted(verified, key=lambda o: (o.price_usd, o.total_minutes))
    if not priority_months or not ranked:
        return ranked[:count]
    wanted = {int(m) for m in priority_months}
    pri = [o for o in ranked if o.depart_date.month in wanted]
    other = [o for o in ranked if o.depart_date.month not in wanted]

    # Reserve the priority slots, then fill what is left from the cheapest
    # remaining *of either kind*. Filling from `other` first would make the
    # quota a ceiling instead of a floor: with twelve priority fares at
    # $1,000 and twelve others at $2,000 it took five of each, burying five
    # cheaper priority fares under dearer ones. The quota shapes membership
    # only when it has to.
    reserved = min(len(pri), int(count * share))
    chosen = list(pri[:reserved])
    taken = {id(o) for o in chosen}
    for o in ranked:
        if len(chosen) >= count:
            break
        if id(o) not in taken:
            chosen.append(o)
            taken.add(id(o))
    # The cheapest fare found must never be hidden by the quota.
    if id(ranked[0]) not in {id(o) for o in chosen}:
        chosen = [ranked[0]] + chosen[:count - 1]
    return sorted(chosen, key=lambda o: (o.price_usd, o.total_minutes))[:count]


def verified_block_html(verified, threshold: int) -> str:
    """The Chrome-verified fares, above everything else in the email.

    These are the numbers the trip owner actually acts on: the HTTP grid
    cannot see the Zurich routings, so on the target window it reported
    $1,635 while the truth was $1,347. When the two disagree, this is the
    one that is right, so it goes first and says where it came from.
    """
    if not verified:
        return ""
    rows = []
    for o in verified[:VERIFIED_ROWS]:
        hrs, mins = divmod(o.total_minutes, 60)
        ago = checked_ago(o)
        age_txt = f" · {ago}" if ago else ""
        under = o.price_usd <= threshold
        colour = GREEN if under else INK
        # The link is price-capped, so it opens on this fare rather than a
        # list. Say "book" because that is what it is one click from.
        link = (f'<a href="{escape(o.deep_link)}" '
                f'style="color:#1a73e8;text-decoration:none;font-weight:600;">'
                f'See &amp; book &rarr;</a>'
                if o.deep_link else "")
        rows.append(
            f'<tr>'
            f'<td style="padding:8px 12px 8px 0;font:700 16px/1.4 {FONT};'
            f'color:{colour};white-space:nowrap;">{escape(format_price(o.price_usd))}</td>'
            f'<td style="padding:8px 12px 8px 0;font:400 13px/1.5 {FONT};color:{INK};">'
            f'{escape(o.route_label)}<br>'
            f'<span style="color:{MUTED};">{escape(str(o.depart_date))} to '
            f'{escape(str(o.return_date))} &middot; {o.nights} nights &middot; '
            f'{hrs} hr {mins} min &middot; {escape(", ".join(o.airlines))}'
            f'{escape(age_txt)}</span></td>'
            f'<td style="padding:8px 0;font:400 13px/1.5 {FONT};text-align:right;">'
            f'{link}</td></tr>')
    return (
        f'<tr><td class="card" style="background:#ffffff;padding:4px 32px 18px;">'
        f'<p style="margin:0 0 8px;font:700 13px/1.5 {FONT};color:{INK};">'
        f'Verified in a real browser</p>'
        f'<p style="margin:0 0 10px;font:400 12px/1.5 {FONT};color:{MUTED};">'
        f'Google hides some of its cheapest routings from the quick search. '
        f'These were re-checked the slow way and are visa-free.</p>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'width="100%">{"".join(rows)}</table></td></tr>')


def verified_block_text(verified, threshold: int) -> list[str]:
    if not verified:
        return []
    lines = ["VERIFIED IN A REAL BROWSER", "-" * 46]
    for o in verified[:VERIFIED_ROWS]:
        hrs, mins = divmod(o.total_minutes, 60)
        flag = "  <-- under your threshold" if o.price_usd <= threshold else ""
        lines.append(f"{format_price(o.price_usd)}  {o.route_label}{flag}")
        ago = checked_ago(o)
        lines.append(f"    {o.depart_date} to {o.return_date} ({o.nights}n), "
                     f"{hrs} hr {mins} min, {', '.join(o.airlines)}"
                     + (f", {ago}" if ago else ""))
        if o.deep_link:
            lines.append(f"    See & book: {o.deep_link}")
    lines.append("")
    return lines


def render(
    itineraries: Sequence[Itinerary],
    bands: PriceBands,
    *,
    threshold: int,
    is_great: bool,
    generated_at: str,
    dashboard_url: str | None = None,
    count: int = DEFAULT_ROWS,
    priority_months: Sequence[int] = (),
    priority_share: float = 0.5,
    priority_label: str = "",
    verified: Sequence = (),
) -> EmailContent:
    if not itineraries and not verified:
        raise ValueError("refusing to render an email with no itineraries")
    shared = dict(
        threshold=threshold, is_great=is_great, generated_at=generated_at,
        count=count, priority_months=priority_months,
        priority_share=priority_share, priority_label=priority_label,
        verified=verified,
    )
    return EmailContent(
        subject=build_subject(itineraries, bands, is_great=is_great,
                              verified=verified),
        html=render_html(itineraries, bands, dashboard_url=dashboard_url, **shared),
        text=render_text(itineraries, bands, **shared),
    )
