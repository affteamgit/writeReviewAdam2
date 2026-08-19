import os
import openai
import requests
import json
import streamlit as st
from google.oauth2.service_account import Credentials   
from googleapiclient.discovery import build
from anthropic import Anthropic
from pathlib import Path
import re
import random
import concurrent.futures
from typing import Dict, Tuple

# CONFIG 
OPENAI_API_KEY      = st.secrets["OPENAI_API_KEY"]
ANTHROPIC_API_KEY   = st.secrets["ANTHROPIC_API_KEY"]
COINMARKETCAP_API_KEY = st.secrets["COINMARKETCAP_API_KEY"]

SPREADSHEET_ID = st.secrets["SPREADSHEET_ID"]
SHEET_NAME     = st.secrets["SHEET_NAME"]

FOLDER_ID = st.secrets["FOLDER_ID"]
GUIDELINES_FOLDER_ID = st.secrets["GUIDELINES_FOLDER_ID"]

# Fine-tuned model for Adam's rewriting
FINE_TUNED_MODEL = "ft:gpt-3.5-turbo-1106:affiliation:adam0301:ByHlJhcR"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive"
]

DOCS_DRIVE_SCOPES = ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/drive"]

def get_service_account_credentials():
    return Credentials.from_service_account_info(st.secrets["service_account"], scopes=SCOPES)

def get_file_content_from_github(filename):
    """Get content of a file from GitHub repository."""
    try:
        github_base_url = "https://raw.githubusercontent.com/affteamgit/writeReviewAdam2/main/templates/"
        file_url = f"{github_base_url}{filename}.txt"
        
        response = requests.get(file_url)
        response.raise_for_status()
        
        return response.text
        
    except Exception as e:
        print(f"Error reading file {filename} from GitHub: {str(e)}")
        return None

def get_all_templates():
    """Fetch all templates at once with parallel processing"""
    templates = {}
    files = [
        'PromptTemplate',
        'BaseGuidelinesClaude',
        'BaseGuidelinesResponsible',
        'StructureTemplateGeneral',
        'StructureTemplatePayments', 
        'StructureTemplateGames', 
        'StructureTemplateResponsible', 
        'StructureTemplateBonuses'
    ]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_to_file = {executor.submit(get_file_content_from_github, filename): filename 
                         for filename in files}
        
        for future in concurrent.futures.as_completed(future_to_file):
            filename = future_to_file[future]
            try:
                templates[filename] = future.result()
            except Exception as e:
                print(f"Error fetching template {filename}: {e}")
                templates[filename] = None
    
    return templates

def get_selected_casino_data():
    creds = get_service_account_credentials()
    sheets = build("sheets", "v4", credentials=creds)
    casino = sheets.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=f"{SHEET_NAME}!B1").execute().get("values", [[""]])[0][0].strip()
    rows = sheets.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range=f"{SHEET_NAME}!B2:S").execute().get("values", [])
    sections = {
        "General": (2, 3, 4),
        "Payments": (5, 6, 7),
        "Games": (8, 9, 10),
        "Responsible Gambling": (11, 12, 13),
        "Bonuses": (14, 15, 16),
    }
    data = {}
    comments_column = 17  # Column S (0-indexed)
    
    # Extract comments from column S
    all_comments = "\n".join(r[comments_column] for r in rows if len(r) > comments_column and r[comments_column].strip())
    
    for sec, (mi, ti, si) in sections.items():
        main = "\n".join(r[mi] for r in rows if len(r) > mi and r[mi].strip())
        if ti is not None:
            top = "\n".join(r[ti] for r in rows if len(r) > ti and r[ti].strip())
        else:
            top = "[No top comparison available]"
        if si is not None:
            sim = "\n".join(r[si] for r in rows if len(r) > si and r[si].strip())
        else:
            sim = "[No similar comparison available]"
        data[sec] = {"main": main or "[No data provided]", "top": top, "sim": sim}
    
    return casino, data, all_comments

def get_cached_casino_data():
    """Get casino data without caching to prevent tone interference"""
    return get_selected_casino_data()

# AI CLIENTS
client = openai.OpenAI(api_key=OPENAI_API_KEY)
anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

def call_openai(prompt):
    # Add fact constraint system message
    fact_constraint = "CRITICAL: Only use facts explicitly provided in the prompt. Never add information not in the source data. Do not make assumptions or add general knowledge about casinos."
    full_prompt = f"{fact_constraint}\n\n{prompt}"
    return client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": full_prompt}], temperature=0.3, max_tokens=1200).choices[0].message.content.strip()

def call_claude(prompt):
    # Add fact constraint system message
    fact_constraint = "CRITICAL: Only use facts explicitly provided in the prompt. Never add information not in the source data. Do not make assumptions or add general knowledge about casinos."
    full_prompt = f"{fact_constraint}\n\n{prompt}"
    response = anthropic.messages.create(model="claude-sonnet-5", max_tokens=1200, thinking={"type": "disabled"}, messages=[{"role": "user", "content": full_prompt}])
    return next(block.text for block in response.content if block.type == "text").strip()

def extract_casino_names_from_data(comparison_data):
    """Extract casino names from comparison data string.
    Assumes format like 'CasinoName (link): data...' or '[CasinoName](link): data...'
    """
    casino_names = []
    # Match patterns like:
    # - "CasinoName (https://...)"
    # - "[CasinoName](https://...)"
    # - "CasinoName:"
    patterns = [
        r'\[([^\]]+)\]\(https?://[^\)]+\)',  # [CasinoName](link)
        r'^([A-Z][A-Za-z0-9\s\.]+?)(?:\s*\(https?://|\s*:)',  # CasinoName (link or :
    ]

    for line in comparison_data.split('\n'):
        line = line.strip()
        if not line or line.startswith('[No '):
            continue
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                casino_name = match.group(1).strip()
                if casino_name and casino_name not in casino_names:
                    casino_names.append(casino_name)
                break

    return casino_names

def get_next_comparison_casino(available_casinos, used_casinos_tracker):
    """Select next casino using round-robin logic.

    Args:
        available_casinos: List of casino names available for comparison
        used_casinos_tracker: List tracking recently used casinos (max 5)

    Returns:
        Selected casino name or None if no casinos available
    """
    if not available_casinos:
        return None

    # Filter out recently used casinos (within last 5 uses)
    available = [c for c in available_casinos if c not in used_casinos_tracker[-5:]]

    # If all casinos have been used recently, reset and use the first one
    if not available:
        available = available_casinos

    # Return the first available casino
    return available[0] if available else None

def update_used_casinos_tracker(tracker, casino_name):
    """Add casino to the used tracker list."""
    if casino_name:
        tracker.append(casino_name)
    return tracker

def sort_comments_by_section(comments):
    """Use AI to intelligently sort comments by section."""
    if not comments or not comments.strip():
        return {"General": "", "Payments": "", "Games": "", "Responsible Gambling": "", "Bonuses": ""}

    prompt = f"""Please analyze the following feedback comments and sort them by the casino review sections they belong to.

Comments:
{comments}

Sections:
- General (overall casino experience, VPN support, reputation, establishment date, etc.)
- Payments (deposits, withdrawals, KYC, payment methods, processing times, etc.)
- Games (game selection, slots, table games, live casino, game providers, etc.)
- Responsible Gambling (limits, self-exclusion, problem gambling tools, etc.)
- Bonuses (welcome bonus, promotions, bonus terms, wagering requirements, etc.)

For each section, return ONLY the comments that belong to that section. If no comments belong to a section, leave it empty.

Format your response exactly like this:
**General**
[relevant comments here or leave empty]

**Payments**
[relevant comments here or leave empty]

**Games**
[relevant comments here or leave empty]

**Responsible Gambling**
[relevant comments here or leave empty]

**Bonuses**
[relevant comments here or leave empty]"""
    
    try:
        response = call_claude(prompt)
        # Parse the response into a dictionary
        sections = {"General": "", "Payments": "", "Games": "", "Responsible Gambling": "", "Bonuses": ""}
        current_section = None
        
        for line in response.split('\n'):
            line = line.strip()
            if line.startswith('**') and line.endswith('**'):
                section_name = line[2:-2]  # Remove ** from both ends
                if section_name in sections:
                    current_section = section_name
            elif current_section and line:
                if sections[current_section]:
                    sections[current_section] += " " + line
                else:
                    sections[current_section] = line
        
        return sections
    except Exception as e:
        print(f"Error sorting comments: {e}")
        # Fallback: return empty sections
        return {"General": "", "Payments": "", "Games": "", "Responsible Gambling": "", "Bonuses": ""}

def incorporate_comments_into_review(review_content, comments):
    """Use AI to incorporate relevant comments into the review before Adam's rewrite."""
    if not comments.strip():
        return review_content
    
    # Parse the review into sections first to maintain structure
    sections = parse_review_sections(review_content)
    
    if not sections:
        # If parsing fails, just return original content
        print("Failed to parse sections for comment incorporation, returning original")
        return review_content
    
    print(f"Incorporating comments into {len(sections)} sections")
    
    # For each section, ask AI to incorporate relevant comments
    updated_sections = []
    
    # Get the title (first line before sections)
    lines = review_content.split('\n')
    title = lines[0] if lines else ""
    
    for section in sections:
        section_title = section['title']
        section_content = section['content']
        
        # Ask AI to incorporate comments for this specific section
        prompt = f"""You are incorporating feedback comments into a specific section of a casino review.

Section: {section_title}
Current content:
{section_content}

All available comments:
{comments}

Please:
1. Look for any comments that specifically mention "{section_title}" or are clearly about this section
2. If you find relevant comments, incorporate that information into the section content
3. If no comments are relevant to this section, return the original content unchanged
4. Keep the writing style consistent with the original content
5. Do NOT include the section header in your response - only return the updated content

Return only the updated section content (without the **{section_title}** header):"""
        
        try:
            updated_content = call_claude(prompt)
            updated_sections.append(f"**{section_title}**\n{updated_content}")
            print(f"Successfully incorporated comments for section: {section_title}")
        except Exception as e:
            print(f"Error incorporating comments for {section_title}: {e}")
            # Fallback to original content for this section
            updated_sections.append(f"**{section_title}**\n{section_content}")
    
    # Reconstruct the full review
    result = title + "\n\n" + "\n\n".join(updated_sections)
    print("Comment incorporation completed successfully")
    return result

def parse_review_sections(content):
    """Parse review content into sections based on **Section Name** format."""
    section_headers = [
        "General",
        "Payments", 
        "Games",
        "Responsible Gambling",
        "Bonuses"
    ]
    
    lines = content.split('\n')
    sections = []
    current_section = None
    current_content = []
    
    for line in lines:
        line_stripped = line.strip()
        
        # Check if this line is a section header in **Section Name** format
        is_header = False
        for header in section_headers:
            if line_stripped == f"**{header}**":
                # Save previous section if exists
                if current_section and current_content:
                    sections.append({
                        'title': current_section,
                        'content': '\n'.join(current_content).strip()
                    })
                
                # Start new section
                current_section = header
                current_content = []
                is_header = True
                break
        
        # If not a header, add to current content
        if not is_header:
            if current_section is None:
                # Skip content before the first section header
                continue
            current_content.append(line)
    
    # Don't forget the last section
    if current_section and current_content:
        sections.append({
            'title': current_section,
            'content': '\n'.join(current_content).strip()
        })
    
    return sections

def rewrite_section(section_title, section_content, presentation_directive=None):
    """Rewrite a single section using the fine-tuned model.

    presentation_directive is this run's structural/opening-style directive for this
    section (see generate_presentation_plan()) - without it, this fine-tuned rewrite
    pass has no signal to vary from its own learned default phrasing/structure, and can
    flatten variety the earlier drafting step already introduced.
    """
    try:
        print(f"Rewriting section: {section_title}")

        user_content = section_content
        if presentation_directive and presentation_directive.strip():
            user_content = f"""Structural/stylistic direction for THIS rewrite (internal instruction - never reference, quote, or let this note appear in the output): {presentation_directive.strip()}

Follow this direction for how you open the section, which sub-topic leads, pacing, and comparison-opener variety. Do not fall back to your usual default opening or structure for a "{section_title}" section if it conflicts with this direction - the point is that this section should not read like every other {section_title} section you've written.

Now rewrite the following in your voice, keeping every fact exactly as given:

{section_content}"""

        response = client.chat.completions.create(
            model=FINE_TUNED_MODEL,
            messages=[
                {"role": "system", "content": "You are Adam Gros, founder and editor-in-chief of Gamblineers, a seasoned crypto casino expert with over 10 years of experience. Your background is in mathematics and data analysis. You are a helpful assistant that rewrites content provided by the user - ONLY THROUGH YOUR TONE AND STYLE, YOU DO NOT CHANGE FACTS or ADD NEW FACTS. YOU REWRITE GIVEN FACTS IN YOUR OWN STYLE.\n\nYou write from a first-person singular perspective and speak directly to \"you,\" the reader.\n\nYour voice is analytical, witty, blunt, and honest-with a sharp eye for BS and a deep respect for data. You balance professionalism with dry humor. You call things as they are, whether good or bad, and never sugarcoat reviews.\n\nWriting & Style Rules\n- Always write in first-person singular (\"I\")\n- Speak directly to you, the reader\n- Keep sentences under 20 words\n- Never use em dashes or emojis\n- Never use fluff words like: \"fresh,\" \"solid,\" \"straightforward,\" \"smooth,\" \"game-changer\"\n- Avoid clichés: \"kept me on the edge of my seat,\" \"whether you're this or that,\" etc.\n- Bold key facts, bonuses, or red flags\n- Use short paragraphs (2–3 sentences max)\n- Use bullet points for clarity (pros/cons, bonuses, steps, etc.)\n- Tables are optional for comparisons\n- Be helpful without sounding preachy or salesy\n- If something sucks, say it. If it's good, explain why.\n\nTone\n- Casual but sharp\n- Witty, occasionally sarcastic (in good taste)\n- Confident, never condescending\n- Conversational, never robotic\n- Always honest-even when it hurts\n\nMission & Priorities\n- Save readers from scammy casinos and shady bonus terms\n- Transparency beats hype-user satisfaction > feature lists\n- Crypto usability matters\n- The site serves readers, not casinos\n- Highlight what others overlook-good or bad\n\nPersonality Snapshot\n- INTJ: Strategic, opinionated, allergic to buzzwords\n- Meticulous and detail-obsessed\n- Enjoys awkward silences and bad data being called out\n- Prefers dry humor and meaningful critiques."},
                {"role": "user", "content": user_content}
            ],
            timeout=30  # Reduced timeout to 30 seconds
        )
        print(f"Successfully rewrote section: {section_title}")
        return response.choices[0].message.content
    except Exception as error:
        error_msg = f"Fine-tuned model failed for {section_title}: {error}"
        print(error_msg)
        return f"[Error rewriting {section_title}]\n{section_content}"

def generate_tldr_points(review_content):
    """Generate 4-5 TLDR bullet points summarizing the entire review."""
    try:
        print("Generating TLDR points from the full review...")

        tldr_prompt = f"""CRITICAL: Only use facts, numbers, and details explicitly stated in the review content below. Never invent, estimate, or add a fact that isn't in the text - if the review doesn't give a number for something, don't make one up. Two bullet points must never contradict each other.

Based on the following casino review, create 4-5 concise TLDR bullet points that summarize the key findings across ALL sections (General, Payments, Games, Responsible Gambling, Bonuses).

Review content:
{review_content}

Create TLDR points that:
1. Cover the most important aspects from different sections
2. Include specific facts, numbers, or standout features mentioned in the review - taken verbatim from the review, never invented
3. Mention both positive and negative aspects if present
4. Are concise but informative (1-2 sentences each)
5. Use Adam's direct, analytical tone

Format your response as exactly 4-5 bullet points, one per line, starting with "- " (dash and space).
Do not include any introduction or explanation - just the bullet points."""

        response = anthropic.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            thinking={"type": "disabled"},
            system="You are Adam Gros, founder and editor-in-chief of Gamblineers. Create concise, analytical TLDR points that capture the essence of casino reviews with your direct, no-nonsense style.",
            messages=[{"role": "user", "content": tldr_prompt}]
        )

        tldr_content = next(block.text for block in response.content if block.type == "text").strip()

        # Parse the bullet points into a list
        bullet_points = []
        for line in tldr_content.split('\n'):
            line = line.strip()
            if line.startswith('- '):
                bullet_points.append(line[2:])  # Remove "- " prefix

        print(f"Successfully generated {len(bullet_points)} TLDR points")
        return bullet_points

    except Exception as error:
        print(f"Error generating TLDR points: {error}")
        return ["Error generating TLDR summary"]

def generate_overview_section(casino_name, keyword, main_points, tldr_points=None, presentation_directive=None):
    """Generate Overview section using Adam's fine-tuned model, optionally with TLDR."""
    try:
        print("Generating Overview section with Adam's voice...")

        opening_hook_instruction = ""
        if presentation_directive and presentation_directive.strip():
            opening_hook_instruction = f"""

Opening-hook direction for THIS review (internal instruction, never reference, quote, or let this note appear in the output):
{presentation_directive.strip()}
Use this only to decide your opening angle/hook and rhetorical framing - it never introduces a fact, number, or claim beyond what's in the main points above."""

        # Create prompt for overview generation
        overview_prompt = f"""Write an engaging overview/introduction for a {casino_name} casino review. Use the following details:

SEO Keywords (MUST appear verbatim): {keyword}

Main points to cover:
{main_points}

Context: This overview will introduce a comprehensive review that covers General info, Payments, Games, Responsible Gambling, and Bonuses sections.

Write a compelling 2-3 paragraph introduction that:
1. MUST include the exact phrase "{keyword}" somewhere in the overview (verbatim for SEO purposes)
2. Touches on the main points provided
3. Sets expectations for what the full review will cover
4. Maintains your signature analytical and honest approach

CRITICAL: The phrase "{keyword}" must appear exactly as written in the overview text for SEO purposes. Do not paraphrase or modify these words.

CRITICAL: Only reference facts, numbers, and claims that appear in the main points above. Never invent a statistic, feature, or detail that isn't provided - this is a high-level teaser, not a place to guess at specifics.

Do not repeat information that will be covered in detail in other sections - this should be a high-level introduction that draws readers in.{opening_hook_instruction}"""

        response = anthropic.messages.create(
            model="claude-sonnet-5",
            max_tokens=800,
            thinking={"type": "disabled"},
            system="You are Adam Gros, founder and editor-in-chief of Gamblineers, a seasoned crypto casino expert with over 10 years of experience. Your background is in mathematics and data analysis. You are a helpful assistant that writes content in your distinctive voice and style.\n\nYou write from a first-person singular perspective and speak directly to \"you,\" the reader.\n\nYour voice is analytical, witty, blunt, and honest-with a sharp eye for BS and a deep respect for data. You balance professionalism with dry humor. You call things as they are, whether good or bad, and never sugarcoat reviews.\n\nWriting & Style Rules\n- Always write in first-person singular (\"I\")\n- Speak directly to you, the reader\n- Keep sentences under 20 words\n- Never use em dashes or emojis\n- Never use fluff words like: \"fresh,\" \"solid,\" \"straightforward,\" \"smooth,\" \"game-changer\"\n- Avoid clichés: \"kept me on the edge of my seat,\" \"whether you're this or that,\" etc.\n- Bold key facts, bonuses, or red flags\n- Use short paragraphs (2–3 sentences max)\n- Use bullet points for clarity (pros/cons, bonuses, steps, etc.)\n- Tables are optional for comparisons\n- Be helpful without sounding preachy or salesy\n- If something sucks, say it. If it's good, explain why.\n\nTone\n- Casual but sharp\n- Witty, occasionally sarcastic (in good taste)\n- Confident, never condescending\n- Conversational, never robotic\n- Always honest-even when it hurts",
            messages=[{"role": "user", "content": overview_prompt}]
        )

        overview_content = next(block.text for block in response.content if block.type == "text").strip()

        # Add TLDR section if points are provided
        if tldr_points:
            tldr_section = "\n\n**TLDR**"
            for point in tldr_points:
                tldr_section += f"\n- {point}"
            overview_content += tldr_section

        print("Successfully generated Overview section")
        return f"**Overview**\n{overview_content}"

    except Exception as error:
        error_msg = f"Failed to generate Overview section: {error}"
        print(error_msg)
        return f"**Overview**\n[Error generating Overview section: {error}]"

def rewrite_review_with_adam(review_content, presentation_plan=None):
    """Rewrite the entire review using Adam's voice, section by section.

    presentation_plan is this run's per-section structural/opening-style directive
    (see generate_presentation_plan()) - threaded through to rewrite_section() so the
    fine-tuned rewrite pass keeps the variety the earlier drafting step already
    introduced instead of flattening it back to its own default phrasing.
    """
    try:
        print("Starting Adam's rewrite process...")
        sections = parse_review_sections(review_content)

        if not sections:
            print("No sections detected, rewriting as whole")
            # If no sections detected, rewrite as whole
            return rewrite_section("Full Review", review_content)

        print(f"Found {len(sections)} sections to rewrite")
        rewritten_sections = []

        for i, section in enumerate(sections, 1):
            print(f"Processing section {i}/{len(sections)}: {section['title']}")

            # Never hand upstream generation errors to the fine-tuned rewrite model -
            # it doesn't recognize error text as invalid input and will fabricate a
            # full plausible-sounding section from it instead of flagging the failure.
            if section['content'].strip().startswith("[Error"):
                print(f"Section {section['title']} already failed upstream, skipping rewrite")
                rewritten_sections.append(f"**{section['title']}**\n{section['content']}")
                continue

            directive = (presentation_plan or {}).get(section['title'], "")
            rewritten_content = rewrite_section(section['title'], section['content'], directive)

            # If there was an error, still include it to avoid breaking the flow
            if rewritten_content.startswith("[Error rewriting"):
                print(f"Failed to rewrite {section['title']}, using original content")
                # Use original content if rewrite fails
                rewritten_sections.append(f"**{section['title']}**\n{section['content']}")
            else:
                rewritten_sections.append(f"**{section['title']}**\n{rewritten_content}")
        
        print("Adam's rewrite process completed successfully")
        return "\n\n".join(rewritten_sections)
        
    except Exception as e:
        error_msg = f"Fatal error in rewrite_review_with_adam: {str(e)}"
        print(error_msg)
        # Return original content if everything fails
        return f"[Rewrite failed - using original content]\n\n{review_content}"

MIN_CASINO_SLUG_LENGTH = 3  # skip pathologically short slugs to avoid false-positive matches

def get_gamblineers_casino_review_map():
    """Fetch the live Gamblineers post-sitemap and build a map of casino review URL
    slug -> full URL, used to auto-link casino mentions to their own Gamblineers review.

    Casino reviews are published at /<slug>-casino-review/ or /<slug>-review/ - both
    patterns exist in production (e.g. bitstarz-casino-review/ and cloudbet-review/).

    Fetched fresh every time internal linking runs, so newly published reviews become
    linkable without any manual list maintenance. Fails open (returns {}) on any error,
    so a transient network issue never blocks review generation - callers must treat an
    empty map as "skip internal linking for this run".
    """
    try:
        response = requests.get("https://gamblineers.com/post-sitemap.xml", timeout=10)
        response.raise_for_status()
        urls = re.findall(r'<loc>(https://gamblineers\.com/[^<]+)</loc>', response.text)

        review_map = {}
        for url in urls:
            path = url.rstrip('/').rsplit('/', 1)[-1]
            if path.endswith('-casino-review'):
                slug = path[:-len('-casino-review')]
            elif path.endswith('-review'):
                slug = path[:-len('-review')]
            else:
                continue
            if slug:
                review_map[slug] = url

        print(f"Fetched {len(review_map)} casino review URLs from the live sitemap")
        return review_map
    except Exception as e:
        print(f"Failed to fetch/parse Gamblineers sitemap, skipping internal linking: {e}")
        return {}

def _link_patterns(text, pattern_url_pairs):
    """Shared matcher: wrap each safe match of a compiled regex with a markdown link to
    its URL, preserving the exact text matched (casing/punctuation untouched). Skips any
    match that overlaps text already bold or already a link, to avoid nested/broken
    markdown, and recomputes those protected ranges fresh after each pattern is applied
    since earlier patterns may have inserted new links.

    Args:
        text: source text
        pattern_url_pairs: iterable of (compiled_pattern, url), most-specific-first -
            once a span is linked, later patterns can't re-claim any part of it.

    Returns:
        Text with matches wrapped as [matched text](url)
    """
    linked_text = text
    for pattern, url in pattern_url_pairs:
        protected = [False] * len(linked_text)
        for span in re.finditer(r'\*\*.*?\*\*', linked_text):
            for i in range(*span.span()):
                protected[i] = True
        for span in re.finditer(r'\[[^\]]*\]\(https?://[^\)]+\)', linked_text):
            for i in range(*span.span()):
                protected[i] = True

        pieces = []
        last_end = 0
        linked_count = 0
        for match in pattern.finditer(linked_text):
            start, end = match.span()
            if any(protected[start:end]):
                continue
            pieces.append(linked_text[last_end:start])
            pieces.append(f'[{match.group(0)}]({url})')
            last_end = end
            linked_count += 1
        pieces.append(linked_text[last_end:])
        linked_text = ''.join(pieces)

        if linked_count:
            print(f"Linked {linked_count} mention(s) matching '{pattern.pattern}' -> {url}")

    return linked_text

def link_casino_mentions(review_text, reviewed_casino_name, casino_review_map=None):
    """Auto-link mentions of other Gamblineers-reviewed casinos to their review pages.

    Matching is based on each casino's URL slug (e.g. "bc-game" from bc-game-review/),
    tolerant of how the writer punctuates/spaces the name in prose (so "BC.Game",
    "BC Game", and "bc-game" all match), while preserving the writer's original
    casing/punctuation in the visible link text - no attempt is made to "correct" a
    brand's display casing. Skips the casino being reviewed.

    Args:
        review_text: The review text content
        reviewed_casino_name: Name of the casino being reviewed (excluded from linking)
        casino_review_map: Optional pre-fetched slug->url map (mainly for testing);
            fetched live from the sitemap if not provided

    Returns:
        Review content with recognized casino names linked in [CasinoName](url) format
    """
    if casino_review_map is None:
        casino_review_map = get_gamblineers_casino_review_map()
    if not casino_review_map:
        return review_text

    reviewed_key = re.sub(r'[^a-z0-9]', '', reviewed_casino_name.lower())

    # Longest slug first so a shorter slug can't grab part of a longer, more specific match.
    candidates = sorted(casino_review_map.items(), key=lambda kv: -len(kv[0]))

    pairs = []
    for slug, url in candidates:
        key = re.sub(r'[^a-z0-9]', '', slug.lower())
        if len(key) < MIN_CASINO_SLUG_LENGTH or key == reviewed_key:
            continue
        chunks = [c for c in slug.split('-') if c]
        if not chunks:
            continue
        # Allow optional spaces/periods/hyphens between the slug's own words (so the
        # "bc-game" slug matches "BC.Game", "BC Game", "bc-game", "BCGame"), but never
        # bridge into unrelated neighboring words - \b anchors the whole phrase.
        pattern = re.compile(
            r'\b' + r'[\s\.\-]*'.join(re.escape(c) for c in chunks) + r'\b',
            re.IGNORECASE
        )
        pairs.append((pattern, url))

    linked_text = _link_patterns(review_text, pairs)
    print("Casino mention linking completed")
    return linked_text

# Cryptocurrency landing pages (name/ticker -> Gamblineers page). Curated by hand from the
# page-sitemap rather than fetched live: unlike casino reviews, these don't get added often,
# and their URL slugs aren't consistent enough (-casinos/-gambling/-sports-betting) to safely
# auto-discover without also sweeping in unrelated "-casinos" pages (e.g. high-roller-casinos).
# Matching is case-sensitive since several of these names/tickers are also common English
# words (Compound, Maker, Optimism, Sandbox, Dash, Gala, Amp, Kava) - requiring the proper-
# noun capitalization they'd carry as a coin name avoids linking generic lowercase usage.
CRYPTO_PAGE_MAP = {
    "Dogecoin": "https://gamblineers.com/dogecoin-casinos/", "DOGE": "https://gamblineers.com/dogecoin-casinos/",
    "Litecoin": "https://gamblineers.com/litecoin-casinos/", "LTC": "https://gamblineers.com/litecoin-casinos/",
    "Monero": "https://gamblineers.com/monero-casinos/", "XMR": "https://gamblineers.com/monero-casinos/",
    "Solana": "https://gamblineers.com/solana-casinos/", "SOL": "https://gamblineers.com/solana-casinos/",
    "Cardano": "https://gamblineers.com/cardano-casinos/", "ADA": "https://gamblineers.com/cardano-casinos/",
    "Ripple": "https://gamblineers.com/ripple-casinos/", "XRP": "https://gamblineers.com/ripple-casinos/",
    "Tron": "https://gamblineers.com/tron-casinos/", "TRX": "https://gamblineers.com/tron-casinos/",
    "Chainlink": "https://gamblineers.com/chainlink-casinos/", "LINK": "https://gamblineers.com/chainlink-casinos/",
    "Polkadot": "https://gamblineers.com/polkadot-casinos/", "DOT": "https://gamblineers.com/polkadot-casinos/",
    "Uniswap": "https://gamblineers.com/uniswap-casinos/", "UNI": "https://gamblineers.com/uniswap-casinos/",
    "Dash": "https://gamblineers.com/dash-casinos/",
    "Tether": "https://gamblineers.com/tether-gambling/", "USDT": "https://gamblineers.com/tether-gambling/",
    "Ethereum Classic": "https://gamblineers.com/ethereum-classic-casinos/", "ETC": "https://gamblineers.com/ethereum-classic-casinos/",
    "Ethereum": "https://gamblineers.com/ethereum-gambling/", "ETH": "https://gamblineers.com/ethereum-gambling/",
    "USD Coin": "https://gamblineers.com/usd-coin-casinos/", "USDC": "https://gamblineers.com/usd-coin-casinos/",
    "Binance Coin": "https://gamblineers.com/binance-coin-casinos/", "BNB": "https://gamblineers.com/binance-coin-casinos/",
    "Binance USD": "https://gamblineers.com/binance-usd-casinos/", "BUSD": "https://gamblineers.com/binance-usd-casinos/",
    "Bitcoin Cash": "https://gamblineers.com/bitcoin-cash-casinos/", "BCH": "https://gamblineers.com/bitcoin-cash-casinos/",
    "Bitcoin Gold": "https://gamblineers.com/bitcoin-gold-casinos/", "BTG": "https://gamblineers.com/bitcoin-gold-casinos/",
    "Bitcoin SV": "https://gamblineers.com/bitcoin-sv-casinos/", "BSV": "https://gamblineers.com/bitcoin-sv-casinos/",
    "Wrapped Bitcoin": "https://gamblineers.com/wrapped-bitcoin-casinos/", "WBTC": "https://gamblineers.com/wrapped-bitcoin-casinos/",
    "BitTorrent": "https://gamblineers.com/bittorrent-casinos/", "BTT": "https://gamblineers.com/bittorrent-casinos/",
    "Shiba Inu": "https://gamblineers.com/shiba-inu-casinos/", "SHIB": "https://gamblineers.com/shiba-inu-casinos/",
    "Dai": "https://gamblineers.com/dai-casinos/",
    "Decentraland": "https://gamblineers.com/decentraland-casinos/", "MANA": "https://gamblineers.com/decentraland-casinos/",
    "DigiByte": "https://gamblineers.com/digibyte-casinos/", "DGB": "https://gamblineers.com/digibyte-casinos/",
    "Enjin": "https://gamblineers.com/enjin-casinos/", "ENJ": "https://gamblineers.com/enjin-casinos/",
    "Fantom": "https://gamblineers.com/fantom-casinos/", "FTM": "https://gamblineers.com/fantom-casinos/",
    "Filecoin": "https://gamblineers.com/filecoin-casinos/", "FIL": "https://gamblineers.com/filecoin-casinos/",
    "Gala": "https://gamblineers.com/gala-casinos/",
    "IoTeX": "https://gamblineers.com/iotex-casinos/",
    "Kusama": "https://gamblineers.com/kusama-casinos/", "KSM": "https://gamblineers.com/kusama-casinos/",
    "NEM": "https://gamblineers.com/nem-casinos/", "XEM": "https://gamblineers.com/nem-casinos/",
    "OmiseGO": "https://gamblineers.com/omisego-casinos/", "OMG": "https://gamblineers.com/omisego-casinos/",
    "PancakeSwap": "https://gamblineers.com/pancakeswap-casinos/", "CAKE": "https://gamblineers.com/pancakeswap-casinos/",
    "Pax Dollar": "https://gamblineers.com/pax-dollar-casinos/", "USDP": "https://gamblineers.com/pax-dollar-casinos/",
    "Polygon": "https://gamblineers.com/polygon-casinos/", "MATIC": "https://gamblineers.com/polygon-casinos/",
    "Sandbox": "https://gamblineers.com/sandbox-casinos/", "The Sandbox": "https://gamblineers.com/sandbox-casinos/", "SAND": "https://gamblineers.com/sandbox-casinos/",
    "SushiSwap": "https://gamblineers.com/sushiswap-casinos/", "SUSHI": "https://gamblineers.com/sushiswap-casinos/",
    "Terra": "https://gamblineers.com/terra-casinos/", "LUNA": "https://gamblineers.com/terra-casinos/",
    "TrueUSD": "https://gamblineers.com/true-usd-casinos/", "TUSD": "https://gamblineers.com/true-usd-casinos/",
    "VeChain": "https://gamblineers.com/vechain-casinos/", "VET": "https://gamblineers.com/vechain-casinos/",
    "Yearn Finance": "https://gamblineers.com/yearn-finance-casinos/", "YFI": "https://gamblineers.com/yearn-finance-casinos/",
    "Zilliqa": "https://gamblineers.com/zilliqa-casinos/", "ZIL": "https://gamblineers.com/zilliqa-casinos/",
    "Bonk": "https://gamblineers.com/bonk-casinos/", "BONK": "https://gamblineers.com/bonk-casinos/",
    "Worldcoin": "https://gamblineers.com/worldcoin-casinos/", "WLD": "https://gamblineers.com/worldcoin-casinos/",
    "ApeCoin": "https://gamblineers.com/apecoin-casinos/", "APE": "https://gamblineers.com/apecoin-casinos/",
    "Near Protocol": "https://gamblineers.com/near-protocol-casinos/", "NEAR": "https://gamblineers.com/near-protocol-casinos/",
    "Kava": "https://gamblineers.com/kava-casinos/",
    "Neo": "https://gamblineers.com/neo-casinos/",
    "Maker": "https://gamblineers.com/maker-casinos/", "MKR": "https://gamblineers.com/maker-casinos/",
    "Arbitrum": "https://gamblineers.com/arbitrum-casinos/", "ARB": "https://gamblineers.com/arbitrum-casinos/",
    "Chiliz": "https://gamblineers.com/chiliz-casinos/", "CHZ": "https://gamblineers.com/chiliz-casinos/",
    "Compound": "https://gamblineers.com/compound-casinos/", "COMP": "https://gamblineers.com/compound-casinos/",
    "Helium": "https://gamblineers.com/helium-casinos/", "HNT": "https://gamblineers.com/helium-casinos/",
    "Internet Computer": "https://gamblineers.com/internet-computer-casinos/", "ICP": "https://gamblineers.com/internet-computer-casinos/",
    "Kaspa": "https://gamblineers.com/kaspa-casinos/", "KAS": "https://gamblineers.com/kaspa-casinos/",
    "Klaytn": "https://gamblineers.com/klaytn-casinos/",
    "MultiversX": "https://gamblineers.com/multiversx-casinos/", "EGLD": "https://gamblineers.com/multiversx-casinos/",
    "Nexo": "https://gamblineers.com/nexo-casinos/",
    "Oasis": "https://gamblineers.com/oasis-casinos/", "ROSE": "https://gamblineers.com/oasis-casinos/",
    "Optimism": "https://gamblineers.com/optimism-casinos/", "OP": "https://gamblineers.com/optimism-casinos/",
    "Pax Gold": "https://gamblineers.com/pax-gold-casinos/", "PAXG": "https://gamblineers.com/pax-gold-casinos/",
    "Pepe": "https://gamblineers.com/pepe-casinos/",
    "Samoyedcoin": "https://gamblineers.com/samoyedcoin-casinos/", "SAMO": "https://gamblineers.com/samoyedcoin-casinos/",
    "Stablecoin": "https://gamblineers.com/stablecoin-casinos/",
    "Sui": "https://gamblineers.com/sui-casinos/",
    "Synthetix": "https://gamblineers.com/synthetix-casinos/", "SNX": "https://gamblineers.com/synthetix-casinos/",
    "Tezos": "https://gamblineers.com/tezos-casinos/", "XTZ": "https://gamblineers.com/tezos-casinos/",
    "The Graph": "https://gamblineers.com/the-graph-casinos/", "GRT": "https://gamblineers.com/the-graph-casinos/",
    "Theta Network": "https://gamblineers.com/theta-network-casinos/", "THETA": "https://gamblineers.com/theta-network-casinos/",
    "THORChain": "https://gamblineers.com/thorchain-casinos/", "RUNE": "https://gamblineers.com/thorchain-casinos/",
    "Toncoin": "https://gamblineers.com/toncoin-casinos/", "TON": "https://gamblineers.com/toncoin-casinos/",
    "Zcash": "https://gamblineers.com/zcash-casinos/", "ZEC": "https://gamblineers.com/zcash-casinos/",
    "Aave": "https://gamblineers.com/aave-casinos/",
    "Algorand": "https://gamblineers.com/algorand-casinos/", "ALGO": "https://gamblineers.com/algorand-casinos/",
    "Amp": "https://gamblineers.com/amp-casinos/",
    "Axie Infinity": "https://gamblineers.com/axie-infinity-casinos/", "AXS": "https://gamblineers.com/axie-infinity-casinos/",
    "Basic Attention Token": "https://gamblineers.com/basic-attention-token-casinos/", "BAT": "https://gamblineers.com/basic-attention-token-casinos/",
    "Avalanche": "https://gamblineers.com/avalanche-casinos/", "AVAX": "https://gamblineers.com/avalanche-casinos/",
    "Cosmos": "https://gamblineers.com/cosmos-casinos/", "ATOM": "https://gamblineers.com/cosmos-casinos/",
    "Cronos": "https://gamblineers.com/cronos-casinos/", "CRO": "https://gamblineers.com/cronos-casinos/",
    "Curve DAO": "https://gamblineers.com/curve-dao-casinos/", "CRV": "https://gamblineers.com/curve-dao-casinos/",
    "EOS": "https://gamblineers.com/eos-casinos/",
    "Floki": "https://gamblineers.com/floki-casinos/",
    "Hedera": "https://gamblineers.com/hedera-casinos/", "HBAR": "https://gamblineers.com/hedera-casinos/",
    "Loopring": "https://gamblineers.com/loopring-casinos/", "LRC": "https://gamblineers.com/loopring-casinos/",
    "Qtum": "https://gamblineers.com/qtum-casinos/",
    "Stellar": "https://gamblineers.com/stellar-casinos/", "XLM": "https://gamblineers.com/stellar-casinos/",
    "1inch": "https://gamblineers.com/1inch-network-casinos/", "1inch Network": "https://gamblineers.com/1inch-network-casinos/",
    "Aptos": "https://gamblineers.com/aptos-casinos/", "APT": "https://gamblineers.com/aptos-casinos/",
}

def link_crypto_mentions(review_text):
    """Auto-link mentions of specific cryptocurrencies (e.g. in a Payments section's
    accepted-coins list) to their Gamblineers coin page. Case-sensitive: several coin
    names/tickers double as common English words (Compound, Maker, Optimism, Sandbox,
    Dash, Gala, Amp, Kava), and requiring proper-noun capitalization avoids linking
    generic lowercase usage of those words.
    """
    # Longest name first (e.g. "Ethereum Classic" before "Ethereum") so a shorter name
    # can't grab part of a longer, more specific one.
    pairs = [
        (re.compile(r'\b' + re.escape(name) + r'\b'), url)
        for name, url in sorted(CRYPTO_PAGE_MAP.items(), key=lambda kv: -len(kv[0]))
    ]
    linked_text = _link_patterns(review_text, pairs)
    print("Cryptocurrency mention linking completed")
    return linked_text

# Topical/resource pages worth linking on sight - concept phrase -> Gamblineers guide page.
# Deliberately excludes generic gambling-activity nouns (slots, dice, roulette, free spins,
# VIP, etc.) that almost always describe the REVIEWED casino's own feature rather than a
# generic reference, since auto-linking those tends to look wrong/spammy in context.
TOPICAL_PAGE_MAP = {
    "anonymous crypto casino": "https://gamblineers.com/anonymous-bitcoin-casinos/",
    "anonymous cryptocurrency casino": "https://gamblineers.com/anonymous-bitcoin-casinos/",
    "anonymous bitcoin casino": "https://gamblineers.com/anonymous-bitcoin-casinos/",
    "anonymous casino": "https://gamblineers.com/anonymous-bitcoin-casinos/",
    "minimum deposit": "https://gamblineers.com/minimum-deposit-bitcoin-casino/",
    "fast withdrawals": "https://gamblineers.com/fast-withdrawal-casinos/",
    "fast withdrawal": "https://gamblineers.com/fast-withdrawal-casinos/",
    "provably fair games": "https://gamblineers.com/provably-fair-games/",
    "provably fair": "https://gamblineers.com/provably-fair-games/",
    "responsible gambling tools": "https://gamblineers.com/responsible-gambling/",
    "responsible gambling": "https://gamblineers.com/responsible-gambling/",
    "wager-free bitcoin casino bonuses": "https://gamblineers.com/wager-free-bitcoin-casino-bonuses/",
    "wager-free": "https://gamblineers.com/wager-free-bitcoin-casino-bonuses/",
    "wager free": "https://gamblineers.com/wager-free-bitcoin-casino-bonuses/",
    "zero wagering": "https://gamblineers.com/wager-free-bitcoin-casino-bonuses/",
    "no wagering": "https://gamblineers.com/wager-free-bitcoin-casino-bonuses/",
    "faucet promotions": "https://gamblineers.com/bitcoin-casino-faucet/",
    "faucet": "https://gamblineers.com/bitcoin-casino-faucet/",
    "cashback": "https://gamblineers.com/bitcoin-casino-cashback/",
    "high rollers": "https://gamblineers.com/high-roller-casinos/",
    "high roller": "https://gamblineers.com/high-roller-casinos/",
    "bitcoin casino bonuses": "https://gamblineers.com/bitcoin-bonus-guide/",
    "crypto casino bonuses": "https://gamblineers.com/bitcoin-bonus-guide/",
}

def link_topical_page_mentions(review_text):
    """Auto-link recognized concept phrases (provably fair, responsible gambling, etc.)
    to their Gamblineers guide page. Case-insensitive since these are ordinary phrases,
    not proper nouns.
    """
    # Longest phrase first (e.g. "responsible gambling tools" before "responsible
    # gambling") so the fuller, more specific anchor text wins where it's present.
    pairs = [
        (re.compile(r'\b' + re.escape(phrase) + r'\b', re.IGNORECASE), url)
        for phrase, url in sorted(TOPICAL_PAGE_MAP.items(), key=lambda kv: -len(kv[0]))
    ]
    linked_text = _link_patterns(review_text, pairs)
    print("Topical page linking completed")
    return linked_text

def fix_bullet_points(review_content):
    """Fix all formatting issues from Adam's rewrite for proper Google Docs display."""
    try:
        import re

        # 0. Strip stray backslashes Adam's rewrite sometimes drops next to a plain
        # word (e.g. "\Bitstarz\" instead of "Bitstarz") - never a valid markdown
        # escape, just noise that would otherwise show up as literal backslashes in
        # the doc. Escapes handled by rules 1-5 below (\*, \#, \+, \-) are followed by
        # punctuation, not a letter/digit, so this never touches them.
        fixed_content = re.sub(r'\\(?=[A-Za-z0-9])', '', review_content)
        fixed_content = re.sub(r'(?<=[A-Za-z0-9])\\(?=\s|$)', '', fixed_content, flags=re.MULTILINE)

        # 1. Replace \* at the beginning of lines with dash bullets for Google Docs
        fixed_content = re.sub(r'^\\+\* ', r'- ', fixed_content, flags=re.MULTILINE)

        # 1b. Same for a plain, unescaped "* " bullet marker - Adam's rewrite doesn't
        # always escape it. A bullet marker is "*" immediately followed by a space;
        # an italic-opening "*word" (no space after the asterisk) is left alone here
        # so this can't eat real emphasis.
        fixed_content = re.sub(r'^\* ', r'- ', fixed_content, flags=re.MULTILINE)

        # 2. Convert escaped hash headers (\#\#\#) to bold format - preserve existing ** if present
        fixed_content = re.sub(r'^\\+\#\\+\#\\+\# \*\*(.+?)\*\*$', r'**\1**', fixed_content, flags=re.MULTILINE)
        fixed_content = re.sub(r'^\\+\#\\+\#\\+\# (.+)$', r'**\1**', fixed_content, flags=re.MULTILINE)

        # 3. Convert markdown headings (## Heading, ### Heading, ...) to bold format
        fixed_content = re.sub(r'^#{2,6} (.+)$', r'**\1**', fixed_content, flags=re.MULTILINE)

        # 4. Fix escaped plus signs in bonus descriptions (\+ -> +)
        fixed_content = re.sub(r'\\+\+', r'+', fixed_content)

        # 5. Ensure \- bullets (which are already correct) stay as - bullets
        fixed_content = re.sub(r'^\\+\- ', r'- ', fixed_content, flags=re.MULTILINE)

        print("All formatting issues fixed successfully")
        return fixed_content

    except Exception as e:
        print(f"Error fixing formatting: {e}")
        # Return original content if fixing fails
        return review_content

# ---- PRESENTATION PLAN (creative-direction step) ----

# Sub-topic labels per section, mirrored from the paragraph groupings already defined
# in templates/StructureTemplate*.txt. The Presentation Plan model is ONLY ever shown
# this static list - never real casino data - so it structurally cannot invent, restate,
# or leak a real fact, number, or casino name. If a structure template's paragraph
# topics change, update this list to match.
SECTION_SUBTOPICS = {
    "General": [
        "VPN friendliness & anonymity",
        "Casino age & extra products (lottery/trading)",
        "Country restrictions & live-chat availability",
        "Website usability (pop-ups, broken links, modern look)",
    ],
    "Payments": [
        "Cryptocurrencies accepted & buying crypto",
        "Withdrawal limits (daily/weekly/monthly)",
        "Withdrawal processing time",
        "KYC verification speed",
    ],
    "Games": [
        "Number of games & providers (incl. top-provider coverage)",
        "In-house games",
        "Game filters",
    ],
    "Responsible Gambling": [
        "Self-exclusion & other RG tools",
        "Cooling-off option",
        "Contacting support to activate tools",
    ],
    "Bonuses": [
        "No deposit bonuses",
        "Welcome/first deposit bonuses",
        "Sports bonuses",
        "Faucet promotions",
    ],
}

PRESENTATION_PLAN_HEADERS = ["General", "Payments", "Games", "Responsible Gambling", "Bonuses", "Overview"]

# Semantically-neutral nonce pool: deliberately unrelated to gambling, casinos, writing,
# or emotion, so none of them can plausibly bias tone or metaphor. They exist only to
# vary the token context each run - never to enumerate structural options.
PRESENTATION_NONCE_WORDS = [
    "lighthouse", "quartz", "marmalade", "tundra", "velvet", "kazoo", "orbit",
    "sundial", "aluminum", "papaya", "trombone", "granite", "compass", "lantern",
    "apricot", "tricycle", "obelisk", "walnut", "ferry", "mitten", "spatula",
    "canyon", "thistle", "harmonica", "pebble", "umbrella", "lattice", "marble",
    "satchel", "bungalow", "cardigan", "silo", "tangerine", "kettle", "meadow",
    "buckle", "chisel", "plaid", "veranda", "wicker", "cobalt", "driftwood",
    "gazebo", "jigsaw", "knapsack", "mosaic", "nutmeg", "otter", "pinwheel",
    "quill", "ribbon", "saddle", "teapot", "urn", "violin", "wagon",
    "xylophone", "yarn", "zipper",
]


def parse_presentation_plan(plan_text: str) -> Dict[str, str]:
    """Split a Presentation Plan LLM response into per-section directive strings.

    Expects '### Header' markers (deliberately different from the '**Header**' markers
    parse_review_sections() looks for, so this internal planning text can never be
    confused with actual review output). Returns {} if no headers are found.
    """
    lines = plan_text.split('\n')
    result = {}
    current = None
    buffer = []
    for line in lines:
        stripped = line.strip()
        matched = None
        for header in PRESENTATION_PLAN_HEADERS:
            if stripped.lower() in (f"### {header}".lower(), f"###{header}".lower()):
                matched = header
                break
        if matched:
            if current and buffer:
                result[current] = '\n'.join(buffer).strip()
            current = matched
            buffer = []
        elif current:
            buffer.append(line)
    if current and buffer:
        result[current] = '\n'.join(buffer).strip()
    return result


def generate_presentation_plan(casino: str) -> Dict[str, str]:
    """Generate one whole-review 'Presentation Plan': free-form structural and rhetorical
    directives (never facts) that vary sub-topic lead order, opening-sentence style,
    comparison-opener variety, and paragraph shape/energy across runs for the 5 body
    sections, plus this review's Overview opening-hook angle.

    Deliberately fact-blind: only takes `casino` (for flavor text) and the static
    SECTION_SUBTOPICS labels - never real casino data - so it cannot invent or leak a
    real fact/number/casino name; it was never shown one.

    Returns a dict keyed by 'General'|'Payments'|'Games'|'Responsible Gambling'|'Bonuses'|
    'Overview' -> directive string. Returns {} on any failure (fail-open: callers must
    treat missing keys as "no extra direction, behave exactly as before this feature
    existed").
    """
    nonce = random.randint(1, 999_999)
    nonce_word = random.choice(PRESENTATION_NONCE_WORDS)

    topic_lines = []
    for sec, topics in SECTION_SUBTOPICS.items():
        tag = "FIXED ORDER - narrative treatment only, do NOT reorder" if sec == "Bonuses" else "order is yours to decide"
        topic_lines.append(f"- {sec} ({tag}): " + "; ".join(topics))
    topic_block = "\n".join(topic_lines)

    plan_prompt = f"""We're about to write a review of a crypto casino called {casino}. To avoid every review reading with the exact same structure, you're deciding this run's PRESENTATION STRATEGY only - never any fact.

Random seed for this run (ignore its literal meaning - it has none; it exists only so this run's creative choices don't default to your most common pattern): nonce={nonce}, nonce_word="{nonce_word}"

You have NOT been given any real information about this specific casino - no numbers, no names, no verdicts. That's intentional: you cannot invent or leak a fact you were never shown. Your job is purely about ORDER, OPENERS, and SHAPE.

Below are the five review sections, each with its fixed list of sub-topics that MAY appear in it (whether a given sub-topic ends up in the final text at all depends on that casino's actual data, which you don't see - so don't assume any of them apply this run):

{topic_block}

For each of the four sections marked "order is yours to decide" (General, Payments, Games, Responsible Gambling):
1. Pick a different lead sub-topic than the obvious first-listed one, unless you have a genuine structural reason not to. As a concrete starting point, consider using (nonce + position in this list) mod (number of topics in that section) to pick an unusual lead - but use your own judgment over the arithmetic if it would produce an awkward result.
2. State, in your own words, which sub-topic you're treating as the lead, which you're pushing later, and briefly why.
3. Decide an opening-sentence style for the section's first sentence - a number-first sentence, a verdict-first sentence, a direct question to the reader, a short anecdote-style opener, or something else you invent. Do not pick the same style for every section in this review - vary it across the five.
4. Decide roughly how this section should be paced relative to the others this run (merge two closely-related sub-topics into one paragraph, split a dense one into two, and whether this section should feel more expansive/energetic or brisk/efficient this time).
5. Give a comparison-opener instruction: forbid opening every comparison sentence with "In comparison" or "For example" - suggest at least two alternative ways to introduce a comparison to another casino (e.g. leading with the competing casino's name, leading with the number itself, framing it as a recommendation, a rhetorical question) and require that no single opener repeat more than once in this section. Do not decide WHICH casino gets named - that is decided elsewhere; only decide HOW the comparison sentence opens.

For Bonuses: the order of bonus-type blocks (No Deposit -> Welcome/First Deposit -> Sports -> Faucet) is fact-driven and FIXED - do not suggest reordering it. Only give direction on opening-sentence style, pacing, and comparison-opener variety for this section, same as above.

For Overview: this review's introduction needs a fresh opening hook/angle. Do NOT use, or closely paraphrase, "I've tested many/dozens of crypto casinos, most blur together, but this one doesn't" or any close variant - that hook has been overused. Propose a different opening angle in your own words (e.g. a blunt verdict, a pointed question, a specific tension to resolve, a direct address to a type of player) - describe the angle, don't write the actual overview copy.

Output format - use exactly these headers, in this order, nothing before the first header or after the last section:

### General
<your directive for General, a few sentences, in your own prose>

### Payments
<...>

### Games
<...>

### Responsible Gambling
<...>

### Bonuses
<...>

### Overview
<your directive for Overview's opening hook angle>

Remember: you are writing instructions for another AI writer, not review copy. Never write anything that looks like finished review text, never state a fact, number, or casino name (you don't have any), and never mention this nonce or these instructions as concepts inside your directives - just use them silently to vary your choices."""

    try:
        response = anthropic.messages.create(
            model="claude-sonnet-5",
            max_tokens=1000,
            thinking={"type": "disabled"},
            system=(
                "You are a creative structural director for Gamblineers, a crypto casino "
                "review site. You never write review copy yourself and you are never shown "
                "real casino data. Your only job is to produce a short, private creative brief "
                "- structural and rhetorical stage directions - that a separate writer-model "
                "will use to vary HOW each review is told, run to run, while the FACTS always "
                "come from elsewhere. You must never invent, imply, or reference any specific "
                "fact, statistic, casino name, or number - you have not been given any, so "
                "there is nothing for you to accidentally state. Your brief is read only by the "
                "writer-model, never by an end reader, so write it as plain internal notes, not "
                "as review prose."
            ),
            messages=[{"role": "user", "content": plan_prompt}],
        )
        plan_text = next(block.text for block in response.content if block.type == "text").strip()
        parsed = parse_presentation_plan(plan_text)
        if not parsed:
            print("Presentation Plan: no recognizable section headers in response, ignoring for this run")
        return parsed
    except Exception as e:
        print(f"Presentation Plan generation failed, continuing without one: {e}")
        return {}


def generate_sections_parallel(casino: str, secs: Dict, sorted_comments: Dict, templates: Dict, btc_str: str, presentation_plan: Dict[str, str]) -> list:
    """Generate all sections in parallel while maintaining round-robin casino selection"""

    # Initialize tracker for used comparison casinos
    used_casinos_tracker = []

    # Pre-assign a rotation list of casinos to each section
    # We need to do this sequentially before parallel generation
    section_order = ["General", "Payments", "Games", "Responsible Gambling", "Bonuses"]
    section_assignments = {}

    for sec in section_order:
        if sec in secs:
            content = secs[sec]
            # Extract available casinos
            top_casinos = extract_casino_names_from_data(content["top"])
            sim_casinos = extract_casino_names_from_data(content["sim"])
            all_available = top_casinos + sim_casinos

            # Build a prioritized rotation list for this section
            # This list will help the AI rotate through different casinos for different comparisons
            rotation_list = []
            temp_tracker = used_casinos_tracker.copy()

            # Get up to 4 casinos for rotation within this section
            for _ in range(min(4, len(all_available))):
                next_casino = get_next_comparison_casino(all_available, temp_tracker)
                if next_casino:
                    rotation_list.append(next_casino)
                    temp_tracker.append(next_casino)

            # Update the main tracker with the first casino from this section's rotation
            if rotation_list:
                used_casinos_tracker.append(rotation_list[0])

            # Store assignment
            section_assignments[sec] = rotation_list

    # Prepare data for each section with pre-assigned casino rotation lists
    section_data = [
        (sec, secs[sec], templates, sorted_comments, casino, btc_str, section_assignments.get(sec, []),
         (presentation_plan or {}).get(sec, ""))
        for sec in section_order if sec in secs
    ]

    # Generate sections in parallel with max 3 workers to avoid API rate limits
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        # Submit all tasks and maintain order
        future_to_section = {
            executor.submit(generate_section_with_assignment, data): data[0]
            for data in section_data
        }

        # Collect results in the original order
        results = {}
        for future in concurrent.futures.as_completed(future_to_section):
            section_name = future_to_section[future]
            try:
                results[section_name] = future.result()
            except Exception as e:
                print(f"Error in parallel generation for {section_name}: {e}")
                results[section_name] = f"**{section_name}**\n[Error: {str(e)}]\n"

    # Return results in the original section order
    return [results[sec] for sec in section_order if sec in results]

def generate_section_with_assignment(section_data: Tuple) -> str:
    """Generate section with pre-assigned rotation list of casinos"""
    sec, content, templates, sorted_comments, casino, btc_str, casino_rotation_list, presentation_directive = section_data

    # Define section configurations
    section_configs = {
        "General": ("BaseGuidelinesClaude", "StructureTemplateGeneral", call_claude),
        "Payments": ("BaseGuidelinesClaude", "StructureTemplatePayments", call_claude),
        "Games": ("BaseGuidelinesClaude", "StructureTemplateGames", call_claude),
        "Responsible Gambling": ("BaseGuidelinesResponsible", "StructureTemplateResponsible", call_claude),
        "Bonuses": ("BaseGuidelinesClaude", "StructureTemplateBonuses", call_claude),
    }

    try:
        guidelines_file, structure_file, fn = section_configs[sec]

        # Get templates from cached data
        guidelines = templates.get(guidelines_file)
        structure = templates.get(structure_file)
        prompt_template = templates.get('PromptTemplate')

        if not guidelines or not structure or not prompt_template:
            return f"**{sec}**\n[Error: Missing templates for section {sec}]\n"

        # Get comments for this specific section
        section_comments = ""
        if sorted_comments.get(sec, "").strip():
            section_comments = f"\n\nCRITICAL USER FEEDBACK - MUST INCLUDE ALL DETAILS:\n{sorted_comments[sec]}\n\nIMPORTANT: The above user feedback contains specific, detailed information that MUST be included in your review. Do NOT summarize, simplify, or condense this information. Include ALL details, numbers, steps, mechanisms, and specifics exactly as provided. This information is factual and verified - include it comprehensively in the review section."

        # Build round-robin instruction for the prompt
        round_robin_instruction = ""
        if casino_rotation_list:
            casinos_str = "', '".join(casino_rotation_list)
            round_robin_instruction = f"\n\nIMPORTANT - Casino Comparison Rotation:\nWhen making comparisons to other casinos in this section, rotate through these casinos from the Top/Similar data in THIS ORDER: '{casinos_str}'.\n\nFor your FIRST comparison, use '{casino_rotation_list[0]}'. For your SECOND comparison, use '{casino_rotation_list[1] if len(casino_rotation_list) > 1 else casino_rotation_list[0]}'. Continue rotating through this list for any additional comparisons. This ensures variety and prevents any single casino from being mentioned too frequently."
            print(f"Section {sec}: Rotation list = {casino_rotation_list}")

        # Shuffle top casino lines so the AI sees a different order each time,
        # preventing it from always picking the same casino for comparisons
        top_lines = [l for l in content["top"].split('\n') if l.strip()]
        random.shuffle(top_lines)
        shuffled_top = '\n'.join(top_lines)

        # Presentation Direction from this run's whole-review Presentation Plan
        # (structural/rhetorical guidance only - never facts; see generate_presentation_plan()).
        presentation_instruction = ""
        if presentation_directive and presentation_directive.strip():
            presentation_instruction = (
                "\n\nIMPORTANT - Presentation Direction for this review (internal instruction, "
                "never reference, quote, or let this note appear in your output):\n"
                f"{presentation_directive.strip()}\n"
                "This direction is about STRUCTURE, ORDER, and RHETORICAL FRAMING ONLY - it does "
                "not decide which casino you name (follow the Casino Comparison Rotation "
                "instruction above for that), and it never overrides the STRUCTURE section's "
                "fact-driven IF/ELSE rules (e.g. which bonus types exist) or the CRITICAL USER "
                "FEEDBACK block if one is present above - those always win. If the sub-topic this "
                "direction suggests leading with turns out not to apply this review, silently lead "
                "with the next applicable sub-topic instead; never mention that you skipped anything."
            )

        prompt = prompt_template.format(
            casino=casino,
            section=sec,
            guidelines=guidelines,
            structure=structure,
            main=content["main"] + section_comments,
            top=shuffled_top,
            sim=content["sim"],
            btc_value=btc_str
        ) + round_robin_instruction + presentation_instruction

        review = fn(prompt)
        return f"**{sec}**\n{review}\n"

    except Exception as e:
        print(f"Error generating section {sec}: {e}")
        return f"**{sec}**\n[Error generating section: {str(e)}]\n"

def write_review_link_to_sheet(link):
    """Write the review link to cell B7 in the spreadsheet."""
    creds = get_service_account_credentials()
    sheets = build("sheets", "v4", credentials=creds)
    body = {"values": [[link]]}
    sheets.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID, 
        range=f"{SHEET_NAME}!B7", 
        valueInputOption="RAW", 
        body=body
    ).execute()

def _reconstruct_markdown_from_formatting(plain_text, formatting_requests):
    """Rebuild **bold**/[text](url)/*italic* markdown from plain_text plus the computed
    Google Docs style ranges, so it can be diffed against the source text as a sanity
    check before anything is sent to the Docs API - if replaying the ranges doesn't
    reproduce the original markdown exactly, the ranges are wrong and would land the
    styling on the wrong characters (the mid-word bold/link corruption this guards
    against).
    """
    opens, closes = {}, {}
    for req in formatting_requests:
        style = req.get("updateTextStyle")
        if not style:
            continue
        text_style = style["textStyle"]
        start = style["range"]["startIndex"]
        end = style["range"]["endIndex"]
        if text_style.get("link"):
            opens.setdefault(start, []).append("[")
            closes.setdefault(end, []).append(f"]({text_style['link']['url']})")
        elif text_style.get("bold"):
            opens.setdefault(start, []).append("**")
            closes.setdefault(end, []).append("**")
        elif text_style.get("italic"):
            opens.setdefault(start, []).append("*")
            closes.setdefault(end, []).append("*")

    pieces = []
    for i, ch in enumerate(plain_text):
        idx = 1 + i
        pieces.extend(closes.get(idx, []))
        pieces.extend(opens.get(idx, []))
        pieces.append(ch)
    end_idx = 1 + len(plain_text)
    pieces.extend(closes.get(end_idx, []))
    pieces.extend(opens.get(end_idx, []))
    return "".join(pieces)


def _first_diff_context(reconstructed, original, radius=40):
    """Human-readable pointer to where two strings first diverge, for error messages."""
    for i in range(min(len(reconstructed), len(original))):
        if reconstructed[i] != original[i]:
            return (
                f"reconstructed={reconstructed[max(0, i - radius):i + radius]!r} "
                f"vs original={original[max(0, i - radius):i + radius]!r}"
            )
    if len(reconstructed) != len(original):
        return f"lengths differ ({len(reconstructed)} vs {len(original)}); one is a prefix of the other"
    return "(strings are identical - unexpected)"


def _find_mid_word_boundaries(plain_text, formatting_requests):
    """Flag any bold/link/italic span whose start or end lands inside a word (a
    letter/digit on both sides of the boundary). The round-trip check above only
    proves this function's own math is internally consistent - it would happily pass
    on malformed markdown that was already broken before it got here (e.g. a link
    wrapping the wrong few characters upstream). This catches that case directly by
    checking the one thing that's never legitimate: styling that splits a word.
    """
    def is_word_char(ch):
        return ch is not None and ch.isalnum()

    problems = []
    for req in formatting_requests:
        style = req.get("updateTextStyle")
        if not style:
            continue
        start = style["range"]["startIndex"]
        end = style["range"]["endIndex"]
        before = plain_text[start - 2] if start - 2 >= 0 else None
        at_start = plain_text[start - 1] if 0 <= start - 1 < len(plain_text) else None
        at_last = plain_text[end - 2] if 0 <= end - 2 < len(plain_text) else None
        after = plain_text[end - 1] if 0 <= end - 1 < len(plain_text) else None
        if is_word_char(before) and is_word_char(at_start):
            problems.append(f"starts mid-word: ...{plain_text[max(0, start - 20):start + 20]!r}...")
        if is_word_char(at_last) and is_word_char(after):
            problems.append(f"ends mid-word: ...{plain_text[max(0, end - 20):end + 20]!r}...")
    return problems


def insert_parsed_text_with_formatting(docs_service, doc_id, review_text):
    # Bullet lines carry a leading "- " (see fix_bullet_points()). Record which lines are
    # bullets before stripping any markup, then strip that marker here - it gets replaced
    # by a real Google Docs bullet glyph below instead of staying as a literal "-" character.
    original_lines = review_text.split('\n')
    bullet_line_flags = [line.startswith('- ') for line in original_lines]
    review_text = '\n'.join(
        line[2:] if is_bullet else line
        for line, is_bullet in zip(original_lines, bullet_line_flags)
    )

    # Parse the text into clean text and extract formatting positions
    plain_text = ""
    formatting_requests = []
    cursor = 1  # Google Docs uses 1-based index after the title

    pattern = (
        r'(?P<bold>\*\*(?P<bold_text>.*?)\*\*)'
        r'|(?P<link>\[(?P<link_text>[^\]]+?)\]\((?P<url>https?://[^\)]+)\))'
        r'|(?P<italic>\*(?P<italic_text>[^\*\n]+?)\*)'
    )
    last_end = 0

    for match in re.finditer(pattern, review_text):
        start, end = match.span()
        before_text = review_text[last_end:start]
        plain_text += before_text
        cursor_start = cursor + len(before_text)

        if match.group('bold') is not None:
            styled_text = match.group('bold_text')
            plain_text += styled_text
            formatting_requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": cursor_start, "endIndex": cursor_start + len(styled_text)},
                    "textStyle": {"bold": True},
                    "fields": "bold"
                }
            })
            cursor += len(before_text) + len(styled_text)

        elif match.group('link') is not None:
            styled_text = match.group('link_text')
            url = match.group('url')
            plain_text += styled_text
            formatting_requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": cursor_start, "endIndex": cursor_start + len(styled_text)},
                    "textStyle": {"link": {"url": url}},
                    "fields": "link"
                }
            })
            cursor += len(before_text) + len(styled_text)

        else:  # Italic (*text*)
            styled_text = match.group('italic_text')
            plain_text += styled_text
            formatting_requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": cursor_start, "endIndex": cursor_start + len(styled_text)},
                    "textStyle": {"italic": True},
                    "fields": "italic"
                }
            })
            cursor += len(before_text) + len(styled_text)

        last_end = end

    remaining_text = review_text[last_end:]
    plain_text += remaining_text

    # Sanity check: replaying the computed ranges against plain_text must reproduce
    # review_text exactly. If it doesn't, the ranges are wrong and would silently apply
    # bold/link/italic styling to the wrong characters once uploaded (mid-word splits).
    # Fail loudly here instead of publishing a corrupted doc.
    reconstructed = _reconstruct_markdown_from_formatting(plain_text, formatting_requests)
    if reconstructed != review_text:
        raise ValueError(
            "Formatting sanity check failed before uploading to Google Docs: replaying "
            "the computed bold/link/italic ranges does not reproduce the source markdown. "
            f"First divergence: {_first_diff_context(reconstructed, review_text)}"
        )

    # Second, independent check: no span may split a word, even if the round-trip above
    # passed (which only proves this function didn't corrupt already-malformed input).
    mid_word_problems = _find_mid_word_boundaries(plain_text, formatting_requests)
    if mid_word_problems:
        raise ValueError(
            "Formatting sanity check failed before uploading to Google Docs: "
            f"{len(mid_word_problems)} bold/link/italic span(s) split a word. "
            + " | ".join(mid_word_problems[:5])
        )

    #  Insert clean plain text first
    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": plain_text}}]}
    ).execute()

    title_line = plain_text.split('\n', 1)[0]
    title_start = 1
    title_end = title_start + len(title_line)

    formatting_requests.insert(0, {
    "updateParagraphStyle": {
        "range": {"startIndex": title_start, "endIndex": title_end},
        "paragraphStyle": {"namedStyleType": "TITLE"},
        "fields": "namedStyleType"
        }
    })

    # Apply inline bold & links
    if formatting_requests:
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": formatting_requests}
        ).execute()

    # Turn bullet lines into real Google Docs bulleted lists (proper glyph + hanging
    # indent) instead of a plain paragraph starting with a literal "-" character.
    # Line count/order is identical between the stripped input text and plain_text
    # (markup stripping never adds/removes lines), so bullet_line_flags still lines up.
    plain_lines = plain_text.split('\n')
    bullet_requests = []
    line_cursor = 1
    run_start = None
    run_end = None
    for i, line in enumerate(plain_lines):
        is_bullet = i < len(bullet_line_flags) and bullet_line_flags[i]
        if is_bullet:
            if run_start is None:
                run_start = line_cursor
            run_end = line_cursor + len(line)
        elif run_start is not None:
            bullet_requests.append({
                "createParagraphBullets": {
                    "range": {"startIndex": run_start, "endIndex": run_end},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"
                }
            })
            run_start = None
        line_cursor += len(line) + 1  # +1 for the '\n' separating lines

    if run_start is not None:
        bullet_requests.append({
            "createParagraphBullets": {
                "range": {"startIndex": run_start, "endIndex": run_end},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"
            }
        })

    if bullet_requests:
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": bullet_requests}
        ).execute()

    doc = docs_service.documents().get(documentId=doc_id).execute()
    header_requests = []
    section_titles = ["Overview", "General", "Payments", "Games", "Responsible Gambling", "Bonuses"]

    for element in doc.get('body', {}).get('content', []):
        if 'paragraph' in element:
            paragraph = element['paragraph']
            paragraph_text = ''.join(
                elem['textRun']['content']
                for elem in paragraph.get('elements', [])
                if 'textRun' in elem
            ).strip()

            # Check if this is a section title
            if paragraph_text in section_titles:
                # Find the exact start and end from element indexes
                start_index = element.get('startIndex')
                end_index = element.get('endIndex')
                if start_index is not None and end_index is not None:
                    header_requests.append({
                        "updateTextStyle": {
                            "range": {"startIndex": start_index, "endIndex": end_index - 1},  # exclude trailing newline
                            "textStyle": {"bold": True, "fontSize": {"magnitude": 16, "unit": "PT"}},
                            "fields": "bold,fontSize"
                        }
                    })

    # Apply section headers formatting
    if header_requests:
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": header_requests}
        ).execute()

def create_google_doc_in_folder(docs_service, drive_service, folder_id, doc_title, review_text):
    doc_id = docs_service.documents().create(body={"title": doc_title}).execute()["documentId"]
    insert_parsed_text_with_formatting(docs_service, doc_id, review_text)

    file = drive_service.files().get(fileId=doc_id, fields="parents").execute()
    previous_parents = ",".join(file.get('parents', []))
    drive_service.files().update(fileId=doc_id, addParents=folder_id, removeParents=previous_parents, fields="id, parents").execute()
    return doc_id

def find_existing_doc(drive_service, folder_id, title):
    query = f"name='{title}' and '{folder_id}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None

def main():
    st.set_page_config(page_title="Merged Review Generator", layout="centered", initial_sidebar_state="collapsed")
    
    # Initialize session state
    if 'review_completed' not in st.session_state:
        st.session_state.review_completed = False
        st.session_state.review_url = None
        st.session_state.casino_name = None
        st.session_state.rewritten_review = None
        st.session_state.awaiting_overview = False
        st.session_state.presentation_plan = {}
    
    # If review is completed and awaiting overview input
    if st.session_state.awaiting_overview and st.session_state.rewritten_review:
        st.markdown(f"## Review Complete! Now add the Overview section for: **{st.session_state.casino_name}**")
        
        # Show the completed review for reference
        with st.expander("📖 View Completed Review (for reference)", expanded=False):
            # Escape $ so Streamlit doesn't parse dollar amounts as LaTeX math delimiters
            st.markdown(st.session_state.rewritten_review.replace("$", "\\$"))
        
        st.markdown("### Add Overview Section")
        st.markdown("Please provide a keyword and main points for the introduction:")

        # Input fields for overview
        keyword = st.text_input("Keyword",
                               placeholder="Enter the keyword")

        main_points = st.text_area("Main Points (2-3 key points to highlight in the overview)",
                                  placeholder="• Strong crypto integration\n• Excellent customer support\n• Wide game variety",
                                  height=120)

        # Generate and display TLDR options
        st.markdown("### TLDR Section")
        st.markdown("Select which TLDR bullet points to include at the bottom of the overview:")

        # Initialize TLDR points in session state if not already done
        if 'tldr_points' not in st.session_state:
            if keyword or main_points:  # Only generate if user has started filling the form
                with st.spinner("🔄 Generating TLDR bullet points..."):
                    st.session_state.tldr_points = generate_tldr_points(st.session_state.rewritten_review)
            else:
                st.session_state.tldr_points = []

        # Show TLDR options with checkboxes if we have points
        selected_tldr_points = []
        if st.session_state.tldr_points:
            st.markdown("**Choose TLDR bullet points to include:**")
            for i, point in enumerate(st.session_state.tldr_points):
                # Escape $ so Streamlit doesn't parse dollar amounts as LaTeX math delimiters
                display_point = point.replace("$", "\\$")
                if st.checkbox(display_point, key=f"tldr_{i}", value=True):  # Default to checked
                    selected_tldr_points.append(point)
        else:
            # Button to generate TLDR points
            if st.button("Generate TLDR Points", type="secondary"):
                with st.spinner("🔄 Generating TLDR bullet points..."):
                    st.session_state.tldr_points = generate_tldr_points(st.session_state.rewritten_review)
                st.rerun()
        
        # Generate overview and finalize
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("Generate Overview & Post to Google Docs", type="primary", disabled=not (keyword and main_points)):
                if keyword and main_points:
                    try:
                        # Generate overview section with selected TLDR points
                        st.info("🔄 Generating Overview section with Adam's voice...")
                        overview_section = generate_overview_section(
                            st.session_state.casino_name,
                            keyword,
                            main_points,
                            selected_tldr_points if selected_tldr_points else None,
                            st.session_state.presentation_plan.get("Overview", "")
                        )
                        
                        # Combine overview with the rest of the review - Overview goes first
                        title_line = f"{st.session_state.casino_name} review"
                        final_review = f"{title_line}\n\n{overview_section}\n\n{st.session_state.rewritten_review}"

                        # Fix bullet points before uploading
                        final_review = fix_bullet_points(final_review)

                        # Add internal links: casinos (live sitemap), cryptocurrencies, and topical pages
                        st.info("🔗 Adding internal links...")
                        final_review = link_casino_mentions(
                            final_review,
                            st.session_state.casino_name
                        )
                        final_review = link_crypto_mentions(final_review)
                        final_review = link_topical_page_mentions(final_review)

                        # Post to Google Docs
                        st.info("📤 Uploading to Google Drive...")
                        user_creds = get_service_account_credentials()
                        docs_service = build("docs", "v1", credentials=user_creds)
                        drive_service = build("drive", "v3", credentials=user_creds)
                        
                        doc_title = f"{st.session_state.casino_name} Review"
                        existing_doc_id = find_existing_doc(drive_service, FOLDER_ID, doc_title)

                        if existing_doc_id:
                            drive_service.files().delete(fileId=existing_doc_id).execute()

                        doc_id = create_google_doc_in_folder(docs_service, drive_service, FOLDER_ID, doc_title, final_review)
                        doc_url = f"https://docs.google.com/document/d/{doc_id}"
                        
                        # Write the review link to the spreadsheet
                        write_review_link_to_sheet(doc_url)
                        
                        # Mark as completed
                        st.session_state.review_completed = True
                        st.session_state.review_url = doc_url
                        st.session_state.awaiting_overview = False
                        st.session_state.rewritten_review = None
                        if 'tldr_points' in st.session_state:
                            del st.session_state.tldr_points

                        st.rerun()
                        
                    except Exception as e:
                        st.error(f"❌ Error finalizing review: {e}")
        
        with col2:
            if st.button("Skip Overview (Post without Overview)", type="secondary"):
                try:
                    # Post to Google Docs without overview - using exact original workflow
                    st.info("📤 Uploading to Google Drive...")
                    user_creds = get_service_account_credentials()
                    docs_service = build("docs", "v1", credentials=user_creds)
                    drive_service = build("drive", "v3", credentials=user_creds)
                    
                    doc_title = f"{st.session_state.casino_name} Review"
                    existing_doc_id = find_existing_doc(drive_service, FOLDER_ID, doc_title)

                    if existing_doc_id:
                        drive_service.files().delete(fileId=existing_doc_id).execute()

                    # Use original review format - exactly as it was before
                    final_review = f"{st.session_state.casino_name} review\n\n{st.session_state.rewritten_review}"

                    # Fix bullet points before uploading
                    final_review = fix_bullet_points(final_review)

                    # Add internal links: casinos (live sitemap), cryptocurrencies, and topical pages
                    st.info("🔗 Adding internal links...")
                    final_review = link_casino_mentions(
                        final_review,
                        st.session_state.casino_name
                    )
                    final_review = link_crypto_mentions(final_review)
                    final_review = link_topical_page_mentions(final_review)

                    doc_id = create_google_doc_in_folder(docs_service, drive_service, FOLDER_ID, doc_title, final_review)
                    doc_url = f"https://docs.google.com/document/d/{doc_id}"
                    
                    # Write the review link to the spreadsheet
                    write_review_link_to_sheet(doc_url)
                    
                    # Mark as completed
                    st.session_state.review_completed = True
                    st.session_state.review_url = doc_url
                    st.session_state.awaiting_overview = False
                    st.session_state.rewritten_review = None
                    if 'tldr_points' in st.session_state:
                        del st.session_state.tldr_points

                    st.rerun()
                    
                except Exception as e:
                    st.error(f"❌ Error posting review: {e}")
        
        return
    
    # If review is already completed, show the success message
    if st.session_state.review_completed:
        st.success("Review successfully written & rewritten with Adam's voice, check the sheet :)")
        if st.session_state.review_url:
            st.info(f"Review link: {st.session_state.review_url}")
        
        # Add a button to generate a new review
        if st.button("Write New Review", type="primary"):
            st.session_state.review_completed = False
            st.session_state.review_url = None
            st.session_state.casino_name = None
            st.session_state.rewritten_review = None
            st.session_state.awaiting_overview = False
            st.session_state.presentation_plan = {}
            if 'tldr_points' in st.session_state:
                del st.session_state.tldr_points
            st.rerun()
        return
    
    # Get casino name first to show in the interface
    try:
        user_creds = get_service_account_credentials()
        casino, _, _ = get_cached_casino_data()
        st.session_state.casino_name = casino
    except Exception as e:
        st.error(f"❌ Error loading casino data: {e}")
        return
    
    # Show casino name and generate button
    st.markdown(f"## Ready to write a review for: **{casino}**")
    st.markdown("The review will be written and then rewritten in Adam's voice before upload.")
    
    # Only generate review when button is clicked
    if st.button("Write Review", type="primary", use_container_width=True):
        # Show progress message
        progress_placeholder = st.empty()
        progress_placeholder.markdown("## Writing review, please wait...")
        
        try:
            docs_service = build("docs", "v1", credentials=user_creds)
            drive_service = build("drive", "v3", credentials=user_creds)

            # Load all data in parallel
            progress_placeholder.markdown("## Loading templates and data...")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                # Submit all data loading tasks
                templates_future = executor.submit(get_all_templates)
                casino_data_future = executor.submit(get_cached_casino_data)
                btc_future = executor.submit(
                    lambda: requests.get(
                        "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
                        headers={"Accepts": "application/json", "X-CMC_PRO_API_KEY": COINMARKETCAP_API_KEY},
                        params={"symbol": "BTC", "convert": "USD"}
                    ).json().get("data", {}).get("BTC", {}).get("quote", {}).get("USD", {}).get("price")
                )
                
                # Collect results
                templates = templates_future.result()
                casino, secs, comments = casino_data_future.result()
                price = btc_future.result()
            
            btc_str = f"1 BTC = ${price:,.2f}" if price else "[BTC price unavailable]"
            
            # Check if all required templates were loaded
            required_templates = ['PromptTemplate', 'BaseGuidelinesClaude', 'BaseGuidelinesResponsible']
            missing_templates = [t for t in required_templates if not templates.get(t)]
            if missing_templates:
                st.error(f"Error: Could not fetch required templates: {', '.join(missing_templates)}")
                return
            
            # Sort comments by section AND generate this run's Presentation Plan in parallel -
            # neither depends on the other, both are single blocking Claude calls.
            progress_placeholder.markdown("## Sorting comments & planning this review's structure...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                sorted_comments_future = executor.submit(sort_comments_by_section, comments)
                presentation_plan_future = executor.submit(generate_presentation_plan, casino)
                sorted_comments = sorted_comments_future.result()
                presentation_plan = presentation_plan_future.result()

            # Cache this run's Presentation Plan so the later, separate Overview step
            # (awaiting_overview branch) can reuse the same stylistic opening-hook direction.
            st.session_state.presentation_plan = presentation_plan

            # Generate all sections in parallel
            progress_placeholder.markdown("## Generating review sections in parallel...")
            parallel_results = generate_sections_parallel(casino, secs, sorted_comments, templates, btc_str, presentation_plan)

            # Stop immediately if any section failed to generate - letting a failed
            # section through would have the rewrite step fabricate content for it
            failed_sections = [r for r in parallel_results if "\n[Error" in r]
            if failed_sections:
                progress_placeholder.empty()
                st.error(f"❌ {len(failed_sections)} section(s) failed to generate - aborting before rewrite:\n\n" + "\n\n".join(failed_sections))
                return

            # Combine results
            out = [f"{casino} review\n"] + parallel_results

            # Step 2: Rewrite with Adam's voice
            progress_placeholder.markdown("## Rewriting with Adam's voice...")

            initial_review = "\n".join(out)

            rewritten_review = rewrite_review_with_adam(initial_review, presentation_plan)

            # Step 3: Store rewritten review, then prompt for Overview input
            st.session_state.rewritten_review = rewritten_review
            st.session_state.awaiting_overview = True
            st.session_state.casino_name = casino
            
            # Clear progress message and show overview input screen
            progress_placeholder.empty()
            st.rerun()

        except Exception as e:
            progress_placeholder.empty()
            st.error(f"❌ An error occurred: {e}")

if __name__ == "__main__":
    main()