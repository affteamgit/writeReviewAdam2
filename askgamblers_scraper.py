"""
AskGamblers Scraper Module
Scrapes casino reviews from AskGamblers using Playwright headless browser
to bypass anti-bot protection.
"""

import subprocess
import sys
import re
from datetime import datetime, timedelta
from typing import List, Dict


def _install_chromium():
    """Install Playwright Chromium browser binary if not already present."""
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True,
            capture_output=True,
            timeout=120,
        )
        print("Playwright Chromium installed successfully")
    except Exception as e:
        print(f"Failed to install Playwright Chromium: {e}")
        raise


class AskGamblersScraper:
    BASE_URL = "https://www.askgamblers.com"

    def __init__(self, timeout: int = 10):
        self.timeout = timeout * 1000  # Playwright uses milliseconds
        self._playwright = None
        self._browser = None
        self._context = None

    def _ensure_browser(self):
        if self._browser is not None:
            return

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()

        try:
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as e:
            print(f"Browser launch failed ({e}), installing Chromium...")
            _install_chromium()
            self._browser = self._playwright.chromium.launch(headless=True)

        self._context = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )

    def _cleanup(self):
        if self._context:
            self._context.close()
            self._context = None
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def search_casino(self, casino_name: str) -> List[Dict]:
        try:
            self._ensure_browser()

            search_term = (
                re.sub(r"\.(com|io|net|org|casino)$", "", casino_name.lower())
                .replace(" ", "-")
                .replace(".", "-")
            )
            # Remove any non-alphanumeric chars except hyphens (mirrors extension)
            search_term = re.sub(r"[^a-z0-9-]", "", search_term)

            possible_urls = [
                f"{self.BASE_URL}/online-casinos/reviews/{search_term}-casino",
                f"{self.BASE_URL}/online-casinos/reviews/{search_term}",
                f"{self.BASE_URL}/reviews/{search_term}-casino",
                f"{self.BASE_URL}/reviews/{search_term}",
            ]

            print(f"Searching AskGamblers for: {casino_name}")

            page = self._context.new_page()
            try:
                for url in possible_urls:
                    try:
                        print(f"Trying URL: {url}")
                        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
                        page.wait_for_timeout(3000)

                        # Current site uses #reviews section with div[id^="review-"] cards
                        review_section = page.query_selector("#reviews")
                        if review_section:
                            print(f"Found working URL: {url}")
                            return [{"url": url, "name": search_term, "matches": True}]
                        else:
                            print(f"No #reviews section at {url}")

                    except Exception as e:
                        print(f"Failed to load {url}: {str(e)[:100]}")
                        continue
            finally:
                page.close()

            print("Could not find casino page with any URL pattern")
            return []

        except Exception as e:
            print(f"Error searching for casino: {e}")
            return []

    def scrape_casino_reviews(
        self,
        casino_name: str,
        max_reviews: int = 50,
        months: int = 6,
    ) -> Dict:
        print(f"\n{'='*60}")
        print(f"Starting AskGamblers scrape for: {casino_name}")
        print(f"{'='*60}\n")

        try:
            search_results = self.search_casino(casino_name)

            if not search_results:
                return {
                    "casino_name": casino_name,
                    "reviews": [],
                    "total_count": 0,
                    "error": "No results found on AskGamblers",
                }

            target_url = search_results[0]["url"]

            self._ensure_browser()
            page = self._context.new_page()

            try:
                print(f"Navigating to {target_url}")
                page.goto(target_url, wait_until="domcontentloaded", timeout=self.timeout)
                page.wait_for_timeout(3000)

                review_section = page.query_selector("#reviews")
                if not review_section:
                    print("Review section not found on page")
                    return {
                        "casino_name": casino_name,
                        "reviews": [],
                        "total_count": 0,
                        "askgamblers_url": target_url,
                        "error": "Review section not found",
                    }

                # Reviews are div elements with id="review-<hash>"
                review_cards = page.query_selector_all("div[id^='review-']")
                print(f"Found {len(review_cards)} review cards")

                all_reviews = []

                for idx, card in enumerate(review_cards):
                    if len(all_reviews) >= max_reviews:
                        break

                    try:
                        # Date -- secondary color span inside user info
                        date_elem = card.query_selector("span.body-compact-01.text--secondary-color")
                        if not date_elem:
                            continue
                        date_str = date_elem.inner_text().strip()

                        if not self.is_review_recent(date_str, months):
                            continue

                        # Rating -- heading-compact-02 inside .review__user-rating (out of 10)
                        rating = None
                        rating_elem = card.query_selector(".review__user-rating .heading-compact-02")
                        if rating_elem:
                            try:
                                rating = float(rating_elem.inner_text().strip())
                            except ValueError:
                                pass

                        # Author -- primary color span inside user info
                        author = "Anonymous"
                        author_elem = card.query_selector("span.heading-compact-01.text--primary-color")
                        if author_elem:
                            author = author_elem.inner_text().strip()

                        # Text -- pros/cons identified by thumbs-up/down icons
                        # Each .review__comment-wrapper has an icon followed by a <p>
                        text = ""
                        title = ""

                        comment_wrappers = card.query_selector_all(".review__comment-wrapper")
                        for wrapper in comment_wrappers:
                            has_thumbs_up = wrapper.query_selector(".icon-aph-thumbs-up")
                            has_thumbs_down = wrapper.query_selector(".icon-aph-thumbs-down")
                            text_elem = wrapper.query_selector("p.review__comment-text")

                            if text_elem:
                                comment_text = text_elem.inner_text().strip()
                                if not comment_text:
                                    continue

                                if has_thumbs_up:
                                    prefix = "Pros: "
                                elif has_thumbs_down:
                                    prefix = "Cons: "
                                else:
                                    prefix = ""

                                if text:
                                    text += "\n\n"
                                text += f"{prefix}{comment_text}"

                        if text or title:
                            all_reviews.append(
                                {
                                    "date": date_str,
                                    "rating": rating,
                                    "title": title,
                                    "text": text,
                                    "author": author,
                                    "full_content": f"{title} {text}".strip(),
                                }
                            )

                    except Exception as e:
                        print(f"Error parsing review card {idx}: {e}")
                        continue

            finally:
                page.close()

        finally:
            self._cleanup()

        all_reviews = all_reviews[:max_reviews]

        result = {
            "casino_name": casino_name,
            "reviews": all_reviews,
            "total_count": len(all_reviews),
            "askgamblers_url": target_url,
            "scraped_at": datetime.now().isoformat(),
        }

        print(f"\n{'='*60}")
        print(f"Scraping complete: {len(all_reviews)} reviews collected")
        print(f"{'='*60}\n")

        return result

    def is_review_recent(self, date_str: str, months: int = 6) -> bool:
        try:
            cutoff_date = datetime.now() - timedelta(days=months * 30)

            date_formats = [
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d",
                "%B %d, %Y",
                "%b %d, %Y",
                "%d %B %Y",
                "%d %b %Y",
                "%d/%m/%Y",
                "%m/%d/%Y",
            ]

            lower_date = date_str.lower()
            if any(
                word in lower_date
                for word in ["today", "yesterday", "day ago", "days ago"]
            ):
                return True
            if "hour" in lower_date or "minute" in lower_date:
                return True
            if "week" in lower_date:
                weeks = (
                    int(re.search(r"\d+", date_str).group())
                    if re.search(r"\d+", date_str)
                    else 1
                )
                return weeks <= (months * 4)
            if "month" in lower_date:
                mons = (
                    int(re.search(r"\d+", date_str).group())
                    if re.search(r"\d+", date_str)
                    else 1
                )
                return mons <= months

            review_date = None
            for fmt in date_formats:
                try:
                    review_date = datetime.strptime(date_str.strip(), fmt)
                    break
                except ValueError:
                    continue

            if review_date:
                return review_date >= cutoff_date

            return False

        except Exception as e:
            print(f"Error parsing date '{date_str}': {e}")
            return False

    def analyze_withdrawal_mentions(self, reviews: List[Dict]) -> Dict:
        withdrawal_keywords = [
            "withdrawal", "withdraw", "payout", "cashout", "cash out",
            "payment", "pay out", "pending", "processing", "waiting",
        ]

        withdrawal_reviews = []
        positive_mentions = []
        negative_mentions = []

        for review in reviews:
            content = review.get("full_content", "").lower()

            if any(keyword in content for keyword in withdrawal_keywords):
                withdrawal_reviews.append(review)

                rating = review.get("rating", 0)
                if rating is None:
                    rating = 0

                is_positive = rating >= 7 or any(
                    word in content
                    for word in ["instant", "fast", "quick", "immediately", "smooth"]
                )
                is_negative = rating <= 4 or any(
                    word in content
                    for word in ["slow", "delayed", "never", "still waiting", "pending"]
                )

                if is_positive and not is_negative:
                    positive_mentions.append(review)
                elif is_negative:
                    negative_mentions.append(review)

        return {
            "total_withdrawal_mentions": len(withdrawal_reviews),
            "positive_count": len(positive_mentions),
            "negative_count": len(negative_mentions),
            "reviews": withdrawal_reviews,
            "positive_reviews": positive_mentions,
            "negative_reviews": negative_mentions,
            "has_sufficient_data": len(withdrawal_reviews) >= 10,
        }

    def extract_player_experiences(
        self, reviews: List[Dict], section_keywords: Dict[str, List[str]]
    ) -> Dict:
        experiences = {}

        for section, keywords in section_keywords.items():
            matching_reviews = []

            for review in reviews:
                content = review.get("full_content", "").lower()

                if any(keyword.lower() in content for keyword in keywords):
                    matching_reviews.append(
                        {
                            "rating": review.get("rating"),
                            "date": review.get("date"),
                            "title": review.get("title"),
                            "text": review.get("text"),
                        }
                    )

            experiences[section] = {
                "count": len(matching_reviews),
                "reviews": matching_reviews,
                "has_sufficient_data": len(matching_reviews) >= 10,
            }

        return experiences


def format_review_summary(review_data: Dict, include_details: bool = True) -> str:
    if not review_data or review_data.get("total_count", 0) == 0:
        return f"No recent reviews found on AskGamblers for {review_data.get('casino_name', 'this casino')}."

    summary_parts = []

    summary_parts.append(f"AskGamblers Player Reviews for {review_data['casino_name']}")
    summary_parts.append(f"Total recent reviews: {review_data['total_count']}")
    summary_parts.append(f"Source: {review_data.get('askgamblers_url', 'N/A')}")
    summary_parts.append("")

    if review_data["total_count"] < 10:
        summary_parts.append(
            "NOTE: Less than 10 reviews available. Take this data with a grain of salt."
        )
        summary_parts.append("")

    if include_details and review_data.get("reviews"):
        summary_parts.append("Player Comments:")
        for idx, review in enumerate(review_data["reviews"][:20], 1):
            rating_str = (
                f"{review.get('rating', 'N/A')}" if review.get("rating") else "N/A"
            )
            summary_parts.append(f"\n{idx}. [{rating_str}/10] {review.get('date', 'N/A')}")
            if review.get("title"):
                summary_parts.append(f"   Title: {review['title']}")
            if review.get("text"):
                text_preview = review["text"][:300]
                if len(review["text"]) > 300:
                    text_preview += "..."
                summary_parts.append(f"   Review: {text_preview}")

    return "\n".join(summary_parts)
