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


def build_subject(
    itineraries: Sequence[Itinerary], bands: PriceBands, *, is_great: bool
) -> str:
    best = min(itineraries, key=lambda i: i.price_usd)
    band = bands.classify(best.price_usd)
    price = format_price(best.price_usd)
    dest = best.destination
    if is_great:
        return f"\u2708 {price} SJO\u2013{dest} \u2014 {BAND_LABEL[band]}, book now"
    return f"\u2708 {price} SJO\u2013{dest} \u2014 {BAND_LABEL[band]} ({len(itineraries)} options)"


# --- HTML ------------------------------------------------------------------


def _price_bar(price: int, bands: PriceBands) -> str:
    """Google-style cheap/typical/expensive bar with a marker."""
    band = bands.classify(price)
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
              <td align="left" style="font:400 12px/1.4 {FONT};color:{MUTED};">
                {escape(format_price(bands.low))}</td>
              <td align="right" style="font:400 12px/1.4 {FONT};color:{MUTED};">
                {escape(format_price(bands.high))}</td>
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
) -> str:
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
    best = min(itineraries, key=lambda i: i.price_usd)
    band = bands.classify(best.price_usd)
    saving = savings_vs_usual(best.price_usd, bands)

    # Name the city, not the code we happened to search. A metro-code search
    # reads as "TYO" everywhere unless it is translated here.
    dests = sorted({i.destination for i in itineraries})
    dest_txt = "Japan" if len(dests) > 1 else describe_destination(dests[0])

    headline = (
        f"Found {n_under} visa-free option"
        f"{'s' if n_under != 1 else ''} from San Jos\u00e9 to {dest_txt} "
        f"at or under {format_price(threshold)}."
    )
    if is_great:
        headline = (
            f"{format_price(best.price_usd)} is a standout price \u2014 "
            f"{headline[0].lower()}{headline[1:]}"
        )

    saving_line = (
        f"<p style=\"margin:0 0 4px;font:400 14px/1.6 {FONT};color:{GREEN};\">"
        f"That is {format_price(saving)} below the {format_price(bands.usual)} "
        f"travellers usually pay.</p>"
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
                                  color:{INK};">Hello,</p>
            <p class="ink" style="margin:0 0 4px;font:400 15px/1.6 {FONT};
                                  color:{INK};">{escape(headline)}</p>
            {saving_line}
          </td>
        </tr>

        <tr>
          <td class="card" style="background:#ffffff;padding:14px 32px 0;">
            <h2 class="ink" style="margin:0;font:600 19px/1.35 {FONT};color:{INK};">
              Visa-free routes to {escape(dest_txt)}</h2>
            <p class="mut" style="margin:4px 0 0;font:400 13px/1.5 {FONT};
                                  color:{MUTED};">
              Top {len(shown)}, cheapest first \u00b7 Round trip \u00b7 1 adult
              \u00b7 Economy \u00b7 No US, Canada or China transit</p>
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
              Checked {escape(generated_at)}. Alerts are capped at two per day, so
              a quiet inbox means nothing beat
              {escape(format_price(threshold))}. Fares move fast \u2014 a listed
              price is what Google showed at check time, not a held quote.</p>
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
) -> str:
    best = min(itineraries, key=lambda i: i.price_usd)
    band = bands.classify(best.price_usd)
    lines = [
        "SJO -> JAPAN FLIGHT TRACKER",
        "=" * 46,
        "",
        f"{sum(1 for i in itineraries if i.price_usd <= threshold)} "
        f"visa-free option(s) at or under {format_price(threshold)}.",
        f"Cheapest: {format_price(best.price_usd)} "
        f"({BAND_LABEL[band]} for this route).",
        "",
    ]
    saving = savings_vs_usual(best.price_usd, bands)
    if saving and bands.usual:
        lines.append(
            f"That is {format_price(saving)} below the "
            f"{format_price(bands.usual)} travellers usually pay."
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
    lines += [
        "-" * 46,
        f"Usual range: {format_price(bands.low)} to "
        f"{format_price(bands.high)}{usual}.",
        SOURCE_NOTE[bands.source],
        "",
        f"Checked {generated_at}. Max two alerts per day; silence means "
        f"nothing beat {format_price(threshold)}.",
    ]
    return "\n".join(lines)


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
) -> EmailContent:
    if not itineraries:
        raise ValueError("refusing to render an email with no itineraries")
    shared = dict(
        threshold=threshold, is_great=is_great, generated_at=generated_at,
        count=count, priority_months=priority_months,
        priority_share=priority_share, priority_label=priority_label,
    )
    return EmailContent(
        subject=build_subject(itineraries, bands, is_great=is_great),
        html=render_html(itineraries, bands, dashboard_url=dashboard_url, **shared),
        text=render_text(itineraries, bands, **shared),
    )
