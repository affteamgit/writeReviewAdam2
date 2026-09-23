"""
Trustpilot Scraper Module
Scrapes casino reviews from Trustpilot using Playwright headless browser.
Extracts review data from the __NEXT_DATA__ JSON embedded in each page.
"""

import json
import subprocess
import sys
import re
from datetime import datetime, timedelta
from typing import List, Dict

from askgamblers_scraper import _install_chromium


class TrustpilotScraper:
    BASE_URL = "https://www.trustpilot.com"

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

    def _extract_reviews_from_json(self, page, months: int = 6) -> List[Dict]:
        """Extract reviews from __NEXT_DATA__ JSON embedded in the page."""
        js = """() => {
            const el = document.querySelector('script#__NEXT_DATA__');
            if (!el) return null;
            const data = JSON.parse(el.textContent);
            return data.props.pageProps.reviews || [];
        }"""
        try:
            reviews_data = page.evaluate(js)
        except Exception as e:
            print(f"Failed to extract __NEXT_DATA__: {e}")
            return []

        if not reviews_data:
            return []

        reviews = []
        for review_json in reviews_data:
            try:
                date_str = review_json.get("dates", {}).get("publishedDate", "")
                if not date_str:
                    continue

                if not self.is_review_recent(date_str, months):
                    continue

                text = review_json.get("text", "").strip()
                title = review_json.get("title", "").strip()
                rating = review_json.get("rating")
                consumer = review_json.get("consumer", {})
                author = consumer.get("displayName", "Anonymous")

                if text or title:
                    reviews.append({
                        "date": date_str,
                        "rating": rating,
                        "title": title,
                        "text": text,
                        "author": author,
                        "full_content": f"{title} {text}".strip(),
                    })

            except Exception as e:
                print(f"Error parsing Trustpilot review: {e}")
                continue

        return reviews

    def _build_domain(self, casino_name: str) -> str:
        """Build a Trustpilot domain slug from a casino name."""
        clean = casino_name.lower().strip()
        # If it already looks like a domain, use as-is
        if "." in clean:
            return clean
        # Otherwise try common extensions
        return clean

    def search_casino(self, casino_name: str) -> List[Dict]:
        try:
            self._ensure_browser()

            clean = casino_name.lower().strip()
            # Build possible domain slugs
            if "." in clean:
                domains = [clean]
            else:
                domains = [
                    f"{clean}.com",
                    f"{clean}.io",
                    f"{clean}.net",
                    clean,
                ]

            print(f"Searching Trustpilot for: {casino_name}")

            page = self._context.new_page()
            try:
                for domain in domains:
                    url = f"{self.BASE_URL}/review/{domain}"
                    try:
                        print(f"Trying URL: {url}")
                        page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
                        page.wait_for_timeout(2000)

                        # Check if it's a valid review page (has __NEXT_DATA__ with reviews)
                        has_reviews = page.evaluate("""() => {
                            const el = document.querySelector('script#__NEXT_DATA__');
                            if (!el) return false;
                            try {
                                const data = JSON.parse(el.textContent);
                                const reviews = data.props.pageProps.reviews;
                                return reviews && reviews.length > 0;
                            } catch(e) { return false; }
                        }""")

                        if has_reviews:
                            print(f"Found working URL: {url}")
                            return [{"url": url, "domain": domain, "matches": True}]
                        else:
                            print(f"No reviews at {url}")

                    except Exception as e:
                        print(f"Failed to load {url}: {str(e)[:100]}")
                        continue
            finally:
                page.close()

            print("Could not find casino on Trustpilot")
            return []

        except Exception as e:
            print(f"Error searching Trustpilot: {e}")
            return []

    def scrape_casino_reviews(
        self,
        casino_name: str,
        max_reviews: int = 50,
        months: int = 6,
    ) -> Dict:
        print(f"\n{'='*60}")
        print(f"Starting Trustpilot scrape for: {casino_name}")
        print(f"{'='*60}\n")

        try:
            search_results = self.search_casino(casino_name)

            if not search_results:
                return {
                    "casino_name": casino_name,
                    "reviews": [],
                    "total_count": 0,
                    "error": "No results found on Trustpilot",
                }

            target_url = search_results[0]["url"]

            self._ensure_browser()
            all_reviews = []
            page_num = 1

            page = self._context.new_page()
            try:
                while len(all_reviews) < max_reviews:
                    url = target_url if page_num == 1 else f"{target_url}?page={page_num}"
                    print(f"Scraping page {page_num}: {url}")

                    page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
                    page.wait_for_timeout(2000)

                    reviews = self._extract_reviews_from_json(page, months)
                    print(f"Found {len(reviews)} recent reviews on page {page_num}")

                    if not reviews:
                        break

                    all_reviews.extend(reviews)

                    # Trustpilot shows 20 reviews per page. If all 20 were recent
                    # and we haven't hit the limit, check the next page.
                    if len(reviews) >= 18 and len(all_reviews) < max_reviews:
                        page_num += 1
                    else:
                        break

            finally:
                page.close()

        finally:
            self._cleanup()

        all_reviews = all_reviews[:max_reviews]

        result = {
            "casino_name": casino_name,
            "reviews": all_reviews,
            "total_count": len(all_reviews),
            "trustpilot_url": target_url,
            "scraped_at": datetime.now().isoformat(),
        }

        print(f"\n{'='*60}")
        print(f"Trustpilot scraping complete: {len(all_reviews)} reviews collected")
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
            ]

            lower_date = date_str.lower()
            if "day" in lower_date or "hour" in lower_date or "minute" in lower_date:
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

                is_positive = rating >= 4 or any(
                    word in content
                    for word in ["instant", "fast", "quick", "immediately", "smooth"]
                )
                is_negative = rating <= 2 or any(
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
                    matching_reviews.append({
                        "rating": review.get("rating"),
                        "date": review.get("date"),
                        "title": review.get("title"),
                        "text": review.get("text"),
                    })

            experiences[section] = {
                "count": len(matching_reviews),
                "reviews": matching_reviews,
                "has_sufficient_data": len(matching_reviews) >= 10,
            }

        return experiences
