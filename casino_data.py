"""
casino_data.py - the casino-fact data layer for the Jakob/BCK single-agent pipeline.

Three distinct layers, in the order a review actually needs them:

1. CasinoDB + the field/dossier builders - ported near-verbatim from writeReviewAdam2's
   writeReviewAgent.py. The schema is identical because it's the literal same
   spreadsheet (confirmed by Goran 2026-09-16, not assumed) - same Data/Bonuses/
   Comments/StatusLog tabs, same 48-column layout. Every hard-won fix from that
   project (withheld enumerable lists, computed provider set-difference, computed
   casino age, computed field-of-N ranking with the reader-facing framing collapsed to
   qualitative tiers, the Trading-feature disambiguation written as non-quotable
   prose) carries over unchanged, because the underlying defect class - arithmetic or
   set-membership left to the model instead of computed in code - is not specific to
   Gamblineers.

2. The Evolution system - ported from the legacy writeReviewJacob.py, with no
   equivalent in Adam2 at all. Looks up a casino's previously-published review via a
   WP-ID mapping spreadsheet, reads it straight out of the live WordPress MySQL
   database, and extracts comparable facts so a new review can state real deltas
   ("grew from 5,000 to 15,000 games") instead of writing as if the site has no
   history. Fails open at every step (missing WP-ID, DB connection failure, review
   too short to bother with) - an old-review comparison is enrichment, never
   something a generation should die over.

3. Player-feedback scraping - reuses the existing askgamblers_scraper.py/
   trustpilot_scraper.py as-is (both already in this repo, both live/Playwright-based,
   kept running live per generation per Goran's explicit choice). Deliberately does
   NOT port the legacy pipeline's two separate Claude summarization calls
   (generate_general_player_summary/generate_withdrawal_player_summary) - in the
   single-agent design the raw prepared review digest goes straight into the main
   dossier, and the one generating call synthesizes both the "what do players say"
   answer and any Payments-relevant withdrawal commentary itself, carrying the same
   hard-won rules (never name the platform, generalize rather than singling out one
   reviewer, weigh positives more heavily since negative reviews skew toward
   emotional post-loss players) as prompt content instead of a second API call.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import config

SPREADSHEET_ID = config.get("SPREADSHEET_ID")
SITE = config.get("SITE")  # "BCK"

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

FIELD_RANK_EXTREME = 5


# ----------------------------------------------------------------------------
# SHEET ACCESS
# ----------------------------------------------------------------------------

def _sheets_client():
    return config.sheets_service()


def _fetch(sheets, rng: str, spreadsheet_id: Optional[str] = None) -> List[List[str]]:
    return (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id or SPREADSHEET_ID, range=rng,
             valueRenderOption="FORMATTED_VALUE")
        .execute()
        .get("values", [])
    )


def cell(row: List[str], idx: int) -> str:
    return row[idx].strip() if idx < len(row) and row[idx] else ""


def count_list(value: str) -> Optional[int]:
    if not value:
        return None
    return len([p for p in value.split(",") if p.strip()])


def to_int(value: str) -> Optional[int]:
    m = re.search(r"\d[\d,]*", value or "")
    return int(m.group(0).replace(",", "")) if m else None


class CasinoDB:
    def __init__(self):
        sheets = _sheets_client()
        self.header_rows = _fetch(sheets, "Data!A1:AV3")
        self.data_rows = [r for r in _fetch(sheets, "Data!A4:AV200") if cell(r, 0)]
        self.bonus_header = _fetch(sheets, "Bonuses!A1:AC2")
        self.bonus_rows = [r for r in _fetch(sheets, "Bonuses!A3:AC1000") if cell(r, 0)]
        self.comment_rows = [r for r in _fetch(sheets, "Comments!A2:R200") if cell(r, 0)]
        self.status_rows = [r for r in _fetch(sheets, "StatusLog!A2:I1000") if cell(r, 1)]

        self.status_by_name = {
            cell(r, 1): cell(r, STATUSLOG_SITE_COL.get(SITE, 3)) for r in self.status_rows
        }
        self.labels = self._build_labels()

    def _build_labels(self) -> Dict[int, str]:
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
        for r in self.data_rows:
            if cell(r, 0).lower() == name.lower():
                return r
        close = [cell(r, 0) for r in self.data_rows if name.lower() in cell(r, 0).lower()]
        hint = f" Did you mean: {', '.join(close[:5])}?" if close else ""
        raise LookupError(f'Casino "{name}" not found in the Data tab.{hint}')

    def live_casino_names(self) -> List[str]:
        return sorted(
            cell(r, COL["name"]) for r in self.data_rows
            if cell(r, COL["name"]) and self.is_live(cell(r, COL["name"]))
        )

    def is_live(self, name: str) -> bool:
        return self.status_by_name.get(name, "") == "Live"

    def casino_id(self, row: List[str]) -> str:
        return cell(row, COL["id"])


# ----------------------------------------------------------------------------
# LANDSCAPE
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
    lines, skipped = [], 0
    for row in db.data_rows:
        name = cell(row, COL["name"])
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

def field_rank(db: CasinoDB, focus: str, label: str, getter) -> Optional[str]:
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
    own, total = scored[i][0], len(scored)

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

    rows = [r for r in db.bonus_rows
            if cell(r, 0).lower() == name.lower() and SITE.lower() in cell(r, 1).lower()]
    if not rows:
        return f"BONUS DATA: no bonus rows on file for this casino on {SITE}."

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
        "MANUAL RESEARCH NOTES (hand-written by the research team; usually the most "
        "interesting material in the whole dossier):\n"
        f"{body}\n\n"
        "DATA-HYGIENE WARNING: these notes are not always re-audited after a casino "
        "rebrands, so a note may refer to this casino by a FORMER name, or describe "
        "another brand entirely. If a note's brand name does not match the casino "
        "under review, do NOT repeat that name as current fact. Use the substance if "
        "it clearly belongs to this casino, and add a line at the very end of your "
        "output starting with 'DATA FLAG:' describing the discrepancy."
    )


# ----------------------------------------------------------------------------
# EVOLUTION SYSTEM - previous-review comparison, no Adam2 equivalent
# ----------------------------------------------------------------------------

def get_review_wp_id(casino_id: str) -> Optional[str]:
    """Casino ID -> WordPress post ID of its previously published review."""
    if not casino_id:
        return None
    try:
        sheets = _sheets_client()
        rows = _fetch(sheets, "WP_IDs!A:B", config.get("CALCULATION_SPREADSHEET_ID"))
        for row in rows:
            if len(row) >= 2 and row[0].strip() == casino_id:
                return row[1].strip()
        return None
    except Exception as e:  # noqa: BLE001 - fail open, this is enrichment
        print(f"Error looking up WP ID for casino {casino_id}: {e}")
        return None


def fetch_old_review_from_mysql(wp_id: str) -> Tuple[Optional[str], Optional[str]]:
    """(post_content, post_name) for a WP post ID, or (None, None) on any failure."""
    if not wp_id:
        return None, None
    mysql_cfg = config.mysql_config()
    if not mysql_cfg:
        print("MySQL not configured, skipping old-review fetch")
        return None, None
    try:
        import pymysql  # noqa: PLC0415
        connection = pymysql.connect(
            host=mysql_cfg["host"],
            port=int(mysql_cfg.get("port", 3306)),
            user=mysql_cfg["user"],
            password=mysql_cfg["password"],
            database=mysql_cfg["database"],
            connect_timeout=10,
            read_timeout=15,
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT post_content, post_name FROM wp_posts WHERE ID = %s", (int(wp_id),)
                )
                result = cursor.fetchone()
                return result if result else (None, None)
        finally:
            connection.close()
    except Exception as e:  # noqa: BLE001
        print(f"Error fetching old review from MySQL (WP ID {wp_id}): {e}")
        return None, None


def strip_html_to_text(html_content: str) -> str:
    if not html_content:
        return ""
    text = re.sub(r"<[^>]+>", " ", html_content)
    text = re.sub(r"\[/?[^\]]+\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for a, b in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "),
                ("&#8217;", "'"), ("&#8216;", "'"), ("&#8220;", '"'), ("&#8221;", '"')]:
        text = text.replace(a, b)
    return text


def _compute_relative_time(date_value) -> Optional[str]:
    """Human-readable relative time ("about a year ago") from a date string."""
    if not date_value:
        return None
    try:
        if isinstance(date_value, str):
            date_value = date_value.strip()
            for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
                try:
                    date_value = datetime.strptime(date_value, fmt)
                    break
                except ValueError:
                    continue
            else:
                print(f"Could not parse date string: {date_value}")
                return None
        months = (datetime.now() - date_value).days // 30
        if months < 1:
            return "a few weeks ago"
        if months == 1:
            return "about a month ago"
        if months <= 3:
            return "a couple of months ago"
        if months <= 6:
            return "a few months ago"
        if months <= 11:
            return "about half a year ago"
        if months <= 14:
            return "about a year ago"
        if months <= 20:
            return "over a year ago"
        if months <= 30:
            return "about two years ago"
        return f"about {months // 12} years ago"
    except Exception as e:  # noqa: BLE001
        print(f"Error computing relative time: {e}")
        return None


def get_review_date_from_affsites(post_name: str) -> Optional[str]:
    """Last Updated date for a review, by matching its post_name slug in the AFF
    SITES spreadsheet's BCK tab (column D = URL Slug, column E = Last Updated)."""
    if not post_name:
        return None
    try:
        sheets = _sheets_client()
        tab = config.get("AFF_SITES_TAB")
        rows = _fetch(sheets, f"{tab}!D:E", config.get("AFF_SITES_SPREADSHEET_ID"))
        variants = {f"/reviews/{post_name}/", f"/reviews/{post_name}",
                   f"/{post_name}/", f"/{post_name}"}
        for row in rows:
            if row and row[0].strip() in variants:
                return row[1].strip() if len(row) >= 2 and row[1].strip() else None
        return None
    except Exception as e:  # noqa: BLE001
        print(f"Error looking up review date from AFF SITES: {e}")
        return None


def fetch_evolution_context(casino_id: str, casino_name: str) -> Tuple[str, str]:
    """WP-ID lookup -> MySQL fetch -> AFF-SITES date lookup, all fail-open.

    Returns (old_review_plain_text, status_message). Deliberately returns the RAW
    stripped text rather than pre-extracting structured facts via a separate Claude
    call (the legacy extract_evolution_facts() step) - the single generating call
    already reads the current dossier in full, so handing it the raw prior review
    plus how long ago it was published lets it identify and phrase whatever deltas
    are actually interesting itself, rather than working from someone else's
    pre-filtered fact list.
    """
    if not casino_id:
        return "", "No Casino ID - old review comparison skipped"

    wp_id = get_review_wp_id(casino_id)
    if not wp_id:
        return "", f"No WP_ID on file for Casino ID {casino_id}"

    html, post_name = fetch_old_review_from_mysql(wp_id)
    if html is None:
        return "", f"Could not fetch review from database for WP ID {wp_id}"

    plain = strip_html_to_text(html)
    if len(plain.strip()) < 100:
        return "", f"Old review too short after stripping ({len(plain)} chars)"

    date_str = get_review_date_from_affsites(post_name)
    relative = _compute_relative_time(date_str)
    time_note = f" (published {relative})" if relative else ""

    max_chars = 15000
    if len(plain) > max_chars:
        plain = plain[:max_chars]

    return (
        f"PREVIOUSLY PUBLISHED REVIEW OF THIS CASINO{time_note}:\n{plain}",
        f"Found and loaded ({len(plain)} chars){time_note}",
    )


# ----------------------------------------------------------------------------
# PLAYER FEEDBACK - live scraping, kept per-generation per Goran's explicit choice
# ----------------------------------------------------------------------------

def scrape_player_feedback(casino_name: str) -> Dict:
    """AskGamblers + Trustpilot reviews for a casino. {} if neither source has any.

    Both scrapers already exist in this repo (askgamblers_scraper.py,
    trustpilot_scraper.py) and are reused unchanged - this only orchestrates them.
    """
    from askgamblers_scraper import AskGamblersScraper  # noqa: PLC0415
    from trustpilot_scraper import TrustpilotScraper  # noqa: PLC0415

    all_reviews, sources = [], []
    try:
        ag = AskGamblersScraper(timeout=30).scrape_casino_reviews(casino_name, max_reviews=50, months=6)
        if ag.get("reviews"):
            all_reviews.extend(ag["reviews"])
            sources.append("AskGamblers")
    except Exception as e:  # noqa: BLE001
        print(f"AskGamblers scrape failed for {casino_name}: {e}")

    try:
        tp = TrustpilotScraper(timeout=30).scrape_casino_reviews(casino_name, max_reviews=50, months=6)
        if tp.get("reviews"):
            all_reviews.extend(tp["reviews"])
            sources.append("Trustpilot")
    except Exception as e:  # noqa: BLE001
        print(f"Trustpilot scrape failed for {casino_name}: {e}")

    if not all_reviews:
        return {}
    return {"casino_name": casino_name, "reviews": all_reviews,
            "total_count": len(all_reviews), "source_str": " and ".join(sources)}


def build_player_feedback_block(feedback: Dict, max_reviews: int = 30) -> str:
    """Raw review digest for the dossier - deliberately not pre-summarized (see
    module docstring). The generating call gets the actual reviews and the hard
    rules for handling them (platform names, generalizing, weighting), and forms
    its own synthesis rather than reading a synthesis of a synthesis."""
    if not feedback:
        return "PLAYER FEEDBACK: none found/scraped for this casino."

    lines = []
    for rev in feedback["reviews"][:max_reviews]:
        rating = rev.get("rating", "N/A")
        text = (rev.get("text") or rev.get("title") or "").strip()
        if not text:
            continue
        if len(text) > 500:
            text = text[:500].rsplit(" ", 1)[0] + "..."
        lines.append(f"[{rating}] {text}")

    if not lines:
        return "PLAYER FEEDBACK: none found/scraped for this casino."

    return (
        f"PLAYER FEEDBACK ({feedback['total_count']} reviews found; showing "
        f"{len(lines)}). Never name the platform these came from - say 'player "
        f"reviews' or 'player feedback'. Never single out one reviewer ('one player "
        f"said') - generalize ('players report', 'feedback suggests'). Weigh "
        f"positives more heavily; a negative review is disproportionately likely to "
        f"come from someone who just lost money, not a real pattern. If a specific "
        f"complaint or praise recurs across multiple reviews, that pattern is worth "
        f"stating; a one-off is not.\n" + "\n".join(lines)
    )
