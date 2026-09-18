#!/usr/bin/env python3
"""
writeReviewAgent.py - PROTOTYPE (option "C")

An alternative to the writeReviewAdam.py assembly line. One strong model
(Claude Opus 5) writes the whole review in a single pass, having been shown:

  1. Adam's voice rules                       (stable, cached)
  2. Gamblineers' editorial criteria table    (stable, cached)
  3. The ENTIRE competitive field, 118 casinos (stable, cached)
  4. This casino's full dossier               (volatile)
  5. The last N reviews written               (volatile - anti-repetition)

Why this shape, vs. the current pipeline:

  * The current pipeline drafts 5 sections as 5 blind parallel calls, so no
    stage ever sees the whole casino and nobody can form a thesis. Here one
    reasoner sees everything before writing a word.
  * The current pipeline has comparison casinos pre-chosen by spreadsheet
    formulas (top-5-that-beat-this-one) before any model reasons about
    relevance. Here the model sees all 118 and picks what's actually telling.
  * Editorial thresholds ("over 7000 games is a lot") are passed as a
    REFERENCE TABLE the model consults, not an IF/THEN script it executes.
    That's the difference between calibrated judgment and a checklist - but
    keeping them as data is what stops "5,000 games" being called "plenty"
    in one review and "thin" in the next.
  * Reads Data/Bonuses/Comments/StatusLog DIRECTLY, bypassing the TempOutput
    formula layer (which pre-narrows comparison pools and uses volatile
    RAND() to pick hedge words like "almost"/"over" for the same raw number).

Deliberately NOT included in this prototype, to keep the comparison clean:
  * Internal linking (link_casino_mentions) - plugs in afterward, unchanged.
  * Google Docs upload - the existing uploader is good, reuse it later.
  * The GPT-3.5 fine-tune voice pass - replacing it is the point.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python3 writeReviewAgent.py "BitStarz"
    python3 writeReviewAgent.py "BitStarz" --dry-run    # inspect prompt, no API call
    python3 writeReviewAgent.py "FortuneJack" --revise  # add a self-check pass
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

import config

SPREADSHEET_ID = config.get("SPREADSHEET_ID")

MODEL = "claude-fable-5-1"
# Switched from claude-opus-5 to Fable 5.1 for review writing (2026-09-18, Goran's
# call, made after seeing the real cost delta: Fable runs ~2x Opus per review here -
# $10/$50 vs $5/$25 per MTok in/out, plus a pricier 1h cache write ($20 vs $10/MTok)
# only partly offset by a cheaper cache-read rate ($0.25 vs $0.50/MTok). No quality
# comparison against Opus has been run yet on this specific voice-matching task.
# cost_of() below was updated to Fable's rates in the same change - it is NOT
# model-aware, it just hardcodes whichever model MODEL currently points to, so if
# MODEL changes again, cost_of() needs a matching edit or every displayed cost will
# be silently wrong (exactly the bug just found and fixed in the Opus cache-write
# rate right before this switch).
# Which site's status column / bonus rows apply. StatusLog col F is Gamblineers.
SITE = config.get("GAMBLINEERS_SITE")

# Verified against the spreadsheet's own formulas (not guessed from headers):
# e.g. TempOutput uses Data!AP="Yes" for VPN-friendly (AP -> index 41),
# Data!AC:AG for the 5 RG limit tools (28-32), Data!AU/AV for ID/Review URL.
COL = {
    "name": 0, "url": 1, "year": 2, "month": 3, "license": 4, "games": 5,
    "look": 6, "prod_casino": 7, "prod_sports": 8, "prod_esports": 9,
    "prod_lottery": 10, "prod_trading": 11, "languages": 12, "livechat": 13,
    "filters_broken": 14, "providers": 15, "provably_fair": 16, "inhouse": 17,
    "extra_filters": 18, "anonymous": 19, "cryptos": 20, "buy_crypto": 21,
    "convert_tokens": 26, "self_exclusion": 27,
    "rg_deposit": 28, "rg_wager": 29, "rg_loss": 30, "rg_reality": 31,
    "rg_time": 32, "cooling_off": 33, "rg_no_support": 34,
    "restricted": 35, "kyc_speed": 36, "withdrawal_time": 37,
    "wd_day": 38, "wd_week": 39, "wd_month": 40,
    "vpn": 41, "aml_wagering": 42, "wagering_visible": 43,
    "popups": 44, "broken_images": 45, "id": 46, "review_url": 47,
}
RG_TOOL_COLS = ["rg_deposit", "rg_wager", "rg_loss", "rg_reality", "rg_time"]
STATUSLOG_SITE_COL = {"BCK": 3, "Gamblineers": 5, "Gamble": 7}

TOP_PROVIDERS = [
    "BetSoft", "BGaming", "Evolution Gaming", "Microgaming", "NetEnt",
    "Novomatic", "Play'n Go", "Playtech", "Pragmatic Play", "QuickSpin",
    "Red Tiger", "Spinomenal", "Yggdrasil",
]

SECTIONS = ["General", "Payments", "Games", "Responsible Gambling", "Bonuses"]


# ----------------------------------------------------------------------------
# SHEET ACCESS
# ----------------------------------------------------------------------------

def _sheets_client():
    """Sheets client via config, so this works under Streamlit secrets or a local key."""
    return config.sheets_service()


def _fetch(sheets, rng: str) -> List[List[str]]:
    return (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=rng, valueRenderOption="FORMATTED_VALUE")
        .execute()
        .get("values", [])
    )


def cell(row: List[str], idx: int) -> str:
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def count_list(value: str) -> Optional[int]:
    """Count comma-separated entries (providers, cryptos, countries)."""
    if not value:
        return None
    return len([p for p in value.split(",") if p.strip()])


def to_int(value: str) -> Optional[int]:
    m = re.search(r"\d[\d,]*", value or "")
    return int(m.group(0).replace(",", "")) if m else None


# ----------------------------------------------------------------------------
# DATA LOADING
# ----------------------------------------------------------------------------

class CasinoDB:
    def __init__(self):
        sheets = _sheets_client()
        # Rows 1-3 are a 3-tier header (group label / field name / sub-field).
        self.header_rows = _fetch(sheets, "Data!A1:AV3")
        self.data_rows = [r for r in _fetch(sheets, "Data!A4:AV200") if cell(r, 0)]
        self.bonus_header = _fetch(sheets, "Bonuses!A1:AC2")
        self.bonus_rows = [r for r in _fetch(sheets, "Bonuses!A3:AC1000") if cell(r, 0)]
        self.comment_rows = [r for r in _fetch(sheets, "Comments!A2:R200") if cell(r, 0)]
        self.status_rows = [r for r in _fetch(sheets, "StatusLog!A2:I1000") if cell(r, 1)]

        self.status_by_name = {
            cell(r, 1): cell(r, STATUSLOG_SITE_COL.get(SITE, 5)) for r in self.status_rows
        }
        self.labels = self._build_labels()

    def _build_labels(self) -> Dict[int, str]:
        """Join the 3 header tiers into one label per column.

        Pairing the sheet's own labels programmatically (rather than hardcoding
        my reading of them) means the model always sees the spreadsheet's
        wording, and a column rename upstream doesn't silently mislabel a fact.
        """
        labels = {}
        tier2 = self.header_rows[1] if len(self.header_rows) > 1 else []
        tier3 = self.header_rows[2] if len(self.header_rows) > 2 else []
        current = ""
        for i in range(48):
            main = cell(tier2, i)
            sub = cell(tier3, i)
            if main:
                current = main
            if sub and current and sub != current:
                labels[i] = f"{current} - {sub}"
            elif sub:
                labels[i] = sub
            else:
                labels[i] = main or current or f"col{i}"
        return labels

    def find(self, name: str) -> List[str]:
        """Row for a casino. Raises LookupError (not sys.exit) so the web app can
        surface a message instead of the process dying under Streamlit."""
        for r in self.data_rows:
            if cell(r, 0).lower() == name.lower():
                return r
        close = [cell(r, 0) for r in self.data_rows if name.lower() in cell(r, 0).lower()]
        hint = f" Did you mean: {', '.join(close[:5])}?" if close else ""
        raise LookupError(f'Casino "{name}" not found in the Data tab.{hint}')

    def live_casino_names(self) -> List[str]:
        """Every casino Live on this site, for a picker rather than free-text entry."""
        return sorted(
            cell(r, COL["name"]) for r in self.data_rows
            if cell(r, COL["name"]) and self.is_live(cell(r, COL["name"]))
        )

    def is_live(self, name: str) -> bool:
        return self.status_by_name.get(name, "") == "Live"


# ----------------------------------------------------------------------------
# LANDSCAPE (all casinos, compact - this is what replaces the top-5 formulas)
# ----------------------------------------------------------------------------

def landscape_row(db: CasinoDB, row: List[str]) -> str:
    name = cell(row, COL["name"])
    games = to_int(cell(row, COL["games"]))
    providers = count_list(cell(row, COL["providers"]))
    cryptos = count_list(cell(row, COL["cryptos"]))
    restricted = count_list(cell(row, COL["restricted"]))
    rg_count = sum(1 for k in RG_TOOL_COLS if cell(row, COL[k]) == "Yes")

    provider_list = cell(row, COL["providers"])
    missing_top = [p for p in TOP_PROVIDERS if p.lower() not in provider_list.lower()]

    fields = [
        name,
        f"est:{cell(row, COL['year']) or '?'}",
        f"lic:{cell(row, COL['license']) or '?'}",
        f"games:{games if games is not None else '?'}",
        f"providers:{providers if providers is not None else '?'}",
        f"missing_top13:{len(missing_top)}",
        f"cryptos:{cryptos if cryptos is not None else '?'}",
        f"restricted:{restricted if restricted is not None else '?'}",
        f"vpn:{cell(row, COL['vpn']) or '?'}",
        f"anon:{cell(row, COL['anonymous']) or '?'}",
        f"chat:{cell(row, COL['livechat']) or '?'}",
        f"payout:{cell(row, COL['withdrawal_time']) or 'not stated'}",
        f"kyc:{cell(row, COL['kyc_speed']) or 'not stated'}",
        f"wd_limits(d/w/m):{cell(row, COL['wd_day']) or '-'}/{cell(row, COL['wd_week']) or '-'}/{cell(row, COL['wd_month']) or '-'}",
        f"rg_tools:{rg_count}",
        f"selfexcl:{cell(row, COL['self_exclusion']) or '?'}",
        f"cooloff:{cell(row, COL['cooling_off']) or '?'}",
        f"provably_fair:{cell(row, COL['provably_fair']) or '?'}",
        f"inhouse:{cell(row, COL['inhouse']) or '?'}",
    ]
    url = cell(row, COL["review_url"])
    if url:
        fields.append(f"review:{url}")
    return " | ".join(fields)


def build_landscape(db: CasinoDB, focus_name: str) -> str:
    lines = []
    skipped = 0
    for row in db.data_rows:
        name = cell(row, COL["name"])
        # Only Live casinos are valid to recommend. Keep the focus casino
        # regardless of status, since we're reviewing it either way.
        if not db.is_live(name) and name.lower() != focus_name.lower():
            skipped += 1
            continue
        lines.append(landscape_row(db, row))
    header = (
        f"THE FIELD - every casino currently Live on {SITE} ({len(lines)} casinos; "
        f"{skipped} closed/unlisted omitted).\n"
        "Use this to calibrate what is genuinely good or bad, and to choose "
        "comparisons. Only ever name a casino from this list.\n"
        "Counts are computed from the source lists. 'missing_top13' = how many "
        "of the 13 major studios that casino lacks.\n"
    )
    return header + "\n".join(sorted(lines))


# ----------------------------------------------------------------------------
# FOCUS CASINO DOSSIER
# ----------------------------------------------------------------------------

FIELD_RANK_EXTREME = 5  # top/bottom N of the field that actually earns "one of the most/least"


def field_rank(db: CasinoDB, focus: str, label: str, getter) -> Optional[str]:
    """Rank the focus casino against the Live field on one metric, with its neighbours.

    Added after a real error: v2 of the BitStarz review claimed it "accepts more tokens
    than any casino on my list except TrustDice" when BitStarz leads at 260 and TrustDice
    is second at 154. The model had every number in the landscape and still inverted the
    comparison, because answering "who has the most" means scanning 78 rows and holding
    them in mind. That is the same class of task as the provider set-difference and the
    casino-age arithmetic: cheap and exact in code, error-prone in a model. So compute
    the standing here and hand over the finished sentence.

    The literal ordinal is for fact-checking only, never for the reader: a first draft
    wrote "ranking 61st of the 78 casinos I track" and "ranking 27th of 78" straight into
    review prose, which exposes the mechanism and reads like a leaderboard, not a review.
    Only the two extreme bands earn qualitative "one of the most/least" framing - a
    middling rank gets no positional language at all, just the neighbour context needed
    to verify a comparison is correctly ordered.
    """
    scored = []
    for r in db.data_rows:
        name = cell(r, COL["name"])
        if not db.is_live(name) and name.lower() != focus.lower():
            continue
        value = getter(r)
        if value is not None:
            scored.append((value, name))
    if len(scored) < 3:
        return None

    scored.sort(key=lambda t: -t[0])
    names = [n for _, n in scored]
    if focus not in names:
        return None
    i = names.index(focus)
    own = scored[i][0]
    total = len(scored)

    if i == 0:
        tier = "HIGHEST in the field - safe to say so in the review, in your own words"
    elif i < FIELD_RANK_EXTREME:
        tier = (f"one of the {FIELD_RANK_EXTREME} highest in the field - "
               f"'one of the highest/most generous I track' is earned here")
    elif i == total - 1:
        tier = "LOWEST in the field - safe to say so in the review, in your own words"
    elif i >= total - FIELD_RANK_EXTREME:
        tier = (f"one of the {FIELD_RANK_EXTREME} lowest in the field - "
               f"'one of the lowest/most restrictive I track' is earned here")
    else:
        tier = ("unremarkable - roughly mid-field. Do NOT frame this as high, low, "
               "rare or notable in any way. Give your verdict from the criteria table "
               "only, with no positional language")

    if i == 0:
        neighbor = f"next highest is {scored[1][1]} at {scored[1][0]}"
    elif i == total - 1:
        neighbor = f"next lowest is {scored[-2][1]} at {scored[-2][0]}"
    else:
        neighbor = (f"above it: {scored[i-1][1]} at {scored[i-1][0]}; "
                   f"below it: {scored[i+1][1]} at {scored[i+1][0]}")

    return (f"- {label}: {own} - {tier}. (Internal fact-check only, never state this "
            f"number or the word 'rank' to the reader: {neighbor}; position {i + 1} "
            f"of {total}.)")


# These 4 fields are long enumerable lists (countries/languages/cryptos/providers).
# Deliberately withheld from the model in raw form in build_dossier() below - not just
# told not to enumerate them, because that instruction didn't reliably hold (a review
# named 13 of BitStarz's 75 blocked countries, another listed 9 cryptocurrencies in
# full). If the model never sees the list, it cannot recite it. Only the count (DERIVED
# below) and, for providers, the curated 13-studio present/missing split remain -
# already the right level of detail for a reader.
WITHHELD_LIST_COLS = {COL["restricted"], COL["languages"], COL["cryptos"], COL["providers"]}


def build_dossier(db: CasinoDB, row: List[str]) -> str:
    name = cell(row, COL["name"])
    out = [f"CASINO UNDER REVIEW: {name}", ""]
    out.append("Raw source fields (label: value, exactly as the database stores them):")
    for i in range(48):
        value = cell(row, i)
        if not value or i == COL["name"]:
            continue
        label = db.labels.get(i, f"col{i}")
        if i in WITHHELD_LIST_COLS:
            n = count_list(value)
            out.append(f"- {label}: withheld - {n} entries, see DERIVED count below. "
                      f"Never enumerate this list; state the count and your judgment of it.")
        elif i == COL["prod_trading"]:
            # Confirmed the model was guessing at this one - it invented "a trading desk
            # where you can buy and sell crypto in real time" for a feature it was never
            # actually told the definition of, and separately collided the word with its
            # unrelated everyday sense ("a brand that's been trading since 2014"). A first
            # fix gave the real definition in flowing prose right where the fact appears -
            # which promptly became its own leak: "a price-prediction game for betting on
            # which way a coin moves" turned up near-verbatim in 3 of 5 reviews, because a
            # complete, quotable sentence in the dossier gets treated as ready-made prose
            # to lift, the same failure as the buy-crypto/AML criteria entries earlier.
            # Stated clinically instead - the disambiguating facts a writer needs, not a
            # sentence shaped to be reused.
            out.append(
                f"- {label}: {value} (disambiguation, not phrasing to reuse - this "
                f"product: predicts price direction; unrelated to 'trading' meaning "
                f"'operating as a business'; do not describe it with exchange/stock-"
                f"market imagery like 'trading desk' or 'buy and sell crypto', which "
                f"overstate what it is - if you mention it, put it in your own words.)"
            )
        else:
            out.append(f"- {label}: {value}")

    provider_list = cell(row, COL["providers"])
    missing = [p for p in TOP_PROVIDERS if p.lower() not in provider_list.lower()]
    present = [p for p in TOP_PROVIDERS if p.lower() in provider_list.lower()]

    # Age is computed here, not left to the model: it has no reliable sense of
    # today's date and will silently guess the current year (observed: a 2014
    # casino described as "eleven years" old in 2026). Same principle as the
    # provider set-difference - code does the arithmetic, the model judges it.
    today = datetime.now()
    year = to_int(cell(row, COL["year"]))
    age_line = "- Casino age: unknown (no year on file)"
    if year:
        age = today.year - year
        age_line = (
            f"- Casino age: approximately {age} years "
            f"(established {year}; today is {today.strftime('%d %B %Y')}). "
            f"Use this number - do not compute the age yourself."
        )

    out += [
        "",
        "DERIVED (computed here so you never have to count by hand - use these numbers):",
        age_line,
        f"- Number of games: {to_int(cell(row, COL['games']))}",
        f"- Number of providers: {count_list(provider_list)}",
        f"- Number of cryptocurrencies: {count_list(cell(row, COL['cryptos']))}",
        f"- Number of restricted countries: {count_list(cell(row, COL['restricted']))}",
        f"- Number of languages: {count_list(cell(row, COL['languages']))}",
        f"- Major studios PRESENT ({len(present)}/13): {', '.join(present) or 'none'}",
        f"- Major studios MISSING ({len(missing)}/13): {', '.join(missing) or 'none'}",
        f"- RG limit tools besides self-exclusion: "
        f"{sum(1 for k in RG_TOOL_COLS if cell(row, COL[k]) == 'Yes')} "
        f"({', '.join(db.labels[COL[k]] for k in RG_TOOL_COLS if cell(row, COL[k]) == 'Yes') or 'none'})",
    ]

    focus = cell(row, COL["name"])
    ranks = [
        field_rank(db, focus, "Cryptocurrencies", lambda r: count_list(cell(r, COL["cryptos"]))),
        field_rank(db, focus, "Number of games", lambda r: to_int(cell(r, COL["games"]))),
        field_rank(db, focus, "Game providers", lambda r: count_list(cell(r, COL["providers"]))),
        field_rank(db, focus, "Restricted countries", lambda r: count_list(cell(r, COL["restricted"]))),
    ]
    ranks = [r for r in ranks if r]
    if ranks:
        out += [
            "",
            "WHERE THIS CASINO STANDS IN THE FIELD (computed - do not re-derive these "
            "standings by scanning the field list yourself, and never claim a casino beats "
            "this one on a metric where these lines say otherwise):",
        ] + ranks
    return "\n".join(out)


def build_bonuses(db: CasinoDB, name: str) -> str:
    labels = {}
    tier1 = db.bonus_header[0] if db.bonus_header else []
    tier2 = db.bonus_header[1] if len(db.bonus_header) > 1 else []
    current = ""
    for i in range(29):
        main, sub = cell(tier1, i), cell(tier2, i)
        if main:
            current = main
        labels[i] = f"{current} - {sub}" if (sub and current and sub != current) else (sub or main or current)

    rows = []
    for r in db.bonus_rows:
        if cell(r, 0).lower() != name.lower():
            continue
        if SITE.lower() not in cell(r, 1).lower():
            continue  # bonus not offered on this site
        rows.append(r)

    if not rows:
        return "BONUS DATA: no bonus rows on file for this casino on " + SITE + "."

    out = [f"BONUS DATA ({len(rows)} bonus rows on file for {SITE}):"]
    for n, r in enumerate(rows, 1):
        out.append(f"\nBonus {n}:")
        for i in range(29):
            value = cell(r, i)
            if value and i != 0:
                out.append(f"  - {labels.get(i) or f'col{i}'}: {value}")
    out.append(
        "\nNOTE: one of these columns is a pre-composed description that already "
        "contains someone else's editorial verdicts ('rating criteria: ...'). "
        "Treat those as raw input, not as your opinion - form your own judgment "
        "from the numbers and the criteria table, and write it in your own words."
    )
    return "\n".join(out)


def build_comments(db: CasinoDB, name: str) -> str:
    found = []
    for r in db.comment_rows:
        if cell(r, 0).lower() != name.lower():
            continue
        for i in range(2, 18):
            value = cell(r, i)
            if value:
                found.append(value)
    if not found:
        return "MANUAL RESEARCH NOTES: none on file."
    body = "\n".join(f"- {c}" for c in found)
    return (
        "YOUR OWN HANDS-ON NOTES from testing this casino. These are real first-hand "
        "observations, and they are usually the most interesting material in the whole "
        "dossier - the things no data column captures:\n"
        f"{body}\n\n"
        "WRITE THESE AS YOUR OWN EXPERIENCE, in the first person and the past tense, "
        "because that is what they are. \"Support came back to me in under a minute\", "
        "not \"support is reported to be responsive\". This is the one place in the "
        "review where you are a person who used the site rather than an analyst reading "
        "a table, so use it.\n"
        "Two limits on that. Keep each note's substance exactly as it is - do not inflate "
        "\"responsive support\" into \"the best support I have ever had\". And do not "
        "invent experience beyond these notes: if a note does not mention it, you did not "
        "test it, so do not claim you did.\n\n"
        "DATA-HYGIENE WARNING: these notes are not always re-audited after a casino "
        "rebrands, so a note may refer to this casino by a FORMER name, or describe "
        "another brand entirely. If a note's brand name does not match the casino "
        "under review, do NOT repeat that name as current fact. Use the substance if "
        "it clearly belongs to this casino, and add a line at the very end of your "
        "output starting with 'DATA FLAG:' describing the discrepancy."
    )


# ----------------------------------------------------------------------------
# ROLLING HISTORY WINDOW (anti-repetition)
# ----------------------------------------------------------------------------

def load_history(dirs: List[Path], focus_name: str, n: int,
                 exclude_same_casino: bool = True) -> List[Tuple[str, str]]:
    """Newest n review .md files across the given dirs.

    Two different callers want two different windows, which is why the exclusion is
    a flag rather than always-on:

    * Full-text context (exclude_same_casino=True): skip prior reviews of the casino
      being written, so the model neither copies nor over-avoids its own last take
      on the same subject.
    * Phrase/signature extraction (False): include them. A casino's own previous
      review is the single most important place not to repeat yourself, and excluding
      it is exactly what let "so nothing is being smuggled past you" survive two
      batches - the phrase lived only in BitStarz reviews, so it was invisible every
      time BitStarz was regenerated.
    """
    candidates = []
    slug = re.sub(r"[^a-z0-9]", "", focus_name.lower())
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            fslug = re.sub(r"[^a-z0-9]", "", f.stem.lower())
            if exclude_same_casino and slug and slug in fslug:
                continue
            candidates.append(f)
    candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [(f.stem, f.read_text(encoding="utf-8", errors="replace")) for f in candidates[:n]]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*#\[\]()]", "", text)).lower()


EVALUATIVE_TOKENS = {
    "i", "my", "me", "worth", "worse", "better", "best", "worst", "rare", "rarer",
    "norm", "average", "standard", "matters", "deserve", "credit", "prefer", "want",
    "encounter", "record", "consider", "genuinely", "honestly", "frankly", "mildest",
    "gentlest", "smuggled", "workable", "generous", "punishing", "leisurely",
    "impressed", "bothers", "quietly", "plainly", "hard", "good", "bad",
}


def recurring_phrases(history: List[Tuple[str, str]], min_reviews: int = 2) -> List[str]:
    """Find wordings that already recur across the window, mechanically.

    Observed on the first real batch: facts were perfect but the model grew a new
    formula - "X is dead average, so nothing is being smuggled past you" and
    "cooling-off is uncommon enough that I credit it" came back near-verbatim in
    separate reviews. Showing it the full prior reviews was not enough, because
    nothing pointed AT the repetition. So compute it here instead of hoping the
    model spots it (same principle as computing the provider set-difference).

    The real bug behind the batch-2 leaks was not the threshold, it was the window:
    signature extraction inherited the same-casino exclusion, so phrases living only in
    BitStarz reviews were invisible every time BitStarz was regenerated. That is fixed
    in load_history() via exclude_same_casino=False for signatures, and the threshold
    stays at 2.

    Deliberately NOT more aggressive than that, on evidence. Adam's own five published
    reviews share 38.3 verbatim 6+-word phrases per 10k words (he reuses whole support
    paragraphs, typos included); batch 3 of this pipeline sat at 47.6. Cross-review
    phrase overlap is inherent to reviewing the same product category against the same
    criteria, so chasing it to zero would mean banning ordinary phrasing, inviting
    paraphrase rather than genuinely different analysis, and bloating the prompt for a
    24% gap over a human baseline. The visible problem - every review opening with an
    identical construction - is handled separately and explicitly by the opener and
    keyword-sentence extraction below, which is where it belonged.

    Two filters keep that from becoming an unusable wall of text:
      * overlaps that are identical only because the underlying fact is - figures,
        comma-heavy provider/country enumerations, top-provider names;
      * anything with no evaluative or distinctive token in it, which leaves the
        judgments and stock reactions (the reusable habits) and drops the ordinary
        factual constructions that have no better phrasing anyway.
    """
    if not history:
        return []

    norm = [_normalize(t) for _, t in history]
    provider_tokens = {p.lower() for p in TOP_PROVIDERS}

    def grams(text: str, n: int) -> set:
        w = text.split()
        return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}

    hits: List[str] = []
    for n in (10, 9, 8, 7, 6, 5):
        for gram in set.union(*(grams(t, n) for t in norm)) if norm else set():
            count = sum(1 for t in norm if gram in t)
            if count < min_reviews:
                continue
            if re.search(r"\d", gram):
                continue                       # shared figures, not shared prose
            if gram.count(",") >= 2:
                continue                       # provider / country enumerations
            if any(p in gram for p in provider_tokens):
                continue
            tokens = set(gram.replace(".", "").replace(",", "").split())
            if not (tokens & EVALUATIVE_TOKENS or any(len(t) > 8 for t in tokens)):
                continue                       # plain factual construction, leave it
            if any(gram in existing for existing in hits):
                continue                       # already covered by a longer hit
            hits.append(gram)
    return hits[:40]


_SECTION_HEADER_RE = re.compile(
    # \*{0,2} rather than a literal \*\* on each side: a review sourced from the local
    # .md pipeline has literal "**General**" markdown, but one read back from the Drive
    # window (history.load_both_windows -> Drive's text/plain export) has already had
    # all markdown stripped by Google Docs - the same line arrives as a bare "General".
    # Matching only \*\* meant this regex silently matched zero lines against anything
    # pulled from Drive, which is every review the live app's window has ever supplied -
    # section-level opener/closer extraction has been dead code in production since
    # go-live, not just missing closers. Caught by testing against a real Drive export
    # rather than the local .md files used everywhere earlier in development.
    r"^\*{0,2}(General|Payments|Games|Responsible Gambling|Bonuses)\*{0,2}\s*$",
    re.MULTILINE,
)


def _section_boundary_lines(text: str) -> List[Tuple[str, str, str]]:
    """(section, first_line, last_line) for each of the 5 body sections found in text.

    Added after a real miss: the original extractor only ever captured the FIRST line
    of a section, so a repeated CLOSING device was invisible to the "already spent"
    list even though the model was shown the full prior text. Checked against 5 real
    reviews: General closed 4/5 with a near-identical "no pop-ups... nothing broken"
    formula, and Payments closed 3/5 sharing the literal string "in chat while your
    balance is still" - neither would have been caught without this.
    """
    out = []
    matches = list(_SECTION_HEADER_RE.finditer(text))
    for idx, m in enumerate(matches):
        section = m.group(1)
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        lines = [ln.strip() for ln in text[start:end].splitlines() if ln.strip()]
        if lines:
            out.append((section, lines[0], lines[-1]))
    return out


def collect_signatures(history: List[Tuple[str, str]]) -> Dict[str, list]:
    """Structured extraction of the model's own recent openers, closers and recurring
    phrases. Used two ways: rendered into the prompt as reflective context (see
    format_reflection, below - not a ban list), and checked against the finished
    review afterward (see check_leakage) as a real audit of whether that reflection
    actually changed anything, not a self-report.
    """
    openers: List[Tuple[str, str, str]] = []  # (title, label, text)
    closers: List[Tuple[str, str, str]] = []
    comparisons = set()

    for title, text in history:
        first = next(
            (ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith(("#", "**", "-", "*"))),
            "",
        )
        if first:
            openers.append((title, "opening line", first[:160]))
        for m in re.finditer(r"\*\*(?:\[)?([A-Z][A-Za-z0-9\.\' ]{2,20})(?:\])?", text):
            comparisons.add(m.group(1).strip())
        for section, first_line, last_line in _section_boundary_lines(text):
            openers.append((title, f"{section} opens", first_line[:140]))
            closers.append((title, f"{section} closes", last_line[:160]))

    # The SEO keyword sentence is a repetition magnet: it has a fixed job, so the same
    # construction gets reached for every time (observed 3/3: "The tension in this
    # <casino> Casino Review is...").
    keyword_sentences: List[Tuple[str, str]] = []
    for title, text in history:
        for sentence in re.split(r"(?<=[.!?])\s+", _normalize(text)):
            if "casino review" in sentence:
                keyword_sentences.append((title, sentence.strip()[:150]))
                break

    return {
        "openers": openers,
        "closers": closers,
        "keyword_sentences": keyword_sentences,
        "phrases": recurring_phrases(history),
        "comparisons": comparisons,
    }


def format_reflection(sig: Dict[str, list]) -> str:
    """Render collect_signatures()'s extraction as material for the model to reason
    about, not a checklist to satisfy.

    This replaces TWO earlier attempts, in order, and the reasoning for rejecting both
    matters for whatever comes after this one:

    1. A literal ban list ("ALREADY-SPENT MOVES - do not reuse ANY of these"). Proven
       to have a real ceiling, not just a theoretical one: "there's a casino, a
       sportsbook and an esports book" was explicitly listed as already-spent and
       still reappeared verbatim in 3 of 5 reviews in the same batch. Telling a model
       not to reuse a specific thing it was directly shown does not reliably hold.
    2. random.choice() over a small set of labeled opening styles, computed in code and
       handed over as this run's assignment. Goran rejected this on sight, correctly:
       a 5-option menu across a 5-review window collides on its own, and "write a
       verdict-first sentence" is itself a instruction the model can satisfy with the
       same canned verdict-first construction every time. It wasn't reasoning about
       its own repetition, it was matching a label - a different mechanical rule, not
       an escape from mechanical rules.

    What both attempts got wrong the same way: they tried to solve repetition FOR the
    model, either by forbidding or by pre-deciding. This hands over the same accurately
    extracted signal - openers, closers, recurring phrases, all still computed exactly
    as before - but frames it as a writer's own working notes, and leaves the actual
    judgment call (is this stale, does it matter, what would be genuinely different)
    to the model's own reasoning against the real context, not a rule.
    """
    if not any(sig.values()):
        return ""

    out = [
        "A LOOK AT YOUR OWN LAST FEW REVIEWS - read this the way you'd reread your own "
        "recent drafts before starting a new one, not as a list of rules. Below is "
        "exactly how you opened and closed each section, and any phrasing that came up "
        "more than once.\n"
        "The STRUCTURE below is fine and does not need to change - a section can end on "
        "the same kind of fact every time (Games ending on provably-fair and in-house, "
        "Bonuses closing on the loyalty club) without that being a problem. What matters "
        "is whether you are reaching for the same SENTENCE to say it. If two of the "
        "closers below are basically the same words in the same order, that is the "
        "thing to fix - say the same fact in a genuinely different sentence, not a "
        "different position or a different fact. Some repetition really is just the "
        "facts having the same shape (Adam repeats himself too when a casino has no cap "
        "and a stated minimum - there isn't ten ways to say that cleanly), so use "
        "judgment: is this wording load-bearing, or is it a groove you're stuck in?",
    ]

    review_openers = [(t, txt) for t, label, txt in sig["openers"] if label == "opening line"]
    if review_openers:
        out.append("\nHow you opened the review overall (Overview's first line):")
        out += [f'  - [{t}]: "{txt}"' for t, txt in review_openers]

    # Grouped BY SECTION, not by review - the previous review-first grouping let a
    # wording repeat hide in plain sight: Games' closer from review 1 sat pages away
    # from Games' closer from review 4, so nothing made the near-identical sentence
    # ("some titles are provably fair, so you can check/verify a result yourself...
    # in-house originals...") obvious. Stacking the same section's last few closers
    # adjacent makes reused wording visible on sight, not something to infer from
    # examples scattered across a much longer read.
    opens_by_section: Dict[str, list] = {s: [] for s in SECTIONS}
    for t, label, txt in sig["openers"]:
        section = label.rsplit(" ", 1)[0]
        if section in opens_by_section:
            opens_by_section[section].append((t, txt))

    closes_by_section: Dict[str, list] = {s: [] for s in SECTIONS}
    for t, label, txt in sig["closers"]:
        section = label.rsplit(" ", 1)[0]
        if section in closes_by_section:
            closes_by_section[section].append((t, txt))

    if any(opens_by_section.values()) or any(closes_by_section.values()):
        out.append(
            "\nHow you've opened and closed each SECTION, grouped by section so reused "
            "wording is obvious at a glance. Same topic in the same spot is fine - "
            "matching words are not:"
        )
        for s in SECTIONS:
            opens, closes = opens_by_section[s], closes_by_section[s]
            if not opens and not closes:
                continue
            out.append(f"\n  {s}:")
            out += [f'    opened [{t}]: "{txt}"' for t, txt in opens]
            out += [f'    closed [{t}]: "{txt}"' for t, txt in closes]

    if sig["keyword_sentences"]:
        out.append(
            "\nHow the SEO keyword phrase got worked in. This sentence has a fixed "
            "job, which makes it the easiest place to fall into a template without "
            "noticing:"
        )
        out += [f'  - [{t}] "{txt}"' for t, txt in sig["keyword_sentences"]]

    if sig["phrases"]:
        out.append(
            "\nWordings that came up more than once (computed, not hand-picked - some "
            "of these may just be the plain way to state a fact, judge each on its own):"
        )
        out += [f'  - "{p}"' for p in sig["phrases"]]

    if sig["comparisons"]:
        out.append(
            "\nCasinos you named as comparisons recently (not off-limits, but if one "
            "keeps becoming the go-to foil, ask whether a different casino from THE "
            "FIELD would actually make the point better this time): "
            + ", ".join(sorted(sig["comparisons"])[:40])
        )

    return "\n".join(out)


def _shared_8gram(a: str, b: str) -> Optional[str]:
    """First run of 8+ consecutive words from a that appears verbatim in b, if any.

    8 words is long enough that a hit is a real repeated construction, not two
    sentences that happen to share a common phrase - the same bar recurring_phrases()
    uses for judgment/fact separation, applied here to a single string pair.
    """
    words = a.split()
    if len(words) < 8:
        return None
    for i in range(len(words) - 7):
        gram = " ".join(words[i:i + 8])
        if gram in b:
            return gram
    return None


def check_leakage(new_review: str, sig: Dict[str, list]) -> List[str]:
    """Did anything on the banned list survive into the finished review anyway?

    Computed against the actual output rather than asked of the model, for the same
    reason every other checkable fact in this pipeline is verified in code: a model
    self-report risks confabulating a clean account regardless of what it actually
    wrote. This is what the UI's "repetition check" is built from.
    """
    norm_new = _normalize(new_review)
    hits: List[str] = []

    for title, label, text in sig["openers"] + sig["closers"]:
        gram = _shared_8gram(_normalize(text), norm_new)
        if gram:
            hits.append(f'{label} from [{title}] reappeared: "{gram}"')

    for title, text in sig["keyword_sentences"]:
        gram = _shared_8gram(text, norm_new)
        if gram:
            hits.append(f'keyword-sentence construction from [{title}] reappeared: "{gram}"')

    for p in sig["phrases"]:
        if p in norm_new:
            hits.append(f'banned recurring phrase reappeared verbatim: "{p}"')

    return hits


# ----------------------------------------------------------------------------
# PROMPTS
# ----------------------------------------------------------------------------

VOICE = """\
You are Adam Gros, founder and editor-in-chief of Gamblineers. Ten-plus years \
reviewing crypto casinos; background in mathematics and data analysis.

Voice: analytical, witty, blunt, honest. Sharp eye for BS, deep respect for data. \
Dry humour, never zany. You call things as they are and never sugarcoat.

Register - the part most easily lost, so read it twice. Every number below is measured \
from your own published CASINO REVIEWS, not from your guide pages, because the two read \
differently and reviews are what you are writing now:

- CONTRACTIONS, constantly, about twenty-two per thousand words - roughly one every two \
sentences. "doesn't", "you'll", "I'd", "that's", "won't", "here's", "isn't", "they've", \
"there's". Drafts keep coming back under this and it is the single most damaging drift, \
because prose without them reads like a trade journal instead of like you. Watch for it \
especially in the analytical stretches, where the temptation to write "it is" and "does \
not" is strongest.
- BE PRESENT. "I" roughly eight times per thousand words. Your judgment, your desk, \
your testing. Drafts keep coming back at half that rate and it makes the review sound \
institutional.
- SENTENCE LENGTH: no sentence goes past 35 words. That is a hard ceiling, not an \
average. If a sentence is running long, it is usually two judgments wearing one coat, \
so split it. Below that ceiling, aim for a median around 17 words and do not chop \
everything into clipped declaratives either; roughly one sentence in twenty comes in \
under 8 words.
- EXCLAMATION MARKS, sparingly but genuinely, about five per thousand words. Often \
inside a parenthetical: "The agents reply very fast (under a minute!), stay on point, \
and talk to you like a normal person." It must always sit at the end of real words - \
never a bare "(!)" on its own, which is a tic, not emphasis. Never decorative, and never \
on a verdict you would not say out loud with that much energy.
- TALK STRAIGHT AT THE READER, in roughly one paragraph in eight. Not every paragraph, \
and not as a tic.
- QUESTIONS ARE RARE IN YOUR REVIEWS. Under one per thousand words. You ask them on \
guide pages, not here. Do not pepper a review with rhetorical questions.
- CONCRETE, PHYSICAL IMAGES over abstractions. Not "support is responsive" but "they \
talk to you like a normal person". Not "the process is quick" but "You send, it \
confirms, you're done."
- PLAIN SPOKEN VOCABULARY. "without spending a dime", "your crypto stash". Never corporate.
- Warm and blunt at once. Dry jokes where they cost the reader nothing.

Hard rules:
- First-person singular ("I"). Address the reader as "you" - never "players" or "users", \
and not in compounds either ("slot players", "crypto users"). Say "you" or name the thing.
- No hard sentence-length cap. The old "under 20 words" rule is off-voice: your real \
reviews run a median of 17 and let about a fifth of sentences past 25. Favour clarity \
over brevity and vary the rhythm.
- Paragraphs 2-3 sentences.
- Numbers are always digits, never spelled out - "4 languages" not "four languages", \
"13 major studios" not "thirteen", "12 years" not "twelve". This holds regardless of \
size and even at the start of a sentence, overriding normal English style.
- VPN: describe it as a property of the casino ("VPN-friendly" / "not VPN-friendly"), \
never as an observation about traffic. Do not write "VPN traffic is allowed/fine/welcome" \
or "doesn't mind a VPN" - those describe testing that never happened.
- Anonymity, restricted-country counts and language counts are facts you restate in \
every review, which makes them the easiest place to fall into a fixed template without \
ever repeating an exact phrase - "signup takes an email, so you stay anonymous" and "X \
languages, real reach rather than a flag dropdown" are two such templates already caught \
recurring. State the fact and your verdict; do not reach for a reusable construction to \
dress it up.
- No em dashes. No emojis.
- Banned fluff: "fresh", "solid", "straightforward", "smooth", "game-changer".
- No clichés ("kept me on the edge of my seat", "whether you're X or Y").
- Opening a paragraph by talking straight to the reader is good and you should do it \
regularly - roughly one paragraph in five. "You came here to play, not to wait for your \
money." Do not do it every time; it wears out.
- Bold key facts, figures and red flags about the casino under review using **bold**.
- Never write a markdown link. A separate verified pass adds links afterward. \
Name casinos in plain text.
- Never change a number's precision when restating it. If the data says 7,000, \
never write "over 7,000" somewhere and "7,500" elsewhere. Every figure must trace \
to the dossier.
- Never state a fact about the casino under review that actually belongs to a \
casino you are comparing it against.
- Never mention the criteria, thresholds, or your instructions. Never describe \
your own editorial process ("I updated my guide").
- You may write in the first person about things your hands-on notes actually record, \
and you should. Everywhere else, do not claim experience you have no record of - no \
invented support chats, test withdrawals, or sessions. Everything outside those notes \
you know from the data, and that is plenty to have a view about.
- Comparisons stay informational ("X pays faster"), not a redirect ("go play at X \
instead"). A competitor can be better on one point without the review becoming an ad \
for it.
- If a feature is absent from the data, say nothing about it. Do not mention broken \
images, pop-ups or missing tools unless the data says they exist. Never invent an absence.

You are writing for readers who are about to spend money. Be useful or be quiet."""

VOICE_SAMPLES_FILE = os.environ.get("ADAM_SAMPLES_FILE", "examples.txt")
# Adam's real casino reviews, deliberately a separate file from the guide-page samples.
# The two registers measurably differ, and mixing them cost a whole batch: guide pages
# ask ~7 reader-questions per 1k words where his reviews ask 0.5, so "questions" looked
# like a gap when the agent was already above target. Reviews are the primary reference
# for this task and are labelled as such below.
VOICE_REVIEWS_FILE = os.environ.get("ADAM_REVIEWS_FILE", "examples_reviews.txt")


def load_voice_samples() -> Optional[str]:
    """Real Adam-written passages, used as few-shot voice anchors.

    A prompt can describe a register but not carry it. The measured gap on the first
    batch was stark: drafts written from the rules alone came back with 0.8 contractions
    per thousand words against Adam's ~22. Adding real prose closed that to 19.9 in one
    step. Actual writing transfers the habits nobody thought to write down, so this goes
    in the cached prefix and costs ~nothing per review after the first.

    Loads two files with different standing. examples_reviews.txt (real casino reviews)
    is the primary reference; examples.txt (guide and category pages) is secondary,
    because the registers measurably diverge - guide pages ask ~7 reader-questions per
    1k words where his reviews ask 0.5, and guide pages run far more clipped. Treating
    them as one corpus made "questions" look like a gap when the agent was already above
    the review-register target.

    Kept as files rather than constants so the samples can be extended without touching
    code - but note they sit in the cached prefix, so editing either one mid-batch
    invalidates the cache (as does editing VOICE or CRITERIA).
    """
    def read(p: str) -> Optional[str]:
        path = Path(p)
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return text or None

    reviews = read(VOICE_REVIEWS_FILE)
    guides = read(VOICE_SAMPLES_FILE)
    if not reviews and not guides:
        return None

    parts = [
        "YOUR OWN PUBLISHED WRITING - VOICE REFERENCE\n",
        "Everything below this line you wrote by hand for Gamblineers. It is here for "
        "one reason: so you can hear your own voice before you write. Match its rhythm, "
        "its contractions, its bluntness, the way it talks to a reader like a person.\n",
        "FOUR THINGS THIS IS NOT FOR:",
        "1. Facts. Every number, casino name and bonus below belongs to a different "
        "article and an older date, and plenty of it is now wrong. Several of these "
        "pieces review casinos that also appear in THE FIELD as possible comparisons, "
        "which makes this trap easy to fall into: if you want a figure for one of those "
        "casinos, take it from THE FIELD, never from the prose below. NEVER carry a "
        "fact, figure, bonus or recommendation out of here.",
        "2. Phrases. Do not lift sentences or distinctive turns of phrase. Sounding like "
        "yourself is the goal; quoting yourself is not.",
        "3. Sales pitch. Some of this is category and guide copy written to sell a click. "
        "A review has to be able to say a casino is bad, so take the register and the "
        "rhythm, not the hype.",
        "4. Punctuation. These passages contain dash punctuation (- and –). House style "
        "has since banned it outright, so the ban wins over the samples: use a comma, a "
        "colon, or a full stop instead. Do not copy a dash out of here.\n",
    ]

    if reviews:
        parts += [
            "===== PRIMARY REFERENCE: YOUR CASINO REVIEWS =====",
            "This is the form you are writing now, so weight it above everything else "
            "below. Note especially how rarely you ask the reader a question here, and "
            "how much room you give a sentence compared with your guide pages.\n",
            reviews,
        ]
    if guides:
        parts += [
            "\n===== SECONDARY REFERENCE: YOUR GUIDE AND CATEGORY PAGES =====",
            "Same voice, different job. Useful for warmth and directness, but it runs "
            "hotter, more clipped and far more question-heavy than a review should. Do "
            "not import that shape.\n",
            guides,
        ]
    parts.append("\n----- END YOUR WRITING -----")
    return "\n".join(parts)


CRITERIA = """\
GAMBLINEERS EDITORIAL CRITERIA - REFERENCE TABLE

This is calibration data, not a script. It tells you what the site considers good \
or bad so your verdicts stay consistent with every other review on the site. Consult \
it, apply judgment, and never recite it or mention a threshold to the reader.

Games:        >7,000 a lot | <5,000 low | between = average
Providers:    >70 a lot | <40 not many | between = average
Major studios (13): BetSoft, BGaming, Evolution Gaming, Microgaming, NetEnt, Novomatic,
              Play'n Go, Playtech, Pragmatic Play, QuickSpin, Red Tiger, Spinomenal, Yggdrasil
              0 missing = rare, exceptional | 1-2 = strong | 3-4 = could do better | 5+ = behind
Cryptos:      >15 above average | 5-15 average | <5 barely qualifies as a crypto casino
Withdrawal limits: daily >$3,000 good, <$2,000 poor | weekly >$12,000 good, <$8,000 poor
              | monthly >$24,000 good, <$16,000 poor | "unlimited"/blank = no cap, a real plus
Time buckets: <12h fast | 12-24h same-day | 24-72h a few days | >72h slow
KYC = 0 hours: means NO identity verification at all (not "none up to a threshold").
              Some such casinos still do soft checks (email/phone). Say what it means for the reader.
Restricted countries: >40 is worth a warning so the reader checks eligibility first
Casino age:   older than 5 years is a positive signal (survived, tightened security, refined UX).
              Under 5 years: say nothing about age.
Buy crypto on site: present = a convenience, usually at a markup. Absent = a real
              limitation worth naming, not a footnote. Worth its own clause either way -
              form the specific angle yourself each time rather than reaching for the
              same framing; it has already recurred near-verbatim across reviews.
Convert crypto in the casino: uncommon and worth crediting when present. Absent is the
              norm, so a plain one-line note is enough - don't build it into a bigger
              point than it is.
RG tools (besides self-exclusion): 3-4 above average | 1-2 average | 0 needs to step up
              Self-exclusion is the baseline every licensed casino should have.
              Cooling-off is uncommon; credit it when present.
              Tools that need a support ticket are worse than self-serve, better than nothing.
Bonuses:      first-deposit match 100-125% average, below low, above generous
              first-deposit value <$500 low, >1.5 BTC or >$10k very generous
              first-deposit free spins ~100 average, below low, above generous
              min deposit $10-30 average, below very player-friendly, above high
              wagering 40x average, <40x player-friendly, >40x high.
              Wagering on bonus+deposit is materially worse - say so.
              no-deposit free spins 20-30 average, below low, above generous"""

OUTPUT_SPEC = f"""\
OUTPUT FORMAT - follow exactly.

Plain markdown, nothing before the first header, no commentary about your work.

# {{casino}} review

**Overview**
3 paragraphs, sandwiched pros - cons - pros: open on genuine strength, name the real
catch in the middle, close back on strength so the review doesn't end up feeling
front-loaded with the downside. The close isn't a rebuttal of the middle paragraph -
it's a separate, genuine strength. Must contain the exact phrase "{{keyword}}" verbatim,
once, reading naturally. This is a hook, not a summary of the sections.

**TLDR**
- 5 to 6 bullets. ONE fact per bullet, not several stitched together with commas and
  "but" - if you're using a semicolon or a second "and" to fit in another figure, split
  it into its own bullet or drop it. Pick the most important point per bullet, not
  every point. Every number copied verbatim from your own body text at the same
  precision. No bullet may contradict another.

Then these five sections, in this order, each headed exactly like this:

{chr(10).join(f'**{s}**' for s in SECTIONS)}

Within a section, YOU decide order, emphasis, what leads and what gets one clause.
Cover what the data supports and skip what it does not. Bonuses must present bonus
types in this fixed order where they exist: no-deposit, then welcome/first-deposit,
then sports, then faucet.

Formatting: **bold** for emphasis, "- " for bullets, no markdown links. No heading
level other than the section headers above (no ##), EXCEPT inside Bonuses: give each
individual bonus its own "### Bonus Name" heading (e.g. "### Exclusive No Deposit
Bonus", "### Standard First Deposit Bonus") - ### is reserved for this and used for
nothing else.

For each bonus: describe the type in prose without figures, then put every specific
figure in bullets underneath. Do NOT comment on every term of every bonus - that reads
as a checklist. Comment only where there's something worth saying: a term that's
genuinely better or worse than the field, or one worth comparing to a competitor.
An unremarkable bonus can go uncommented, or get one line. Save the real commentary
for the one or two bonuses (or terms) that actually stand out, good or bad."""

TASK = """\
Write the review.

Before you write, think it through:

1. Read the dossier and find the STORY. What is actually true about this place? Where
   does it sit in the field? What is the one thing a reader most needs to know, and
   what is the tension worth resolving? A review with a spine beats five sections that
   each dutifully hit their marks.
2. Choose comparisons on relevance, not ranking. You can see the whole field. Pick the
   casino that makes a specific point land - sometimes that is a WORSE casino, to show
   how good this one is. Do not name the same handful of casinos every review reaches
   for, and do not compare on a metric where the difference is trivial. Not every
   section needs a comparison. Every number you attribute to a comparison casino must
   come from THE FIELD list. Never invent an unnamed strawman to compare against
   ("most casinos", "a flag dropdown", "some sites don't bother") - that isn't a real
   comparison, it isn't falsifiable, and it's exactly the kind of line that turns into
   a template you reach for every time a fact needs dressing up. Name a real casino
   from THE FIELD, or skip the comparison and just state the fact.
3. Read the look-at-your-own-last-few-reviews section below, if there is one, before
   you write - the same way you'd reread your own recent drafts. It groups your own
   openers and closers by section, so you can see at a glance whether you keep reaching
   for the same SENTENCE. This is not about structure - a section ending on the same
   kind of fact every time (Games on provably-fair, Bonuses on the loyalty club) is
   completely fine, that's just where those facts belong. The thing to catch is
   wording: if two entries under the same section are basically the same words in the
   same order, say that fact in a genuinely different sentence this time - different
   words, different rhythm, maybe a different vantage point - without moving where it
   sits or inventing a reason to skip it.
4. Verify every figure against the dossier before you commit to it.
5. Never state a literal rank, position, or "Nth of 78" in the review - the dossier's
   field-standing lines are for your own fact-checking, not for the reader. Only frame
   something as "one of the highest/lowest I track" when the dossier itself says that
   tier is earned; a mid-field number gets your plain verdict from the criteria table
   and no positional language at all.
6. A feature worth mentioning (buy crypto on site, converting tokens, cooling-off,
   provably fair, in-house games...) gets its own clause with your judgment on it - not
   a trailing half-sentence bolted onto a different point.

Two failure modes to avoid, in tension with each other:

  - The checklist: every paragraph mapping 1:1 to a criterion, opinion reduced to a
    stock reaction bolted onto a data point ("which I appreciate", "that's already a
    win"). This is the one we are trying to escape.
  - Trying-too-hard: meta-asides in brackets, notes to yourself, jokey hyperbole,
    quirky tangents, addressing the casino instead of the reader. Reaching for
    personality instead of having a view. This is worse than dry.

Real opinion is specific and falsifiable. It says what is good, for whom, and why -
and what would have to change. Aim for the shortest review that says everything
worth saying; if a sentence carries no fact and no judgment, cut it.

Output only the review."""

REVISE = """\
Below is a review you just wrote, and the source dossier it came from.

Check it, in this order:
1. FACTS: every number and claim traces to the dossier at the same precision. Every
   fact attributed to a comparison casino belongs to that casino. Flag nothing you
   cannot verify - fix it.
2. CONTRADICTIONS: no two statements disagree, including TLDR vs. body.
3. VOICE RULES: no em dashes, no banned fluff words, no markdown links, no sentence
   over ~20 words, reader addressed as "you", no paragraph opening by addressing the
   reader, no addressing the casino, no meta-commentary or bracketed asides.
4. REPETITION: no opener or device echoing the recent reviews shown earlier.
5. FORMAT: exact section headers, keyword present verbatim, TLDR 4-5 bullets.

Rewrite only what actually breaks a rule. Anything already correct comes back
byte-for-byte identical. Never add a fact, and never "improve" clean prose.

Output only the corrected review, nothing else."""


def assemble(db: CasinoDB, row: List[str], keyword: str, history: List[Tuple[str, str]],
             focus_name: str, signature_history: Optional[List[Tuple[str, str]]] = None
             ) -> Tuple[list, str, Dict[str, list]]:
    """Returns (system_blocks, user_text, signatures).

    signatures is collect_signatures()'s raw output - returned so the caller can run
    check_leakage() against the finished review afterward, not just format the ban
    list into the prompt.

    Cache boundary matters: render order is system -> messages, and any byte change
    invalidates everything after it. So everything stable (voice, criteria, the whole
    field) goes in `system` behind a cache breakpoint, and everything that changes per
    review (this dossier, the rolling history) goes in `messages`. Across a batch the
    ~6k-token field is then billed once, not once per casino.
    """
    system_blocks = [{"type": "text", "text": VOICE}]

    samples = load_voice_samples()
    if samples:
        system_blocks.append({"type": "text", "text": samples})
    else:
        print(f"NOTE: no voice samples found at {VOICE_SAMPLES_FILE} - "
              "running on the written rules alone, which measurably weakens the voice.",
              file=sys.stderr)

    system_blocks += [
        {"type": "text", "text": CRITERIA},
        {
            "type": "text",
            "text": build_landscape(db, focus_name),
            # 1h TTL: a batch of 5-7 reviews takes longer than the default 5m.
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]

    parts = [
        build_dossier(db, row),
        "",
        build_bonuses(db, focus_name),
        "",
        build_comments(db, focus_name),
        "",
    ]

    # sig is used two ways below: rendered as reflective context in the prompt itself
    # (format_reflection), and returned so generate_review() can run check_leakage()
    # against the FINISHED review afterward - a real audit of whether reflection
    # actually changed anything, independent of whatever the model did with it.
    sig: Dict[str, list] = {"openers": [], "closers": [], "keyword_sentences": [],
                            "phrases": [], "comparisons": set()}
    if history:
        parts.append(
            f"THE LAST {len(history)} REVIEWS PUBLISHED (newest first). Read them for "
            "rhythm and habits, then write this one with real variability from them - "
            "a different shape, not just different words for the same shape. Do not "
            "treat any fact in them as applying to this casino.\n"
        )
        for title, text in history:
            parts.append(f"----- BEGIN PRIOR REVIEW: {title} -----\n{text}\n----- END -----\n")
        sig = collect_signatures(signature_history or history)
        reflection = format_reflection(sig)
        if reflection:
            parts.append(reflection)
        parts.append("")
    else:
        parts.append("(No prior reviews available for comparison this run.)\n")

    parts.append(OUTPUT_SPEC.replace("{casino}", focus_name).replace("{keyword}", keyword))
    parts.append("")
    parts.append(TASK)
    return system_blocks, "\n".join(parts), sig


# ----------------------------------------------------------------------------
# GENERATION
# ----------------------------------------------------------------------------

def _drain_with_progress(stream, label: str = "write", progress=None):
    """Consume a stream, printing a live progress line, and return the final message.

    Exists because silence looked exactly like a hang. Opus at effort=high with
    adaptive thinking spends minutes on a 45k-token prompt before emitting any visible
    text, and the original code streamed without printing anything - so a healthy slow
    run and a dead connection were indistinguishable, and a real run got killed with
    Ctrl+C on the assumption it had frozen. Thinking tokens are counted separately from
    review text so it is obvious which phase is active.
    """
    start = time.monotonic()
    thinking = text = 0
    last_draw = 0.0

    def draw(final: bool = False) -> None:
        elapsed = time.monotonic() - start
        phase = "writing" if text else "thinking"
        if progress is not None:
            # Web UI: hand over structured state and let the caller render it.
            progress(label=label, phase=phase, elapsed=elapsed,
                     thinking_chars=thinking, text_chars=text, final=final)
            return
        line = (f"\r  [{label}] {phase}... {elapsed:5.0f}s  "
                f"thinking {thinking:,} chars  review {text:,} chars")
        sys.stderr.write(line + ("\n" if final else "   "))
        sys.stderr.flush()

    for event in stream:
        if getattr(event, "type", "") == "content_block_delta":
            delta = getattr(event, "delta", None)
            dtype = getattr(delta, "type", "")
            if dtype == "thinking_delta":
                thinking += len(getattr(delta, "thinking", "") or "")
            elif dtype == "text_delta":
                text += len(getattr(delta, "text", "") or "")
        now = time.monotonic()
        if now - last_draw > 0.5:
            last_draw = now
            draw()
    draw(final=True)
    return stream.get_final_message()


def generate(system_blocks: list, user_text: str, effort: str, max_tokens: int,
             progress=None) -> Tuple[str, object]:
    import anthropic

    # Explicit generous timeout: a long thinking phase can sit silent for minutes, and
    # the SDK default would otherwise decide for us. max_retries=1 because a mid-stream
    # retry regenerates the whole review and pays for it twice.
    client = anthropic.Anthropic(api_key=config.anthropic_api_key(),
                                 timeout=1800.0, max_retries=1)
    try:
        # Streaming: max_tokens is high enough that a non-streaming request risks
        # an HTTP timeout, and adaptive thinking makes turns longer.
        with client.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            system=system_blocks,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": user_text}],
        ) as stream:
            message = _drain_with_progress(stream, "write", progress)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted before the review finished - nothing was saved. "
                 "This model can sit silent for a few minutes while it thinks; the "
                 "progress line shows it is alive. Use --effort medium for a faster run.")
    except anthropic.NotFoundError:
        sys.exit(f"Model {MODEL} not available to this key.")
    except anthropic.AuthenticationError:
        sys.exit("Bad ANTHROPIC_API_KEY.")
    except anthropic.RateLimitError as e:
        sys.exit(f"Rate limited. Retry after {e.response.headers.get('retry-after', '60')}s.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Network error reaching the Anthropic API.")

    if message.stop_reason == "refusal":
        detail = getattr(message.stop_details, "explanation", "") or ""
        sys.exit(f"Model declined this request. {detail}")
    if message.stop_reason == "max_tokens":
        print("WARNING: hit max_tokens - output is truncated. Raise --max-tokens.",
              file=sys.stderr)

    text = "".join(b.text for b in message.content if b.type == "text").strip()
    return text, message.usage


def revise(review: str, dossier: str, effort: str, max_tokens: int,
           progress=None) -> Tuple[str, object]:
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key(),
                                 timeout=1800.0, max_retries=1)
    prompt = f"{REVISE}\n\n--- SOURCE DOSSIER ---\n{dossier}\n\n--- REVIEW ---\n{review}"
    with client.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        system="You are a meticulous copy editor. You fix only what breaks a stated "
               "rule and never rewrite prose that is already correct.",
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = _drain_with_progress(stream, "revise", progress)

    cleaned = "".join(b.text for b in message.content if b.type == "text").strip()
    # Fail open: a revise pass that truncates a good review is worse than the
    # defects it was meant to catch.
    if not cleaned or len(cleaned) < 0.6 * len(review):
        print("Revise pass looked truncated - keeping the original.", file=sys.stderr)
        return review, message.usage
    return cleaned, message.usage


def report_cost(label: str, usage) -> None:
    if usage is None:
        return
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    c_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    c_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    # Opus 5: $5/1M in, $25/1M out; cache read ~0.1x, cache write ~1.25x.
    cost = (inp * 5 + c_read * 0.5 + c_write * 6.25 + out * 25) / 1_000_000
    print(
        f"  [{label}] in={inp} cache_read={c_read} cache_write={c_write} "
        f"out={out} -> ~${cost:.3f}",
        file=sys.stderr,
    )
    if c_read == 0 and c_write == 0:
        print("  (no cache activity - expected on the first review of a batch)", file=sys.stderr)


def cost_of(usage) -> float:
    """Approximate USD for one call. Fable 5.1 (MODEL as of 2026-09-18): $10/1M in,
    $50/1M out, cache read 0.025x ($0.25 - Fable's cache-hit multiplier is 4x cheaper
    than Opus's 0.1x). Cache write is 2x base ($20) because assemble() writes with
    ttl="1h", not the 5-minute default (1.25x/$12.50) - rates confirmed against
    platform.claude.com/docs pricing 2026-09-18. NOT model-aware: hardcoded to
    whatever MODEL currently is, update both together."""
    if usage is None:
        return 0.0
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    c_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    c_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (inp * 10 + c_read * 0.25 + c_write * 20 + out * 50) / 1_000_000


# ----------------------------------------------------------------------------
# PROGRAMMATIC ENTRY POINT (used by the Streamlit app)
# ----------------------------------------------------------------------------

def generate_review(
    db: "CasinoDB",
    casino: str,
    keyword: Optional[str] = None,
    history: Optional[List[Tuple[str, str]]] = None,
    signature_history: Optional[List[Tuple[str, str]]] = None,
    effort: str = "high",
    max_tokens: int = 32000,
    do_revise: bool = False,
    progress=None,
) -> Dict[str, object]:
    """Write one review and return it plus metadata. Raises LookupError for an unknown
    casino, and whatever the API raises on a hard failure.

    Separated from main() so the CLI and the web app share one code path - the only
    difference between them is where the rolling window comes from (local files vs the
    Drive folder) and how progress is displayed.
    """
    row = db.find(casino)
    focus = cell(row, COL["name"])
    keyword = keyword or f"{focus} Casino Review"
    history = history or []
    signature_history = signature_history if signature_history is not None else history

    system_blocks, user_text, sig = assemble(db, row, keyword, history, focus, signature_history)
    review, usage = generate(system_blocks, user_text, effort, max_tokens, progress=progress)
    total_cost = cost_of(usage)

    revise_usage = None
    if do_revise:
        review, revise_usage = revise(review, build_dossier(db, row), effort, max_tokens,
                                      progress=progress)
        total_cost += cost_of(revise_usage)

    # Computed against the finished text, not asked of the model - see check_leakage()'s
    # docstring for why a self-report isn't trusted here the way a code-checkable fact is.
    repetition_leaks = check_leakage(review, sig)

    data_flags = [ln for ln in review.splitlines() if ln.strip().startswith("DATA FLAG:")]
    return {
        "casino": focus,
        "keyword": keyword,
        "review": review,
        "cost": total_cost,
        "usage": usage,
        "revise_usage": revise_usage,
        "data_flags": data_flags,
        "history_titles": [t for t, _ in history],
        "banned_move_count": (len(sig["openers"]) + len(sig["closers"])
                              + len(sig["keyword_sentences"]) + len(sig["phrases"])),
        "repetition_leaks": repetition_leaks,
        "chars": len(review),
        "words": len(review.split()),
    }


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Agentic single-pass casino review writer (prototype).")
    ap.add_argument("casino", help='Brand name exactly as in the Data tab, e.g. "BitStarz"')
    ap.add_argument("--keyword", default=None, help='SEO phrase (default "<casino> Casino Review")')
    ap.add_argument("--history-dir", default="reviews_agent",
                    help="Rolling-window dir; new reviews are written here (default reviews_agent)")
    ap.add_argument("--seed-dir", default="reviews",
                    help="Extra read-only dir of prior reviews to seed the window (default reviews)")
    ap.add_argument("--history", type=int, default=5, help="How many prior reviews to show (default 5)")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-tokens", type=int, default=32000)
    ap.add_argument("--revise", action="store_true", help="Add a fact/voice self-check pass")
    ap.add_argument("--dry-run", action="store_true", help="Write the prompt to disk, make no API call")
    args = ap.parse_args()

    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. export it, or use --dry-run.")

    keyword = args.keyword or f"{args.casino} Casino Review"

    print(f"Loading casino database...", file=sys.stderr)
    db = CasinoDB()
    try:
        row = db.find(args.casino)
    except LookupError as e:
        sys.exit(str(e))
    focus_name = cell(row, COL["name"])
    if not db.is_live(focus_name):
        print(f"NOTE: {focus_name} is not marked Live on {SITE} "
              f"(status: {db.status_by_name.get(focus_name, 'unknown')}).", file=sys.stderr)

    hist_dir = Path(args.history_dir)
    seed_dir = Path(args.seed_dir)
    history = load_history([hist_dir, seed_dir], focus_name, args.history)
    signature_history = load_history([hist_dir, seed_dir], focus_name, args.history,
                                     exclude_same_casino=False)
    print(f"Rolling window: {len(history)} prior review(s) "
          f"[{', '.join(t for t, _ in history) or 'none'}]", file=sys.stderr)
    extra = [t for t, _ in signature_history if t not in {x for x, _ in history}]
    if extra:
        print(f"  + phrase bans also drawn from this casino's own past review(s): "
              f"[{', '.join(extra)}]", file=sys.stderr)

    system_blocks, user_text, sig = assemble(db, row, keyword, history, focus_name,
                                             signature_history)

    if args.dry_run:
        out = Path(f"prompt_{focus_name.replace(' ', '_')}.txt")
        sys_text = "\n\n".join(b["text"] for b in system_blocks)
        out.write_text(
            f"===== SYSTEM ({len(sys_text)} chars) =====\n{sys_text}\n\n"
            f"===== USER ({len(user_text)} chars) =====\n{user_text}",
            encoding="utf-8",
        )
        total = len(sys_text) + len(user_text)
        print(f"\nDry run. Prompt written to {out}")
        print(f"  system: {len(sys_text):>7} chars")
        print(f"  user:   {len(user_text):>7} chars")
        print(f"  total:  {total:>7} chars (~{total // 4:,} tokens, rough)")
        return

    print(f"Writing {focus_name} review with {MODEL} (effort={args.effort})...", file=sys.stderr)
    review, usage = generate(system_blocks, user_text, args.effort, args.max_tokens)
    report_cost("write", usage)

    if args.revise:
        print("Running self-check pass...", file=sys.stderr)
        review, usage2 = revise(review, build_dossier(db, row), args.effort, args.max_tokens)
        report_cost("revise", usage2)

    hist_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_path = hist_dir / f"{focus_name} Review (agent {stamp}).md"
    out_path.write_text(review, encoding="utf-8")

    words = len(review.split())
    print(f"\nWrote {out_path}", file=sys.stderr)
    print(f"  {len(review):,} chars / {words:,} words", file=sys.stderr)
    if "DATA FLAG:" in review:
        for line in review.splitlines():
            if line.startswith("DATA FLAG:"):
                print(f"  !! {line}", file=sys.stderr)

    banned_count = (len(sig["openers"]) + len(sig["closers"])
                   + len(sig["keyword_sentences"]) + len(sig["phrases"]))
    leaks = check_leakage(review, sig)
    print(f"\nRepetition check: {banned_count} move(s) banned from the window.", file=sys.stderr)
    if leaks:
        for l in leaks:
            print(f"  LEAKED: {l}", file=sys.stderr)
    else:
        print("  none reappeared in this review." if banned_count else
              "  (no window this run - nothing to check.)", file=sys.stderr)
    print(f"\nThis review is now part of the rolling window for the next run.", file=sys.stderr)


if __name__ == "__main__":
    main()
