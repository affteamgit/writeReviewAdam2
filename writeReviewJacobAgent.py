#!/usr/bin/env python3
"""
writeReviewJacobAgent.py - single-Opus-call BCK review generator, Q&A format.

Same architecture as writeReviewAdam2's writeReviewAgent.py, ported to a genuinely
different content shape and two extra live data sources. See casino_data.py's module
docstring for the data-layer rationale. This file is the prompt-assembly and
generation layer.

QUESTION SLOTS, not narrative sections:
Content is `## Q: ...` pairs. A fixed core bank of questions gets asked (nearly)
every time, plus a dynamic, comment-driven long tail with no fixed form. Each core
slot is marked LOCKED or FREE (see QUESTION_SLOTS below) - a decision Claude made and
Goran delegated explicitly (2026-09-16): a locked slot's literal question text is
kept verbatim because it plausibly matches a real, high-intent search query ("is
casino X legit", "casino X VPN", "casino X no deposit bonus code") and varying it
risks losing that match; a free slot's wording can vary because it reads as editorial
framing, not a keyword target. Only the WORDING is locked for locked slots - the
underlying judgment/criteria for what goes in the answer is never locked for any slot.

WHY THE OLD VARIABILITY APPROACH DIDN'T WORK, confirmed reading BaseGuidelinesClaude.txt
directly: it relies entirely on written instructions with no grounding mechanism -
"never use the same sentence structure... for the same question across different
casinos" and a hand-maintained "BANNED OPENER PATTERNS" list, with nothing showing the
model what it actually wrote last time. That's the identical failure diagnosed and
fixed on Adam2 (a prompt can describe a constraint but the model has nothing to check
itself against), just never diagnosed here before. The reflection mechanism below
(collect_signatures/format_reflection, ported from Adam2's third and final iteration -
see writeReviewAdam2's memory for why the first two attempts, a ban list and
random.choice(), were both rejected) replaces that whole category of unfounded
self-monitoring instruction. The BANNED OPENER PATTERNS list and the bare
"never repeat across reviews" instructions are deliberately NOT ported for this
reason; the genuinely load-bearing formatting/terminology rules ARE.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
import casino_data as cd
from casino_data import cell, count_list, to_int, CasinoDB, COL

MODEL = "claude-fable-5-1"
# Switched from claude-opus-5 to Fable 5.1 for review writing (2026-09-18, Goran's
# call, made after seeing the real cost delta: Fable runs ~2x Opus per review here -
# $10/$50 vs $5/$25 per MTok in/out, plus a pricier 1h cache write ($20 vs $10/MTok)
# only partly offset by a cheaper cache-read rate ($0.25 vs $0.50/MTok). No quality
# comparison against Opus has been run yet on this specific voice-matching task.
# cost_of() below was updated to Fable's rates in the same change - it is NOT
# model-aware, it just hardcodes whichever model MODEL currently points to, so if
# MODEL changes again, cost_of() needs a matching edit or every displayed cost will
# be silently wrong (same bug class just found and fixed in the Opus cache-write
# rate right before this switch, in this file and writeReviewAgent.py).


# ----------------------------------------------------------------------------
# QUESTION SLOTS
# ----------------------------------------------------------------------------
# Ported from templates/StructureTemplate{General,Payments,Games,Responsible,FAQ}.txt.
# "guidance" carries the judgment/criteria from those templates nearly verbatim - that
# content is genuinely hard-won (e.g. the VPN answer's "terms don't mention it" vs
# "explicitly allows it" distinction, the highroller 4-criteria assessment) and none of
# it is formulaic cruft to discard, unlike Adam2's old rigid IF/THEN rubrics. What
# changed in translation: every template originally phrased these as a script to
# execute ("SCENARIO A: ...", "check X, then Y, then Z"); here they're calibration
# data the model consults and applies judgment to, matching how Adam2's CRITERIA table
# works - the model decides tone and exact wording, the guidance just keeps verdicts
# consistent site-wide.

QUESTION_SLOTS = [
    {
        "key": "legit",
        "header": "Is {casino} legit?",
        "locked": True,
        "always_ask": True,
        "guidance": (
            "Applies to every casino regardless of age. You may draw on your own "
            "general knowledge of this casino's public reputation, licensing history, "
            "and any known controversies for this question ONLY - this is a "
            "deliberate exception to the 'only use the data below' rule, because "
            "reputation is exactly the kind of thing a real reviewer would know "
            "independently. Ownership, licensing entities, and controversy status can "
            "change after your training cutoff - use web search to check anything "
            "time-sensitive before stating it, rather than relying on memory alone. Be "
            "honest if you don't have reliable information; never fabricate a "
            "controversy or a clean record you're not sure of. 2-4 short "
            "sentences across at most 2 paragraphs."
        ),
    },
    {
        "key": "standout",
        "header": "What makes {casino} stand out from other casinos?",
        "locked": False,
        "always_ask": False,
        "guidance": (
            "Only answer if manual comments describe a genuinely unique PRODUCT, TOOL "
            "or FEATURE competitors typically lack (a poker room, live streams, a "
            "customizable homepage, unusual support channels). Standard data alone "
            "(game counts, provider lists, crypto support) is never a standout feature "
            "- skip this question if that's all you have. VIP programs, loyalty tiers, "
            "rakeback, cashback, tournaments, gamification and any ongoing reward "
            "mechanic belong exclusively in the ongoing-promotions slot, never here, "
            "even if a comment calls one of them a 'feature'. 2-4 sentences, specific "
            "and concrete - name the actual product, don't just say 'great features'."
        ),
    },
    {
        "key": "welcome_bonus",
        "header": None,  # one of 3 mutually exclusive headers, chosen by which applies
        "locked": True,  # each of the 3 possible headers below is itself locked
        "always_ask": True,
        "guidance": (
            "Exactly one of these three applies, based on the bonus data:\n"
            "  A) Has a no-deposit bonus -> header 'Is there a no deposit bonus code "
            "for {casino}?'. About the no-deposit offer ONLY - never mention deposit "
            "bonuses here. If a bonus code exists, include it prominently; if none is "
            "required, say so. Cover amount/spins, code, wagering, max cashout, time "
            "limit. 2-3 sentences.\n"
            "  B) Has welcome/deposit bonuses but no no-deposit bonus -> header 'How do "
            "{casino}'s welcome bonuses compare to other sites?'. Cover percentages, "
            "max amounts, free spins, wagering, min deposit, max cashout; compare "
            "against THE FIELD - are the terms competitive? 3-5 sentences.\n"
            "  C) No welcome bonuses at all -> header 'Are there any bonuses in "
            "{casino}?'. If comments mention promotions/loyalty/other rewards, frame "
            "positively around those; if truly none, say so and recommend 1-2 "
            "alternatives from THE FIELD. 2-3 sentences."
        ),
        "header_options": [
            "Is there a no deposit bonus code for {casino}?",
            "How do {casino}'s welcome bonuses compare to other sites?",
            "Are there any bonuses in {casino}?",
        ],
    },
    {
        "key": "promotions",
        "header": "Are there any ongoing promotions for returning players?",
        "locked": False,
        "always_ask": False,
        "guidance": (
            "The PRIMARY home for VIP programs, loyalty systems, rakeback, cashback, "
            "tournaments, races, gamification, token systems, and every ongoing reward "
            "mechanic - if any of these appear in the standout answer instead, that's "
            "an error. Only describe what the data explicitly provides; never state an "
            "absence ('doesn't mention tournaments') - if nothing exists here, skip the "
            "slot or say bonuses focus on the welcome package and recommend 1-2 "
            "alternatives from THE FIELD for ongoing rewards. Note whether a VIP/loyalty "
            "program is open to everyone or invitation-only - that matters to the "
            "reader. 2-4 sentences depending on how much there is to cover."
        ),
    },
    {
        "key": "how_many_games",
        "header": "How many games are in {casino}?",
        "locked": False,
        "always_ask": True,
        "guidance": (
            ">7,000 a lot | 5,000-7,000 decent | 3,000-5,000 on the smaller side | "
            "below 3,000 genuinely low, worth flagging plainly - do not call it "
            "'average'. If under 5,000, recommend an alternative from THE FIELD with "
            "more than 7,000. If 7,000+, a comparison to a smaller casino can show the "
            "gap. Short: state the fact, comment, compare if useful. No filler."
        ),
    },
    {
        "key": "providers",
        "header": "Can I find games from most popular game providers?",
        "locked": False,
        "always_ask": True,
        "guidance": (
            "State the provider count: >70 a lot | <40 not a lot | between average. "
            "Then check the 13 major studios (always write 'Evolution', never "
            "'Evolution Gaming'): 0 missing = exceptional coverage, don't list them; "
            "1-2 missing = name them, still strong overall; 3-4 missing = name 2-3, "
            "could do better; 5+ missing = name 2-3, casino needs to catch up. HARD "
            "LIMIT: never name more than 5 provider names total in this answer "
            "(present + missing combined) - focus on what's missing, not what's "
            "present. Evolution and Pragmatic Play both present is a real signal for "
            "live-game quality; both missing is a real gap - work that in wherever it "
            "fits naturally, it doesn't need to be its own sentence every time. NEVER "
            "state the tracked total or a fraction of it to the reader ('12 of the 13 "
            "major studios', 'I check for 13 studios') - that exposes the methodology. "
            "Describe coverage in the "
            "reader's terms instead: 'nearly every major studio', 'all the big names "
            "are here', or name the 1-2 specific gaps."
        ),
    },
    {
        "key": "originals",
        "header": "Does {casino} have Originals or provably fair games?",
        "locked": False,
        "always_ask": True,
        "guidance": (
            "Always call in-house titles 'Originals', never 'in-house games'. Short "
            "and factual only, no explanation of what these are or why they matter: "
            "(1) Originals - yes/no, count if available; (2) provably fair - yes/no, "
            "name the actual providers if present (do not guess - only ones that "
            "genuinely appear in this casino's provider list); (3) if missing either, "
            "recommend 1-2 alternatives from THE FIELD. 2-3 sentences max."
        ),
    },
    {
        "key": "game_filters",
        "header": "Can I search for games based on RTP or volatility?",
        "locked": False,
        "always_ask": False,
        "guidance": (
            "Only answer if the casino HAS this filter (works or broken) - if it "
            "simply doesn't have RTP/volatility filtering and nothing is broken, skip "
            "this slot entirely, don't mention the absence. If present and working, "
            "frame as a genuine positive for informed play. If present but broken, "
            "explain the problem and recommend an alternative from THE FIELD with "
            "working filters."
        ),
    },
    {
        "key": "withdrawal_time",
        "header": "How long can withdrawals take?",
        "locked": False,
        "always_ask": True,
        "guidance": (
            "This is what the casino STATES/PROMISES, not measured fact - say so. "
            "Prefer the exact figure from the data over a vague bucket; only fall back "
            "to a bucket phrase (under 12h: 'a matter of hours'; 12-24h: 'less than a "
            "day'; 24-72h: 'up to a few days'; 72h+: 'more than three days') when no "
            "exact figure exists. If the terms use deliberately vague language with no "
            "number, say so plainly - that's harder to dispute, not a neutral fact. "
            "A vague or absent timeframe has no floor at exactly the moment a cashout "
            "gets pulled for manual review - that's when it actually costs the player "
            "something. Bring that in only when it's genuinely the relevant point, in "
            "your own words. Never compare to another casino or discuss player feedback here - "
            "feedback about withdrawals has its own slot."
        ),
    },
    {
        "key": "withdrawal_restrictions",
        "header": "Are there any withdrawal restrictions I should know about?",
        "locked": False,
        "always_ask": True,
        "guidance": (
            "Non-monetary restrictions only - KYC and AML wagering, not limits (covered "
            "elsewhere). KYC: what triggers it, whether a review-time is stated (use "
            "the exact figure, distinguish 'time for you to submit docs' from 'time for "
            "the casino to review them' - only the latter matters here); 0 hours/no KYC "
            "is a genuine positive, say so. AML: 1x is standard, don't mention it; "
            "above 1x, state the multiplier clearly and that 1x is standard, recommend "
            "an alternative from THE FIELD with 1x. If genuinely nothing restrictive "
            "applies, say so briefly as a positive. 2-4 sentences."
        ),
    },
    {
        "key": "withdrawal_limits",
        "header": "What are the withdrawal limits?",
        "locked": False,
        "always_ask": True,
        "guidance": (
            "List day/week/month limits that exist. Daily >$3,000 good, <$2,000 not so "
            "good; weekly >$12,000 good, <$8,000 not so good; monthly >$24,000 good, "
            "<$16,000 not so good. Convert BTC to USD internally for comparison only - "
            "never state the USD figure in the answer, only the BTC amount. Brief "
            "verdict on player-friendliness, no filler about why limits matter."
        ),
    },
    {
        "key": "bonus_withdrawal_terms",
        "header": "Do any bonus terms affect the withdrawals?",
        "locked": False,
        "always_ask": False,
        "guidance": (
            "Only answer if at least one bonus specifies Max Cashout, Max Win, "
            "wagering requirement, or wagering time - skip entirely if none do. Name "
            "each affected bonus specifically, never 'the bonuses' generically. "
            "Wagering: 20x or lower good, 25-35x average, 35x+ high. Wagering time: "
            "7 days or less tight, 14 days average, 30 days comfortable. Cover each "
            "restricted bonus separately if more than one."
        ),
    },
    {
        "key": "altcoins",
        "header": "Can I deposit different altcoins?",
        "locked": False,
        "always_ask": True,
        "guidance": (
            ">15 cryptos above average (may recommend a similar casino); 5-15 average "
            "(recommend a BETTER casino with more, never same-or-fewer); <5 very low, "
            "barely a crypto casino (recommend better). Then check the 12 majors "
            "(Bitcoin, Ethereum, Tether, Litecoin, Tron, Dogecoin, Binance Coin, "
            "Ripple, Bitcoin Cash, USD Coin, Cardano, Solana) only if the casino has "
            "5+ total: all 12 present = say so, name at most 5 as examples, don't list "
            "all; up to 3 missing = name what's missing, still good coverage; 3-5 "
            "missing = name 2 missing, notable gap; 5+ missing = name 2 missing, most "
            "majors absent. Never name more than 5 crypto names total in this answer. "
            "NEVER state the tracked total or a fraction of it to the reader ('10 of "
            "the 12 majors', 'all 12 present') - that exposes the methodology. "
            "Describe coverage in the reader's terms instead: 'every major coin is "
            "here', 'covers all the big names', or name the specific gaps. "
            "Mention Buy Crypto or Crypto Swap only if actually available - Swap is "
            "genuinely rare and worth flagging as such."
        ),
    },
    {
        "key": "withdrawal_feedback",
        "header": "Do players have any complaints regarding withdrawals?",
        "locked": False,
        "always_ask": False,  # only if player feedback data mentions withdrawals
        "guidance": (
            "Only include this slot if the player feedback below actually discusses "
            "withdrawals - skip entirely otherwise, do not invent or assume problems. "
            "One short paragraph synthesizing what's actually there, mostly positive "
            "or describing real issues as they're actually described. See the "
            "PLAYER FEEDBACK rules in the dossier for platform-naming/generalization "
            "requirements."
        ),
    },
    {
        "key": "self_exclusion",
        "header": "Can I self-exclude at {casino}?",
        "locked": True,
        "always_ask": True,
        "guidance": (
            "If missing: this is a genuine red flag, say so plainly - either the "
            "casino isn't operating legally on responsible gambling or doesn't care "
            "enough to offer the most basic tool. If present: confirm briefly, state "
            "whether it activates automatically or needs support (support-required is "
            "an extra step worth recommending an alternative for, from THE FIELD)."
        ),
    },
    {
        "key": "rg_tools",
        "header": "Which responsible gambling tools are available besides self-exclusion?",
        "locked": False,
        "always_ask": True,
        "guidance": (
            "Count everything besides self-exclusion, including cooling-off. 3-4 = "
            "above average, 1-2 = average, 0 = needs to step up. State whether these "
            "are self-managed or need support - self-managed is a genuine positive, "
            "support-required is a real drawback worth one comparison (at most one "
            "recommendation total for this slot, not one per shortfall). 3-4 sentences "
            "max."
        ),
    },
    {
        "key": "ownership",
        "header": "Who owns {casino}?",
        "locked": True,
        "always_ask": True,
        "guidance": (
            "State the operating company/parent from the data or REPUTATION context if "
            "available, including other brands it runs and any notable regulatory "
            "history. Operating entities and licenses are reassigned or restructured "
            "over time - use web search to confirm the CURRENT operator before naming "
            "one from memory; a name that was correct a year ago may not be now. If "
            "ownership isn't transparent or is just a generic shell with no context, "
            "that absence is informative, not neutral - established operators tend to "
            "disclose this openly, so say so in your own way. 1-4 short sentences."
        ),
    },
    {
        "key": "vpn",
        "header": "Can I use a VPN to play at {casino}?",
        "locked": True,
        "always_ask": True,
        "guidance": (
            "State the restricted-country count (>40 is significant) and what the "
            "TERMS actually say about VPN use, not just the data label. If the terms "
            "explicitly prohibit VPN/IP-masking, say so and warn about consequences "
            "(account closure, confiscated funds), recommend a VPN-friendly "
            "alternative. If terms mention detecting masking without an outright ban, "
            "flag the risk. If terms say NOTHING about VPN, the honest statement is "
            "that the terms don't mention any restriction - NOT that VPN use is "
            "'explicitly allowed' or that the casino is 'VPN-friendly'. A general "
            "'VPN friendly: yes' data label does not by itself mean the terms say so "
            "explicitly; the distinction between 'not restricted' and 'explicitly "
            "allowed' matters and must be preserved. No generic benefit sentences "
            "about privacy or travel. 2-3 sentences."
        ),
    },
    {
        "key": "anonymity",
        "header": "Can I stay anonymous at {casino}?",
        "locked": True,
        "always_ask": True,
        "guidance": (
            "Covers the FULL player journey - signup, deposit, play, AND withdraw, not "
            "just registration. If genuinely anonymous (email-only signup, no "
            "mandatory KYC up to a threshold), say so and name the threshold if the "
            "terms state one; if the terms only have the standard 'we reserve the "
            "right to require KYC' boilerplate, ignore it; if no threshold is stated "
            "at all beyond that boilerplate, the honest picture is that KYC has no "
            "floor - make sure the reader understands that risk, in your own words. "
            "If not anonymous, be specific about what's required and at which "
            "stage, and recommend 1-2 anonymous alternatives from THE FIELD."
        ),
    },
    {
        "key": "support",
        "header": "Is {casino}'s support any good?",
        "locked": False,
        "always_ask": True,
        "guidance": (
            "24/7 live chat is the baseline, not something to celebrate - mention it "
            "briefly if present. If NOT 24/7, warn the reader and recommend an "
            "alternative from THE FIELD with proper 24/7 support. Mention other "
            "channels (email, Telegram, social) briefly if they exist. 2-3 sentences."
        ),
    },
    {
        "key": "highrollers",
        "header": "Is {casino} good for highrollers?",
        "locked": True,
        "always_ask": True,
        "guidance": (
            "Assess all four that have data: (1) limits - daily >$10,000, weekly "
            ">$50,000, monthly >$100,000 highroller-friendly, no crypto cap is a major "
            "plus; (2) payout speed - a specific stated timeframe is accountable, "
            "vague language is a concern; (3) large-win restrictions - payout caps, "
            "AML above 1x disproportionately hurts big depositors (3x on a $50,000 "
            "deposit is $150,000 of required wagering, say so if relevant); (4) track "
            "record from player feedback if available, specifically on large "
            "withdrawals - skip this one criterion if no feedback exists, don't "
            "mention its absence. Give a clear verdict either way, citing the actual "
            "numbers. If not suitable, name the dealbreakers and recommend "
            "alternatives from THE FIELD. 3-6 sentences, direct, numbers not fluff."
        ),
    },
    {
        "key": "player_sentiment",
        "header": "What do players say about {casino}?",
        "locked": False,
        "always_ask": False,  # only if player feedback was found
        "guidance": (
            "Synthesize the PLAYER FEEDBACK block in the dossier directly - specific "
            "details (what's praised, what's complained about), not vague summary "
            "phrases. Add your own reaction: does it match what the data suggests, "
            "does anything surprise you. 70-120 words, 2-3 short paragraphs (positives "
            "then concerns). See the PLAYER FEEDBACK rules for platform-naming and "
            "generalization requirements - they apply here in full."
        ),
    },
]

LOCKED_HEADERS = {
    slot["key"]: slot["header"] for slot in QUESTION_SLOTS
    if slot.get("locked") and slot.get("header")
}

# Top-level structure the legacy pipeline already uses (confirmed via parse_qa_blocks'
# "section" block kind): **Section Name** headers, ## Q: pairs nested under each - not
# a flat Q&A list. No top-level Bonuses; bonus content lives under General/Payments.
SECTIONS = ["General", "Payments", "Games", "Responsible Gambling", "FAQ"]

SECTION_FOR_SLOT = {
    "legit": "General", "standout": "General", "welcome_bonus": "General",
    "promotions": "General",
    "withdrawal_time": "Payments", "withdrawal_restrictions": "Payments",
    "withdrawal_limits": "Payments", "bonus_withdrawal_terms": "Payments",
    "altcoins": "Payments", "withdrawal_feedback": "Payments",
    "how_many_games": "Games", "providers": "Games", "originals": "Games",
    "game_filters": "Games",
    "self_exclusion": "Responsible Gambling", "rg_tools": "Responsible Gambling",
    "ownership": "FAQ", "vpn": "FAQ", "anonymity": "FAQ", "support": "FAQ",
    "highrollers": "FAQ", "player_sentiment": "FAQ",
}


# ----------------------------------------------------------------------------
# VOICE
# ----------------------------------------------------------------------------
# Ported from templates/BaseGuidelinesClaude.txt, which turned out to already contain
# real, specific, hard-won editorial rules - not discarded, kept below almost
# verbatim (banned words, "Originals" not "in-house", "Evolution" not "Evolution
# Gaming", the 2-sentence mobile-paragraph rule, provider/crypto naming caps, comma
# formatting). Two categories were deliberately NOT ported, because they are the
# exact mechanism this whole migration replaces:
#   1. The "BANNED OPENER PATTERNS" list ("When I checked...", "I immediately
#      noticed..."). Same ceiling as every literal ban list Adam2 tried and outgrew -
#      a growing blocklist chases symptoms one at a time forever.
#   2. Bare cross-review self-monitoring instructions ("never use the same sentence
#      structure... for the same question across different casinos", "vary
#      comparison openers... never reuse a phrase in the entire review"). These ask
#      the model to remember and avoid its own past output with nothing to check
#      against - no prior review is ever shown for comparison in the legacy
#      pipeline. That gap is exactly what the reflection mechanism below closes.
# Jakob's actual voice/register (contraction rate, sentence rhythm, real personality
# texture) still needs measuring against genuine Jakob-written samples once Goran
# provides them, the same way Adam's was - this VOICE constant carries the
# structural/formatting rules that don't need a sample to get right, not a
# substitute for that measurement.

VOICE = """\
You are Jakob, a crypto casino reviewer for BCK.

Jakob is subjective, UX-focused, and opinionated - he cares about the player \
experience, not raw data. He shares personal impressions directly ("I was \
disappointed to see...", "I really enjoyed..."). He is not a data analyst: avoid \
stats and percentages, express what a feature means for the player in practice. If \
something is bad, say it's bad. If something is good, show genuine enthusiasm.

Hard rules:
- First-person singular ("I"). Address the reader as "you" - never "players" or \
"users". Never address the reader at the start of an answer.
- 2-3 sentences per answer as a default; only go longer if the question genuinely \
needs it.
- HARD paragraph rule: never more than 2 sentences per paragraph, no exceptions. A \
4-sentence answer is 2 paragraphs; a 6-sentence answer is 3. This is for mobile \
readers - long unbroken blocks are unreadable on a phone.
- No heading/title beyond the question itself - the question IS the header.
- Never pad with "why this matters to you" filler. State the fact, comment briefly, \
compare if useful, stop.
- Numbers always get thousands commas: $1,500 not $1500; $100,000 not $100000. \
Applies to USD, BTC sub-values, game counts, anything over 999.
- Bold (using **text**) the feature or figure itself, never the whole sentence, \
wherever you mention: VPN terms, anonymous registration, year established, years of \
experience, restricted-country count, number of cryptocurrencies, withdrawal limit \
values, withdrawal/KYC processing time, game count, provider count, provably fair \
games, Originals, game filters, minimum deposit amounts.
- Never write a markdown link yourself. A separate verified pass adds links \
afterward. Name casinos in plain text.
- No em dash, no en dash. No hyphen used as a clause connector - hyphens are only for \
compound words ("email-only") or number ranges ("24-48 hours").
- Never say "in-house games" - say "Originals". Never say "Evolution Gaming" - say \
"Evolution".
- Never name more than 5 game providers, and never more than 5 cryptocurrencies, in \
a single answer - present and missing combined. Focus on what's missing, not what's \
present.
- Never mention broken images, broken links, or pop-ups unless the casino actually \
has them.
- Never mention a rarity statistic ("only 31% of crypto casinos offer this"). If \
something is rare, say so in your own words.
- Never expose the criteria or thresholds you're applying to the reader.
- Never claim personal testing you have no record of - see the PLAYER FEEDBACK and \
PREVIOUSLY PUBLISHED REVIEW sections in the dossier for what you actually know versus \
what you're inferring from the data.
- Comparisons: vary the structure genuinely (direct contrast, recommendation, \
context/benchmark, "for players who need X, Y is a better fit") rather than reaching \
for "In comparison"/"For example" as a default. Always attach the compared casino's \
actual value from THE FIELD, and link the name as [CasinoName](placeholder) - a \
separate pass resolves the real URL. When comparing crypto counts, phrase the \
compared casino's count as "more than N".
- Banned words/phrases, no exceptions: "Trustpilot", "AskGamblers", any review \
platform name (say "player reviews" or "player feedback"); "platform" for a casino \
(say "casino" or "site"); "one reviewer"/"one player noted" (say "some players \
report"); "no other casino"/"the only casino"/"you won't find anywhere else"; \
"verify fairness"/"cryptographic methods"/"mathematical proof"; "the data doesn't \
specify"/"I don't have"/"no data available" as a cop-out (say what IS known, or that \
the casino doesn't publish the information); "solid" (use a specific alternative: \
"reliable", "clean", "impressive", "respectable", or describe what makes it good); \
"comprehensive gambling/gaming/entertainment ecosystem".
- Occasionally give one genuine, freshly-worded compliment per answer where it's \
earned - never more than one per section, never reused verbatim within a review.

You are writing for readers about to spend money. Be useful or be quiet."""

VOICE_SAMPLES_FILE = "examples_jakob.txt"


def load_voice_samples() -> Optional[str]:
    """Real Jakob-written passages as few-shot voice anchors - see writeReviewAgent.py's
    equivalent for why this matters more than any written rule above: a prompt can
    describe a register but not carry it. Not yet populated; Goran is providing
    Jakob's real writing (short and long, posts and reviews) to measure against, the
    same way Adam's samples were measured before being wired in here.
    """
    path = Path(VOICE_SAMPLES_FILE)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    return (
        "YOUR OWN PUBLISHED WRITING - VOICE REFERENCE\n\n"
        "Everything below this line you wrote by hand. Match its rhythm, its "
        "contractions, its directness - not its facts, not its phrases (those "
        "belong to a different casino and a different date), and not any dash "
        "punctuation it contains (house style bans dashes outright now).\n\n"
        "----- BEGIN YOUR WRITING -----\n"
        f"{text}\n"
        "----- END YOUR WRITING -----"
    )


# ----------------------------------------------------------------------------
# REFLECTION - grouped by QUESTION SLOT, not by review
# ----------------------------------------------------------------------------
# Direct port of Adam2's third and final anti-repetition design (see
# writeReviewAgent.py's collect_signatures/format_reflection and the project memory
# for why the first two - a literal ban list, then random.choice() over a small
# style menu - were both tried and rejected). The translation from narrative
# sections to question slots is mechanical: instead of grouping "how General opened/
# closed" across the last N reviews, group "how you answered THIS question" across
# the last N reviews that asked it. Locked-header slots only ever show/reflect on
# ANSWER wording (the header text is fixed, nothing to vary there); free-header slots
# reflect on both the header phrasing and the answer.

_QA_PATTERN = re.compile(r"^##\s*Q:\s*(.+?)\s*$\n+(.*?)(?=\n##\s*Q:|\Z)",
                        re.MULTILINE | re.DOTALL)


def parse_qa_pairs(text: str) -> List[Tuple[str, str]]:
    """(question header, answer body) pairs from a rendered review's markdown."""
    return [(h.strip(), b.strip()) for h, b in _QA_PATTERN.findall(text)]


def _slot_key_for_header(header: str) -> Optional[str]:
    """Match a rendered header back to its QUESTION_SLOTS key, for locked headers
    (exact match after casino-name substitution) and free ones (best-effort - a free
    header's wording changes each time, so this only recognizes the ones that happen
    to still resemble their template; unmatched free headers are still shown under
    the dynamic long-tail).
    """
    norm = re.sub(r"\s+", " ", header.strip().lower())
    for slot in QUESTION_SLOTS:
        candidates = slot.get("header_options") or ([slot["header"]] if slot.get("header") else [])
        for tmpl in candidates:
            pattern = re.escape(tmpl).replace(re.escape("{casino}"), r".+?")
            if re.fullmatch(pattern, norm, re.IGNORECASE):
                return slot["key"]
    return None


def collect_qa_signatures(history: List[Tuple[str, str]]) -> Dict[str, list]:
    """Structured extraction: per-slot (title, header, answer) across the window,
    plus recurring phrases and comparison casinos - same computed-not-hand-picked
    approach as Adam2's recurring_phrases(), reused via its normalization logic
    where it doesn't need the narrative-section assumptions baked in.

    Full answers, not truncated - confirmed empirically (2026-09-18) that a [:300]/
    [:200] cutoff here silently hid exactly the repetition it existed to catch: a
    comparison clause near the end of an answer (where "for bigger stakes, X commits
    to..." style sentences tend to land) fell past the cutoff in every highroller
    answer checked, so the model had never actually seen its own prior wording for
    the part that was repeating verbatim. The fix is showing it the real material,
    not adding another instruction telling it what not to write.
    """
    by_slot: Dict[str, list] = {}
    long_tail: List[Tuple[str, str, str]] = []

    for title, text in history:
        for header, answer in parse_qa_pairs(text):
            key = _slot_key_for_header(header)
            if key:
                by_slot.setdefault(key, []).append((title, header, answer))
            else:
                long_tail.append((title, header, answer))

    return {"by_slot": by_slot, "long_tail": long_tail[-15:]}


def format_qa_reflection(sig: Dict[str, list]) -> str:
    """The 'reread your own last few reviews' block, grouped by question slot."""
    by_slot, long_tail = sig["by_slot"], sig["long_tail"]
    if not by_slot and not long_tail:
        return ""

    out = [
        "A LOOK AT YOUR OWN LAST FEW REVIEWS - read this the way you'd reread your "
        "own recent drafts before starting a new one, not as a list of rules. Below "
        "is how you've answered each recurring question, grouped by question so a "
        "reused sentence is obvious at a glance.\n"
        "Every fact and number below belongs to THAT casino on THAT date, not the "
        "casino you're writing about now - a wagering multiplier, a payout time, a "
        "cap, a country count, all of it is specific to the bracketed title it's "
        "filed under. Never state one of these as if it were true of the current "
        "casino, and never use another casino's name here unless THE FIELD in this "
        "review's own dossier confirms that fact for that casino - if you want a "
        "real comparison, pull the comparison casino's actual figure from THE FIELD, "
        "not from what it said in an old review.\n"
        "The TOPIC each question covers is fixed and does not need to change. What "
        "matters is whether you're reaching for the same SENTENCE to answer it. If "
        "two entries under the same question are basically the same words in the "
        "same order, answer it a genuinely different way this time - different "
        "opening move, different angle into the fact, different rhythm. Some overlap "
        "is just the facts having the same shape (a 24/7-live-chat answer will always "
        "say it's the baseline, there's only so many ways to say that), so use "
        "judgment: is this wording load-bearing, or a groove you're stuck in?",
    ]

    for slot in QUESTION_SLOTS:
        entries = by_slot.get(slot["key"])
        if not entries:
            continue
        out.append(f"\n  \"{slot['header'] or slot['key']}\":")
        out += [f'    [{t}] Q: "{h}" A: "{a}"' for t, h, a in entries]

    if long_tail:
        out.append(
            "\n  Recent dynamic/comment-driven Q&As (both the QUESTION WORDING and "
            "the answer are free to vary here - these have no fixed form to begin "
            "with, but the same 'don't reuse a construction out of habit' judgment "
            "applies):"
        )
        out += [f'    [{t}] Q: "{h}" A: "{a}"' for t, h, a in long_tail]

    return "\n".join(out)


# ----------------------------------------------------------------------------
# OUTPUT SPEC + TASK
# ----------------------------------------------------------------------------

def _render_slot_spec(slot: Dict) -> str:
    if slot.get("header_options"):
        header_line = "one of:\n    " + "\n    ".join(f'"{h}"' for h in slot["header_options"])
        lock_note = "LOCKED - use the exact wording shown for whichever scenario applies."
    elif slot["locked"]:
        header_line = f'"{slot["header"]}"'
        lock_note = "LOCKED - use this exact wording, casino name substituted."
    else:
        header_line = f'(topic: {slot["header"]} - wording is yours)'
        lock_note = "FREE - the topic must be covered when it applies; phrase the question yourself."

    ask_note = "Always ask" if slot["always_ask"] else "Conditional - see guidance, skip if it doesn't apply"
    return (
        f'- [{slot["key"]}] {ask_note}. Header: {header_line} ({lock_note})\n'
        f'  {slot["guidance"]}'
    )


def build_output_spec(keyword: str, casino: str) -> str:
    by_section: Dict[str, List[Dict]] = {s: [] for s in SECTIONS}
    for slot in QUESTION_SLOTS:
        by_section[SECTION_FOR_SLOT[slot["key"]]].append(slot)

    parts = [
        "OUTPUT FORMAT - follow exactly.\n",
        "Plain markdown, nothing before the first header, no commentary about your work.\n",
        "# {casino} review\n",
        f'The exact phrase "{keyword}" must appear verbatim somewhere in this review, '
        "reading naturally - anywhere is fine, it does not need its own answer.\n",
    ]
    for section in SECTIONS:
        parts.append(f"\n**{section}**\n")
        parts.append(
            "Each question below becomes a '## Q: <header>' line followed by its "
            "answer. Skip any conditional question that doesn't apply - do not "
            "include an empty or forced answer. After the listed questions, add any "
            "genuinely new Q&A the manual comments or player feedback below call "
            "for and don't already fit an existing question (see TASK)."
        )
        for slot in by_section[section]:
            parts.append(_render_slot_spec(slot))

    parts.append(
        "\nFormatting: **bold** for the specific rules in VOICE, \"- \" for bullet "
        "lists (only when 2+ items), no markdown links (a separate pass adds them), "
        "no heading level other than the '## Q:' question headers themselves."
    )
    # Substituted here, not left for the caller: build_output_spec's own header text
    # (e.g. "Is {casino} legit?") is the one place in the whole assembled prompt that
    # needs {casino} resolved, so doing it inline removes a step a future caller could
    # forget - this was a real bug caught in dry-run testing, where a first version
    # shipped the literal string "{casino}" straight into the prompt unresolved.
    return "\n".join(parts).replace("{casino}", casino)


TASK = """\
Write the review.

Before you write, think it through:

1. Read the full dossier - casino data, bonus data, manual comments, player
   feedback, and the previously published review if one exists. Form a real sense
   of this specific casino before touching any question.
2. Work through the question slots in OUTPUT FORMAT in order. For each: does it
   apply (check the guidance's conditions)? If yes, write the answer using the
   guidance as calibration, not as a script to recite - the guidance tells you WHAT
   the site needs covered and how to judge it, never the sentence to write.
3. For a LOCKED question, use the exact header text given - vary the ANSWER only.
   For a FREE question, phrase the header yourself, naturally, the way a player
   would actually ask it.
4. If a previously published review is provided, look for real deltas worth stating
   - a game count, a crypto count, a limit, a feature that's new or gone since
   then. State the change plainly ("up from X", "no longer offers Y") using the
   time-since note provided. Don't force a delta that isn't there; not every
   question needs one.
5. Manual comments: check whether each one strengthens an existing answer above
   before creating a new Q&A for it. Only create a new one if it's genuinely
   unrelated to every existing question and has enough substance to justify its own
   answer. A new question must be broad enough a player would ask it before
   knowing the feature exists - never name the specific feature in the question
   itself, only in the answer.
6. Player feedback, if present: only answer the withdrawal-feedback and player-
   sentiment questions if the feedback actually covers those topics. Follow the
   platform-naming and generalization rules in the dossier's PLAYER FEEDBACK block
   exactly.
7. Read the reflection section below, if there is one, the way you'd reread your
   own last few drafts. It's grouped by question so a repeated sentence is visible
   at a glance. Nothing in it is forbidden - use judgment about what's a genuine
   habit worth breaking versus what's just how the facts have to be stated.
8. Never invent a feature, number, or name not in the dossier. Every fact traces to
   the data below.
9. If a slot's guidance tells you to verify something with web search (legit,
   ownership), search silently and state the verified fact directly in your normal
   voice - never narrate the search itself ("I'll look this up", "Based on my
   search", "According to [source]"). The reader sees your conclusion, not your
   process.

Two failure modes to avoid, in tension with each other:
  - The checklist: every answer a flat recitation of the guidance, opinion reduced
    to a stock reaction. This is what the migration is trying to escape.
  - Trying too hard: meta-commentary, addressing anyone but the reader, forced
    personality instead of an actual view. Worse than dry.

Output only the review."""


# ----------------------------------------------------------------------------
# ASSEMBLY
# ----------------------------------------------------------------------------

def assemble(
    db: CasinoDB, row: List[str], keyword: str, history: List[Tuple[str, str]],
    focus_name: str, evolution_context: str = "", feedback_block: str = "",
) -> Tuple[list, str, Dict[str, list]]:
    """Returns (system_blocks, user_text, signatures) - same cache-boundary logic as
    Adam2's assemble(): stable content (voice, samples, the whole field) in `system`
    behind a cache breakpoint, everything per-review (dossier, Evolution context,
    player feedback, rolling history) in the volatile user message.
    """
    system_blocks = [{"type": "text", "text": VOICE}]
    samples = load_voice_samples()
    if samples:
        system_blocks.append({"type": "text", "text": samples})
    else:
        print(f"NOTE: no voice samples found at {VOICE_SAMPLES_FILE} - running on "
              "the written rules alone, which measurably weakens the voice.",
              file=sys.stderr)

    system_blocks.append({
        "type": "text",
        "text": cd.build_landscape(db, focus_name),
        "cache_control": {"type": "ephemeral", "ttl": "1h"},
    })

    parts = [
        cd.build_dossier(db, row),
        "",
        cd.build_bonuses(db, focus_name),
        "",
        cd.build_comments(db, focus_name),
        "",
    ]
    if evolution_context:
        parts += [evolution_context, ""]
    if feedback_block:
        parts += [feedback_block, ""]

    sig: Dict[str, list] = {"by_slot": {}, "long_tail": []}
    if history:
        parts.append(
            f"THE LAST {len(history)} REVIEWS PUBLISHED (newest first). Read them for "
            "rhythm and habits, then write this one with real variability from them.\n"
        )
        for title, text in history:
            parts.append(f"----- BEGIN PRIOR REVIEW: {title} -----\n{text}\n----- END -----\n")
        sig = collect_qa_signatures(history)
        reflection = format_qa_reflection(sig)
        if reflection:
            parts.append(reflection)
        parts.append("")
    else:
        parts.append("(No prior reviews available for comparison this run.)\n")

    parts.append(build_output_spec(keyword, focus_name))
    parts.append("")
    parts.append(TASK)
    return system_blocks, "\n".join(parts), sig


# ----------------------------------------------------------------------------
# GENERATION - ported verbatim from writeReviewAgent.py, zero content-specific
# logic here (streaming/progress/error-handling/cost math).
# ----------------------------------------------------------------------------

def _drain_with_progress(stream, label: str = "write", progress=None):
    start = time.monotonic()
    thinking = text = 0
    last_draw = 0.0

    def draw(final: bool = False) -> None:
        elapsed = time.monotonic() - start
        phase = "writing" if text else "thinking"
        if progress is not None:
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


def _sum_usages(usages: list):
    """Combine per-turn usage across a pause_turn continuation loop into one object
    with the same attributes cost_of() reads - real cost is the sum across every
    turn, not just the last one."""
    import types
    fields = ("input_tokens", "output_tokens", "cache_read_input_tokens",
              "cache_creation_input_tokens")
    searches = sum(
        getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0
        for u in usages
    )
    return types.SimpleNamespace(web_search_requests=searches, **{
        f: sum(getattr(u, f, 0) or 0 for u in usages) for f in fields
    })


# Basic web search (not the dynamic-filtering 20260209+ versions - a single factual
# lookup like "who currently owns this casino" doesn't need code-execution-backed
# result filtering). max_uses caps it well under what a batch of 20 answers could
# otherwise rack up if the model got search-happy; the legit/ownership slots are the
# only ones instructed to use it. $10/1k searches - trivial next to per-review cost.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 4}


def generate(system_blocks: list, user_text: str, effort: str, max_tokens: int,
            progress=None) -> Tuple[str, object]:
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key(),
                                 timeout=1800.0, max_retries=1)
    messages = [{"role": "user", "content": user_text}]
    usages = []
    try:
        for _ in range(5):  # hard ceiling on pause_turn continuations, never expected in practice
            with client.messages.stream(
                model=MODEL,
                max_tokens=max_tokens,
                system=system_blocks,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                tools=[WEB_SEARCH_TOOL],
                messages=messages,
            ) as stream:
                message = _drain_with_progress(stream, "write", progress)
            usages.append(message.usage)
            if message.stop_reason != "pause_turn":
                break
            # A long-running search paused the turn - resend the paused assistant
            # message unchanged to continue, per the web search tool's documented
            # pause_turn protocol. Not expected for a single factual lookup capped
            # at max_uses=4, but handled rather than silently truncating output.
            messages.append({"role": "assistant", "content": message.content})
            print("  [search paused mid-turn, continuing...]", file=sys.stderr)
    except KeyboardInterrupt:
        sys.exit("\nInterrupted before the review finished - nothing was saved.")
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
        print("WARNING: hit max_tokens - output is truncated.", file=sys.stderr)

    # Always normalize to _sum_usages()'s flat shape (even for the single-turn case)
    # so cost_of() reads a consistent shape regardless of whether a pause_turn
    # continuation happened.
    usage = _sum_usages(usages)

    text = "".join(b.text for b in message.content if b.type == "text").strip()
    return text, usage


def cost_of(usage) -> float:
    """Fable 5.1 (MODEL as of 2026-09-18): $10/1M in, $50/1M out, cache read 0.025x
    ($0.25 - Fable's cache-hit multiplier is 4x cheaper than Opus's 0.1x). Cache write
    is 2x base ($20) because assemble() writes with ttl="1h", not the 5-minute default
    (1.25x/$12.50) - rates confirmed against platform.claude.com/docs pricing
    2026-09-18. NOT model-aware: hardcoded to whatever MODEL currently is, update both
    together. Web search (added 2026-09-18 for the legit/ownership slots) is $10 per
    1,000 searches on top of token costs - trivial at max_uses=4, but real."""
    if usage is None:
        return 0.0
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    c_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    c_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    searches = getattr(usage, "web_search_requests", 0) or 0
    return (inp * 10 + c_read * 0.25 + c_write * 20 + out * 50) / 1_000_000 + searches * 0.01


# ----------------------------------------------------------------------------
# PROGRAMMATIC ENTRY POINT
# ----------------------------------------------------------------------------

def generate_review(
    db: CasinoDB,
    casino: str,
    keyword: Optional[str] = None,
    history: Optional[List[Tuple[str, str]]] = None,
    effort: str = "high",
    max_tokens: int = 32000,
    fetch_evolution: bool = True,
    fetch_player_feedback: bool = True,
    progress=None,
) -> Dict[str, object]:
    """Write one review and return it plus metadata. Raises LookupError for an
    unknown casino. Mirrors Adam2's generate_review() shape so app.py can be wired
    the same way.
    """
    row = db.find(casino)
    focus = cell(row, COL["name"])
    keyword = keyword or f"{focus} Casino Review"
    history = history or []

    evolution_context, evolution_status = "", "skipped"
    if fetch_evolution:
        evolution_context, evolution_status = cd.fetch_evolution_context(
            db.casino_id(row), focus
        )

    feedback_block, feedback_count = "", 0
    if fetch_player_feedback:
        feedback = cd.scrape_player_feedback(focus)
        feedback_count = feedback.get("total_count", 0)
        feedback_block = cd.build_player_feedback_block(feedback)

    system_blocks, user_text, sig = assemble(
        db, row, keyword, history, focus, evolution_context, feedback_block
    )
    review, usage = generate(system_blocks, user_text, effort, max_tokens, progress=progress)

    data_flags = [ln for ln in review.splitlines() if ln.strip().startswith("DATA FLAG:")]
    qa_pairs = parse_qa_pairs(review)
    return {
        "casino": focus,
        "keyword": keyword,
        "review": review,
        "cost": cost_of(usage),
        "usage": usage,
        "data_flags": data_flags,
        "evolution_status": evolution_status,
        "player_feedback_count": feedback_count,
        "question_count": len(qa_pairs),
        "history_titles": [t for t, _ in history],
        "chars": len(review),
        "words": len(review.split()),
    }


def load_local_history(dirs: List[Path], focus_name: str, n: int) -> List[Tuple[str, str]]:
    """Newest n .md files across the given local dirs - the CLI's window source, ported
    from writeReviewAgent.py's identically-named function. The web app uses history.py's
    Drive-backed loader instead (see app.py); this exists only for the CLI/local testing
    path, same split Adam2 has.
    """
    slug = re.sub(r"[^a-z0-9]", "", focus_name.lower())
    candidates = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            fslug = re.sub(r"[^a-z0-9]", "", f.stem.lower())
            if slug and slug in fslug:
                continue
            candidates.append(f)
    candidates.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [(f.stem, f.read_text(encoding="utf-8", errors="replace")) for f in candidates[:n]]


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> None:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="Single-pass Jakob/BCK review writer.")
    ap.add_argument("casino", help='Brand name exactly as in the Data tab')
    ap.add_argument("--keyword", default=None)
    ap.add_argument("--history-dir", default="reviews_jakob")
    ap.add_argument("--history", type=int, default=5)
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-tokens", type=int, default=32000)
    ap.add_argument("--no-evolution", action="store_true", help="Skip the MySQL old-review lookup")
    ap.add_argument("--no-feedback", action="store_true", help="Skip live AskGamblers/Trustpilot scraping")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. export it, or use --dry-run.")

    print("Loading casino database...", file=sys.stderr)
    db = CasinoDB()
    try:
        row = db.find(args.casino)
    except LookupError as e:
        sys.exit(str(e))
    focus = cell(row, COL["name"])

    hist_dir = Path(args.history_dir)
    history = load_local_history([hist_dir], focus, args.history)
    print(f"Rolling window: {len(history)} prior review(s) "
          f"[{', '.join(t for t, _ in history) or 'none'}]", file=sys.stderr)

    if args.dry_run:
        evolution_context, status = ("", "skipped (dry-run)") if args.no_evolution else \
            cd.fetch_evolution_context(db.casino_id(row), focus)
        print(f"Evolution lookup: {status}", file=sys.stderr)
        feedback_block = "" if args.no_feedback else "(scraper not run in dry-run)"
        system_blocks, user_text, _ = assemble(
            db, row, args.keyword or f"{focus} Casino Review", history, focus,
            evolution_context, feedback_block,
        )
        out = Path(f"prompt_{focus.replace(' ', '_')}.txt")
        sys_text = "\n\n".join(b["text"] for b in system_blocks)
        out.write_text(f"===== SYSTEM =====\n{sys_text}\n\n===== USER =====\n{user_text}")
        print(f"Dry run. Prompt written to {out} "
              f"({len(sys_text) + len(user_text):,} chars)")
        return

    print(f"Writing {focus} review with {MODEL} (effort={args.effort})...", file=sys.stderr)
    result = generate_review(
        db, focus, keyword=args.keyword, history=history, effort=args.effort,
        max_tokens=args.max_tokens, fetch_evolution=not args.no_evolution,
        fetch_player_feedback=not args.no_feedback,
    )

    hist_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_path = hist_dir / f"{focus} Review (jakob {stamp}).md"
    out_path.write_text(result["review"], encoding="utf-8")

    print(f"\nWrote {out_path}", file=sys.stderr)
    print(f"  {result['chars']:,} chars / {result['words']:,} words / "
          f"{result['question_count']} questions", file=sys.stderr)
    print(f"  Evolution: {result['evolution_status']}", file=sys.stderr)
    print(f"  Player feedback: {result['player_feedback_count']} reviews found", file=sys.stderr)
    print(f"  Cost: ~${result['cost']:.3f}", file=sys.stderr)
    for flag in result["data_flags"]:
        print(f"  !! {flag}", file=sys.stderr)


if __name__ == "__main__":
    main()
