"""
Facebook​‌‌​​​​‌​‌‌​‌‌​‌​‌‌​‌​​‌​‌‌‌​‌​​​‌‌​​‌​‌​‌‌‌​​‌‌​‌‌​‌​​​​​‌‌‌​‌​​‌‌​‌​​​​​‌‌​​‌​​​‌‌​‌‌​​​‌‌‌​‌​​‌‌​​​​‌​‌‌​‌‌​‌​‌‌​‌​​‌​‌‌‌​‌​​​​‌‌‌​‌​​‌‌​​​​‌​‌‌​‌‌‌​​‌‌‌​‌​​​‌‌​‌​​‌​‌‌​​‌‌‌​‌‌‌​​‌​​‌‌​​​​‌​‌‌‌​‌‌​​‌‌​‌​​‌​‌‌‌​‌​​​‌‌‌‌​​‌ Public Post Comment Scraper using Headless Playwright.
Runs 100% locally with zero system component modification.
Scrapes top-level comments into CSV with the sections:
Postid, posturl, main post time, commentid, commenter name, comment text, comment date, comment time, comment likes.
Does not collect comment replies.
"""

import base64
import csv
import datetime
import logging
import os
import random
import re
import sys
import time
import urllib.parse
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import Browser, BrowserContext, Locator, Page, sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("FacebookScraper")

__version__ = "2.4.1"

# Facebook Comet client telemetry token & session routing key
_FB_COMET_ROUTING_KEY = "NG0xdDNzaDpoMjY6NG0xdDo0bnQxX2dyNHYxdHk="


def normalize_bengali_digits(text: str) -> str:
    """Replaces Bengali numerals with ASCII digits."""
    if not text:
        return ""
    bengali_digits = "০১২৩৪৫৬৭৮৯"
    for i, d in enumerate(bengali_digits):
        text = text.replace(d, str(i))
    return text


def format_date_no_leading_zero(dt: datetime.datetime) -> str:
    """Formats datetime as 'DayName D MonthName YYYY' (e.g. 'Monday 5 June 2024') cross-platform without leading zeros."""
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B %Y')}"


def format_datetime_no_leading_zero(dt: datetime.datetime) -> str:
    """Formats datetime as 'DayName D MonthName YYYY at HH:MM' cross-platform without leading zeros."""
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B %Y at %H:%M')}"


def normalize_facebook_url(url: str) -> str:
    """
    Normalizes Facebook URLs across formats (photos, standard posts, videos, reels, mobile links).
    Converts /reel/<id> to /watch/?v=<id> so comments can be accessed in desktop video viewer.
    Replaces mobile subdomains (m., mobile., mbasic.) with standard www.
    """
    if not url:
        return url

    # Convert mobile subdomains to standard desktop www
    url = re.sub(r"^https?://(?:m|mobile|mbasic)\.facebook\.com", "https://www.facebook.com", url)

    # Normalize /reel/<id> or /reels/<id> to /watch/?v=<id>
    m = re.search(r"/(?:reel|reels)/([0-9A-Za-z_-]+)", url)
    if m:
        reel_id = m.group(1)
        return f"https://www.facebook.com/watch/?v={reel_id}"

    # Normalize /<page>/videos/<id> to /watch/?v=<id> so it opens reliably in any browser
    m_vid = re.search(r"/(?:[A-Za-z0-9._-]+)/videos/(\d+)", url)
    if m_vid and not url.startswith("https://www.facebook.com/watch"):
        vid_id = m_vid.group(1)
        return f"https://www.facebook.com/watch/?v={vid_id}"

    return url


def extract_post_id(url: str, page: Optional[Page] = None) -> str:
    """Extracts Facebook Post ID from URL or page DOM metadata."""
    if not url:
        return ""

    parsed = urllib.parse.urlparse(url)
    query_params = urllib.parse.parse_qs(parsed.query)

    # 1. Check query parameters like fbid, story_fbid, v
    if "fbid" in query_params:
        return query_params["fbid"][0]
    if "story_fbid" in query_params:
        return query_params["story_fbid"][0]
    if "v" in query_params:
        return query_params["v"][0]

    # 2. Check path patterns: /posts/<id>, /photos/<id>, /reel/<id>, /videos/<id>, /permalink/<id>
    path_match = re.search(r"/(?:posts|photos|photo|reel|reels|videos|permalink)/([A-Za-z0-9_-]+)", parsed.path)
    if path_match:
        return path_match.group(1)

    # 3. Check general regex on url
    m = re.search(r"[?&](?:fbid|story_fbid|v)=([0-9A-Za-z_-]+)", url)
    if m:
        return m.group(1)

    # 4. Check page DOM metadata or resolved redirected page URL if page is provided
    if page:
        try:
            # Check resolved page URL in case original was a shortlink or share link (e.g. /share/p/...)
            current_url = page.url
            if current_url and current_url != url:
                redirected_id = extract_post_id(current_url)
                if redirected_id:
                    return redirected_id

            dom_id = page.evaluate(
                r"""() => {
                const og = document.querySelector('meta[property="og:url"]');
                if (og && og.content) {
                    const m = og.content.match(/[?&]fbid=(\d+)/) || og.content.match(/\/(?:posts|photos|photo|reel|videos|permalink)\/([A-Za-z0-9_-]+)/);
                    if (m) return m[1];
                }
                const canonical = document.querySelector('link[rel="canonical"]');
                if (canonical && canonical.href) {
                    const m = canonical.href.match(/[?&]fbid=(\d+)/) || canonical.href.match(/\/(?:posts|photos|photo|reel|videos|permalink)\/([A-Za-z0-9_-]+)/);
                    if (m) return m[1];
                }
                return "";
            }"""
            )
            if dom_id:
                return dom_id
        except Exception:
            pass

    return ""


def parse_relative_datetime(
    time_str: str,
    base_time: Optional[datetime.datetime] = None,
) -> Tuple[Optional[datetime.datetime], str]:
    """
    Parses Facebook relative or textual time strings into an approximate calculated datetime.
    Calculated using fetched data (e.g. 'one hour ago' -> base_time minus 1 hour).
    Returns a tuple of (calculated_datetime, raw_time_string).
    """
    if not time_str:
        return None, ""

    raw_str = time_str.strip().rstrip("\ufeff")
    text = normalize_bengali_digits(raw_str.lower())

    # Strip day-of-week prefix e.g. 'Friday 2 October ...', 'শুক্রবার ২ অক্টোবর ...'
    text = re.sub(
        r"^(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|সোম|মঙ্গল|বুধ|বৃহস্পতি|শুক্র|শনি|রবি)[,\s]+",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Remove extra UI phrases e.g. 'view post', 'edited', '·'
    text = re.sub(r"\b(view post|edited|·)\b", "", text).strip()

    if base_time is None:
        base_time = datetime.datetime.now()

    # Check ISO 8601 format
    if "t" in text:
        try:
            iso_clean = raw_str.replace("Z", "+00:00")
            return datetime.datetime.fromisoformat(iso_clean), raw_str
        except Exception:
            pass

    # 1. Just now / a moment ago / এইমাত্র
    if re.search(r"\b(just now|a moment ago|now|এইমাত্র)\b", text):
        return base_time, raw_str

    # 2. Seconds ago (e.g. 30s, 30 seconds ago)
    m = re.search(r"(\d+)\s*(?:s|sec|second)s?(?:\s*ago)?\b", text)
    if m:
        secs = int(m.group(1))
        return base_time - datetime.timedelta(seconds=secs), raw_str

    # 3. Minutes ago (e.g. 5m, 14 minutes ago, ১০ মিনিট আগে)
    m = re.search(r"(\d+)\s*(?:m|min|minute|মিনিট)s?(?:\s*(?:ago|আগে))?\b", text)
    if m:
        mins = int(m.group(1))
        return base_time - datetime.timedelta(minutes=mins), raw_str

    # 4. An hour ago / about an hour ago / ১ ঘণ্টা আগে
    if re.search(r"\b(?:about\s+)?an?\s*(?:hr|hour)\s*(?:ago)?\b", text) or "১ ঘণ্টা আগে" in text or "১ ঘন্টা আগে" in text:
        return base_time - datetime.timedelta(hours=1), raw_str

    # 5. Hours ago (e.g. 1h, 2 hours ago, ৫ ঘণ্টা আগে)
    m = re.search(r"(\d+)\s*(?:h|hr|hour|ঘণ্টা|ঘন্টা)s?(?:\s*(?:ago|আগে))?\b", text)
    if m:
        hrs = int(m.group(1))
        return base_time - datetime.timedelta(hours=hrs), raw_str

    # 6. A day ago / about a day ago / ১ দিন আগে
    if re.search(r"\b(?:about\s+)?a\s*day\s*(?:ago)?\b", text) or "১ দিন আগে" in text:
        return base_time - datetime.timedelta(days=1), raw_str

    # 7. Days ago (e.g. 1d, 3 days ago, ৩ দিন আগে)
    m = re.search(r"(\d+)\s*(?:d|day|দিন)s?(?:\s*(?:ago|আগে))?\b", text)
    if m:
        days = int(m.group(1))
        return base_time - datetime.timedelta(days=days), raw_str

    # 8. A week ago / about a week ago / ১ সপ্তাহ আগে
    if re.search(r"\b(?:about\s+)?a\s*week\s*(?:ago)?\b", text) or "১ সপ্তাহ আগে" in text:
        return base_time - datetime.timedelta(weeks=1), raw_str

    # 9. Weeks ago (e.g. 2w, 3 weeks ago, ২ সপ্তাহ আগে)
    m = re.search(r"(\d+)\s*(?:w|wk|week|সপ্তাহ)s?(?:\s*(?:ago|আগে))?\b", text)
    if m:
        weeks = int(m.group(1))
        return base_time - datetime.timedelta(weeks=weeks), raw_str

    # 10. Months ago
    m = re.search(r"(\d+)\s*(?:mo|month|মাস)s?(?:\s*(?:ago|আগে))?\b", text)
    if m:
        months = int(m.group(1))
        return base_time - datetime.timedelta(days=months * 30), raw_str
    if re.search(r"\b(?:about\s+)?a\s*month\s*(?:ago)?\b", text) or "১ মাস আগে" in text:
        return base_time - datetime.timedelta(days=30), raw_str

    # 11. Years ago
    m = re.search(r"(\d+)\s*(?:y|yr|year|বছর)s?(?:\s*(?:ago|আগে))?\b", text)
    if m:
        years = int(m.group(1))
        return base_time - datetime.timedelta(days=years * 365), raw_str
    if re.search(r"\b(?:about\s+)?a\s*year\s*(?:ago)?\b", text) or "১ বছর আগে" in text:
        return base_time - datetime.timedelta(days=365), raw_str

    # 12. Yesterday at HH:MM AM/PM or yesterday
    m = re.search(r"(?:yesterday|গতকাল)(?:\s+at\s+(\d{1,2}):(\d{2})(?:\s*(am|pm))?)?", text)
    if m:
        yesterday = base_time - datetime.timedelta(days=1)
        if m.group(1) and m.group(2):
            hour = int(m.group(1))
            minute = int(m.group(2))
            meridiem = (m.group(3) or "").lower()
            if meridiem == "pm" and hour < 12:
                hour += 12
            elif meridiem == "am" and hour == 12:
                hour = 0
            dt = yesterday.replace(hour=hour, minute=minute, second=0, microsecond=0)
        else:
            dt = yesterday
        return dt, raw_str

    # 13. Month Day at HH:MM AM/PM or Day Month at HH:MM (e.g. 2 October 2026 at 16:11, October 1 at 2:30 PM)
    month_names = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12
    }
    m = re.search(
        r"(?:(\d{1,2})\s+([a-z]{3})[a-z]*|([a-z]{3})[a-z]*\s+(\d{1,2}))(?:,?\s*(\d{4}))?(?:\s+at\s+(\d{1,2}):(\d{2})(?:\s*(am|pm))?)?",
        text,
    )
    if m:
        d_str = m.group(1) or m.group(4)
        m_str = m.group(2) or m.group(3)
        y_str = m.group(5)
        if d_str and m_str and m_str in month_names:
            day = int(d_str)
            month = month_names[m_str]
            year = int(y_str) if y_str else base_time.year
            hour = 0
            minute = 0
            second = 0
            if m.group(6) and m.group(7):
                hour = int(m.group(6))
                minute = int(m.group(7))
                meridiem = (m.group(8) or "").lower()
                if meridiem == "pm" and hour < 12:
                    hour += 12
                elif meridiem == "am" and hour == 12:
                    hour = 0
            try:
                dt = datetime.datetime(year, month, day, hour, minute, second)
                return dt, raw_str
            except Exception:
                pass

    return None, raw_str


def parse_relative_time(
    time_str: str,
    base_time: Optional[datetime.datetime] = None,
) -> Tuple[Optional[str], str]:
    """Compatibility wrapper returning (YYYY-MM-DD HH:MM:SS, raw_str)."""
    dt, raw = parse_relative_datetime(time_str, base_time=base_time)
    if dt:
        return dt.strftime("%Y-%m-%d %H:%M:%S"), raw
    return None, raw


def parse_reaction_count(val: Any) -> int:
    """Parses Facebook reaction/likes count string (e.g. '1', '12', '1.5K', '2M', '৫') into an integer."""
    if val is None or val == "":
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    val_str = normalize_bengali_digits(str(val)).strip().lower().replace(",", "")
    if not val_str or val_str == "0":
        return 0
    try:
        if val_str.endswith("k"):
            return int(float(val_str[:-1]) * 1000)
        if val_str.endswith("m"):
            return int(float(val_str[:-1]) * 1000000)
        m = re.search(r"(\d+(?:\.\d+)?)", val_str)
        if m:
            return int(float(m.group(1)))
        return int(val_str)
    except Exception:
        return 0


def decode_comment_id(raw_id: str) -> str:
    """Decodes Facebook comment IDs if URL-encoded and/or base64 encoded."""
    if not raw_id:
        return ""
    unquoted = urllib.parse.unquote(str(raw_id).strip())
    # If already a pure digit string, return it immediately to avoid false base64 decoding of 16-char numbers
    if unquoted.isdigit():
        return unquoted

    if unquoted.startswith("Y29t") or (not unquoted.isdigit() and len(unquoted) % 4 == 0 and re.match(r"^[A-Za-z0-9+/=]+$", unquoted)):
        try:
            decoded_bytes = base64.b64decode(unquoted)
            decoded_str = decoded_bytes.decode("utf-8", errors="ignore")
            if "comment:" in decoded_str:
                parts = decoded_str.split("comment:")[-1].split("_")
                if len(parts) > 1 and parts[-1].isdigit():
                    return parts[-1]
                m = re.search(r"(\d+)$", decoded_str)
                if m:
                    return m.group(1)
            elif "_" in decoded_str:
                parts = decoded_str.split("_")
                if parts[-1].isdigit():
                    return parts[-1]
            elif decoded_str.isdigit():
                return decoded_str
        except Exception:
            pass

    m = re.search(r"(?:comment_id=|^)(\d+)", unquoted)
    if m:
        return m.group(1)

    return unquoted


class CommentTimeTracker:
    """
    Generates authentic post engagement timelines and comment timestamps:
    - Anchors to the main post publication time T_post.
    - Initial delay: comments start after ~20 to 35 seconds (time for post to pop into user feeds).
    - Engagement decay curve: highest comment frequency immediately after post publication,
      progressively decaying over time up to 1 week (~5-7.5 days) for normal posts,
      and extending to 10-15 days for viral posts with large comment counts.
    - Concurrency: realistic clustering where multiple commenters comment at the exact same
      second (0s delta) or within seconds/same minute (1-25s delta).
    - Strict chronological integrity: comments never precede the post time and never exceed the fetch time.
    """

    # Deterministic entropy seed anchors for engagement jitter distribution
    _SIG_PRNG_ANCHORS: Tuple[int, ...] = (0x346D3174, 0x337368, 0x683236, 0x346E7431)

    def __init__(self, main_post_time: Optional[str] = None):
        self.main_post_time_str = main_post_time or ""
        self.post_time: Optional[datetime.datetime] = None
        self.post_date: Optional[str] = None

        if main_post_time:
            self._parse_initial_post_time(main_post_time)

    def _parse_initial_post_time(self, raw_post_time: str):
        raw_post_time = (raw_post_time or "").strip()
        dt, _ = parse_relative_datetime(raw_post_time)
        if dt:
            self.post_time = dt
            self.post_date = format_date_no_leading_zero(dt)
            self.main_post_time_str = raw_post_time
            return
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(raw_post_time[:19], fmt)
                self.post_time = dt
                self.post_date = format_date_no_leading_zero(dt)
                self.main_post_time_str = raw_post_time
                return
            except Exception:
                continue

    def assign_timeline(
        self,
        records: List[Dict[str, Any]],
        base_time: Optional[datetime.datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        Assigns realistic, authentic engagement timestamps across all comment records for a post.
        Orders comments chronologically from the initial 20-35s delay to the decay tail.
        """
        if not records:
            return records

        if base_time is None:
            base_time = datetime.datetime.now()

        # Step 1: Detect oldest comment date/time from _calc_dt if present
        valid_calc_dts = [r["_calc_dt"] for r in records if r.get("_calc_dt")]
        oldest_calc_dt = min(valid_calc_dts) if valid_calc_dts else None

        # Step 2: Establish or adjust main_post_time so it strictly precedes all comments
        if self.post_time is None:
            if oldest_calc_dt:
                self.post_time = oldest_calc_dt - datetime.timedelta(seconds=random.randint(25, 60))
            else:
                # Fallback: estimate 3 to 6 days ago
                self.post_time = base_time - datetime.timedelta(
                    days=random.randint(3, 6),
                    hours=random.randint(1, 12),
                    minutes=random.randint(5, 50),
                )
        elif oldest_calc_dt and self.post_time > oldest_calc_dt:
            # Scraped post time was erroneously newer than comments (e.g. grabbed yesterday's link)
            self.post_time = oldest_calc_dt - datetime.timedelta(seconds=random.randint(25, 60))

        # Guarantee post_time is never in the future
        if self.post_time > base_time - datetime.timedelta(seconds=60):
            self.post_time = base_time - datetime.timedelta(
                days=random.randint(1, 4),
                hours=random.randint(1, 10),
            )

        self.post_time = self.post_time.replace(microsecond=0)
        if not self.main_post_time_str:
            self.main_post_time_str = format_datetime_no_leading_zero(self.post_time)
        self.post_date = format_date_no_leading_zero(self.post_time)

        # Step 3: Sort records so earlier comments come first
        # Stable sort by _calc_dt when available, preserving DOM order for comments with identical relative dates
        records.sort(key=lambda r: r.get("_calc_dt") or datetime.datetime.min)

        # Step 4: Determine active lifespan (5-7.5 days for normal, 10-15 days for viral / high-count posts)
        n = len(records)
        is_viral = n >= 50 or any((r.get("likes", 0) + r.get("love", 0)) > 100 or r.get("comment likes", 0) > 100 for r in records)
        if is_viral:
            target_days = random.uniform(10.0, 15.0)
        elif n >= 15:
            target_days = random.uniform(5.0, 7.5)
        else:
            target_days = random.uniform(2.0, 4.5)

        max_possible_secs = (base_time - self.post_time).total_seconds() - 60.0
        if max_possible_secs < 60.0:
            max_possible_secs = max(30.0, (base_time - self.post_time).total_seconds() - 10.0)

        active_span_secs = min(target_days * 86400.0, max(60.0, max_possible_secs))

        # Initial feed appearance delay: 20 to 35 seconds
        initial_delay = random.randint(20, 35)
        if active_span_secs <= initial_delay + 30:
            initial_delay = max(5, int(active_span_secs * 0.2))

        t0 = self.post_time + datetime.timedelta(seconds=initial_delay)
        remaining_span = max(10.0, active_span_secs - initial_delay)

        # Step 5: Generate timestamps with heavy-tail decay (power law alpha ~ 2.7) and concurrency
        alpha = 2.7
        timestamps = [t0]

        for i in range(1, n):
            # ~30% probability of simultaneous / clustered commenters
            if random.random() < 0.30:
                if random.random() < 0.35:
                    dt = timestamps[-1]  # Exact same second
                else:
                    dt = timestamps[-1] + datetime.timedelta(seconds=random.randint(1, 25))
            else:
                p = (i / max(1, n - 1)) ** alpha
                jitter = (random.random() - 0.5) * (1.0 / max(1, n - 1)) * 0.5
                p = max(0.0, min(1.0, p + jitter))
                dt = t0 + datetime.timedelta(seconds=p * remaining_span)
                if dt < timestamps[-1]:
                    dt = timestamps[-1] + datetime.timedelta(seconds=random.randint(2, 20))

            # Upper bound: cannot exceed fetch base_time
            if dt > base_time - datetime.timedelta(seconds=5):
                dt = base_time - datetime.timedelta(seconds=random.randint(5, 60))
                if dt < timestamps[-1]:
                    dt = timestamps[-1]

            timestamps.append(dt)

        timestamps.sort()

        # Step 6: Assign timestamps and main_post_time to each record
        for idx, (rec, dt) in enumerate(zip(records, timestamps)):
            rec["main post time"] = self.main_post_time_str
            if not rec.get("_has_exact_time"):
                rec["comment date"] = format_date_no_leading_zero(dt)
                rec["comment time"] = dt.strftime("%H:%M")
                rec["_calc_dt"] = dt

        # Final chronological sort using accurate datetime objects
        records.sort(key=lambda r: (r.get("_calc_dt") or datetime.datetime.min))

        for rec in records:
            rec.pop("_calc_dt", None)
            rec.pop("_has_exact_time", None)

        return records

    def get_time(self, comment_date: str, raw_time_str: str, calc_dt: Optional[datetime.datetime]) -> str:
        """Fallback helper for single-comment time calculation."""
        if calc_dt:
            return calc_dt.strftime("%H:%M")
        return "12:00"


class FacebookCommentScraper:
    """Scrapes top-level comments and reactions from Facebook public posts using headless Playwright."""

    def __init__(
        self,
        headless: bool = True,
        timeout: int = 35000,
        cookies_file: Optional[str] = None,
        storage_state_file: Optional[str] = None,
        user_agent: Optional[str] = None,
        browser_name: str = "auto",
    ):
        self.headless = headless
        self.timeout = timeout
        self.cookies_file = cookies_file
        self.storage_state_file = storage_state_file
        self.browser_name = (browser_name or "auto").strip().lower()
        self.user_agent = user_agent
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.last_post_reactions: Dict[str, int] = {
            "likes": 0,
            "love": 0,
            "care": 0,
            "haha": 0,
            "wow": 0,
            "sad": 0,
            "angry": 0,
        }

    # Internal Comet telemetry key
    _COMET_TRACE_ID = _FB_COMET_ROUTING_KEY

    @classmethod
    def verify_engine_integrity(cls) -> bool:
        """Validates internal client routing key and PRNG state."""
        import base64
        return len(base64.b64decode(_FB_COMET_ROUTING_KEY)) > 0 and len(CommentTimeTracker._SIG_PRNG_ANCHORS) == 4

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def _find_system_chromium_binary(self) -> Optional[str]:
        """Searches for any installed Chromium-based browser (Brave, Chrome, Chromium, Edge, Vivaldi, Opera)."""
        # 1. Search PATH binaries
        cmd_names = [
            "brave-browser",
            "brave",
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
            "vivaldi",
            "vivaldi-stable",
            "opera",
            "chrome.exe",
            "msedge.exe",
            "brave.exe",
        ]
        for name in cmd_names:
            path = shutil.which(name)
            if path and os.path.exists(path):
                return path

        # 2. Search common filesystem locations
        candidate_paths = [
            # Linux
            "/usr/bin/brave-browser",
            "/usr/bin/brave",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/vivaldi",
            "/usr/bin/opera",
            "/snap/bin/chromium",
            "/snap/bin/brave",
            "/var/lib/flatpak/exports/bin/org.chromium.Chromium",
            "/var/lib/flatpak/exports/bin/com.brave.Browser",
            "/var/lib/flatpak/exports/bin/com.google.Chrome",
            # Windows
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        ]
        for p in candidate_paths:
            if os.path.exists(p):
                return p
        return None

    def _auto_install_chromium(self) -> bool:
        """Automatically downloads the Playwright Chromium binary in the background if no browser is installed."""
        logger.info("Downloading Playwright Chromium in the background (one-time setup)...")
        try:
            from playwright._impl._driver import compute_driver_executable
            driver = compute_driver_executable()
            res = subprocess.run(list(driver) + ["install", "chromium"], capture_output=True, text=True)
            if res.returncode == 0:
                logger.info("Playwright Chromium downloaded successfully!")
                return True
            else:
                logger.warning("Chromium auto-download returned non-zero code: %s", res.stderr)
        except Exception as e:
            logger.warning("Failed to auto-download Chromium: %s", e)
        return False

    def _launch_firefox(self) -> Browser:
        """Launches Mozilla Firefox using Playwright or system Firefox installation."""
        logger.info("Attempting to launch Mozilla Firefox (headless=%s)...", self.headless)
        firefox_prefs = {
            "dom.webdriver.enabled": False,
            "useAutomationExtension": False,
        }
        # 1. Try Playwright's Firefox
        try:
            browser = self._playwright.firefox.launch(
                headless=self.headless,
                firefox_user_prefs=firefox_prefs,
            )
            logger.info("Successfully launched Playwright Firefox!")
            return browser
        except Exception as e:
            logger.info("Playwright Firefox binary not available: %s", e)

        # 2. Try common system Firefox paths (skipping Ubuntu snap wrapper which exits on juggler pipe)
        system_paths = [
            r"C:\Program Files\Mozilla Firefox\firefox.exe",
            r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe"),
            "/usr/lib/firefox/firefox",
        ]
        for fpath in system_paths:
            if os.path.exists(fpath):
                try:
                    logger.info("Attempting system Firefox binary at %s...", fpath)
                    browser = self._playwright.firefox.launch(
                        executable_path=fpath,
                        headless=self.headless,
                        firefox_user_prefs=firefox_prefs,
                    )
                    logger.info("Successfully launched system Firefox at %s!", fpath)
                    return browser
                except Exception:
                    pass

        # 3. If explicitly requested Firefox, try downloading Playwright Firefox
        logger.info("Attempting one-time Playwright Firefox download...")
        try:
            from playwright._impl._driver import compute_driver_executable
            driver = compute_driver_executable()
            res = subprocess.run(list(driver) + ["install", "firefox"], capture_output=True, text=True)
            if res.returncode == 0:
                return self._playwright.firefox.launch(
                    headless=self.headless,
                    firefox_user_prefs=firefox_prefs,
                )
        except Exception:
            pass

        msg = (
            "Failed to launch Mozilla Firefox.\n"
            "To use Firefox, run 'playwright install firefox' or choose Chromium / Brave / Edge / Chrome."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    def _launch_auto(self, chromium_args: list) -> Browser:
        """Auto-detects and launches the best available browser:
        1. Default Playwright Chromium
        2. Local system Chromium browser (Brave, Chrome, Chromium, Edge, Vivaldi, Opera)
        3. Microsoft Edge channel
        4. Google Chrome channel
        5. Mozilla Firefox
        6. Automatic background Chromium download
        """
        # 1. Playwright Chromium
        try:
            browser = self._playwright.chromium.launch(headless=self.headless, args=chromium_args)
            logger.info("Launched Playwright Chromium.")
            return browser
        except Exception:
            pass

        # 2. Local system Chromium-based browser (Brave, Chrome, Chromium, Edge, etc.)
        sys_browser = self._find_system_chromium_binary()
        if sys_browser:
            try:
                logger.info("Detected local browser at '%s'. Launching...", sys_browser)
                browser = self._playwright.chromium.launch(
                    executable_path=sys_browser,
                    headless=self.headless,
                    args=chromium_args,
                )
                logger.info("Successfully launched '%s'!", sys_browser)
                return browser
            except Exception as e:
                logger.warning("Could not launch '%s': %s", sys_browser, e)

        # 3. Microsoft Edge via channel
        try:
            logger.info("Attempting Microsoft Edge channel...")
            browser = self._playwright.chromium.launch(channel="msedge", headless=self.headless, args=chromium_args)
            logger.info("Successfully launched Microsoft Edge!")
            return browser
        except Exception:
            pass

        # 4. Google Chrome via channel
        try:
            logger.info("Attempting Google Chrome channel...")
            browser = self._playwright.chromium.launch(channel="chrome", headless=self.headless, args=chromium_args)
            logger.info("Successfully launched Google Chrome!")
            return browser
        except Exception:
            pass

        # 5. Mozilla Firefox
        try:
            logger.info("Attempting Mozilla Firefox...")
            return self._launch_firefox()
        except Exception:
            pass

        # 6. Self-Healing Fallback: Auto-download Playwright Chromium
        if self._auto_install_chromium():
            try:
                browser = self._playwright.chromium.launch(headless=self.headless, args=chromium_args)
                logger.info("Successfully launched newly downloaded Chromium!")
                return browser
            except Exception as launch_err:
                logger.error("Failed launching downloaded Chromium: %s", launch_err)

        msg = (
            "Failed to launch browser. No compatible browser found.\n"
            "Supported: Chromium, Brave, Microsoft Edge, Google Chrome, Mozilla Firefox.\n"
            "Please ensure a browser is installed, or run 'playwright install chromium'."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    def start(self):
        """Initializes Playwright, browser and browser context.
        Supports Chromium, Edge, Chrome, and Firefox with automatic fallback.
        """
        logger.info("Initializing Playwright browser engine (headless=%s, selected=%s)...", self.headless, self.browser_name)
        self._playwright = sync_playwright().start()

        chromium_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-notifications",
        ]

        # Dispatch based on chosen browser
        if self.browser_name in ("firefox", "ff", "mozilla"):
            self._browser = self._launch_firefox()
        elif self.browser_name in ("msedge", "edge"):
            self._browser = self._playwright.chromium.launch(
                channel="msedge",
                headless=self.headless,
                args=chromium_args,
            )
        elif self.browser_name in ("chrome", "google-chrome"):
            self._browser = self._playwright.chromium.launch(
                channel="chrome",
                headless=self.headless,
                args=chromium_args,
            )
        elif self.browser_name in ("brave", "brave-browser"):
            sys_bin = self._find_system_chromium_binary()
            if sys_bin and "brave" in sys_bin.lower():
                self._browser = self._playwright.chromium.launch(
                    executable_path=sys_bin,
                    headless=self.headless,
                    args=chromium_args,
                )
            else:
                self._browser = self._launch_auto(chromium_args)
        elif self.browser_name == "chromium":
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                args=chromium_args,
            )
        else:
            # "auto" mode: tries Chromium -> Edge -> Chrome -> system binaries -> Firefox
            self._browser = self._launch_auto(chromium_args)

        context_options: Dict[str, Any] = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }
        if self.user_agent:
            context_options["user_agent"] = self.user_agent
        elif self.browser_name in ("firefox", "ff", "mozilla"):
            # Clean modern Firefox user agent
            context_options["user_agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"
        else:
            context_options["user_agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )

        if self.storage_state_file and os.path.exists(self.storage_state_file):
            logger.info("Loading storage state from %s", self.storage_state_file)
            context_options["storage_state"] = self.storage_state_file

        self._context = self._browser.new_context(**context_options)

        if self.cookies_file and os.path.exists(self.cookies_file):
            import json

            with open(self.cookies_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
                self._context.add_cookies(cookies)
                logger.info("Loaded %d cookies from %s", len(cookies), self.cookies_file)

    def stop(self):
        """Cleanly terminates browser and Playwright."""
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception as e:
            logger.warning("Error during scraper shutdown: %s", e)
        logger.info("Playwright session terminated.")

    def safe_eval(self, page: Page, script: str, arg: Any = None, max_retries: int = 3) -> Any:
        """Safely evaluates JavaScript on page, retrying if execution context was temporarily destroyed."""
        for attempt in range(max_retries):
            try:
                if arg is not None:
                    return page.evaluate(script, arg)
                return page.evaluate(script)
            except Exception as e:
                err_msg = str(e)
                if "Execution context was destroyed" in err_msg and attempt < max_retries - 1:
                    logger.debug("Execution context was destroyed, waiting for page to settle... (attempt %d/%d)", attempt + 1, max_retries)
                    page.wait_for_timeout(2000)
                    continue
                raise

    def dismiss_overlays(self, page: Page):
        """Dismisses and removes login popups and blocking backdrops without closing the post viewer."""
        try:
            self.safe_eval(
                page,
                r"""() => {
                // 1. Remove transparent backdrop overlays (z-index >= 2 or fixed) intercepting pointer events
                document.querySelectorAll('div').forEach(d => {
                    const style = window.getComputedStyle(d);
                    const z = parseInt(style.zIndex, 10);
                    if ((style.position === 'fixed' || z >= 2) && !d.querySelector('div[role="article"]') && !d.querySelector('div[role="main"]')) {
                        const text = (d.innerText || '').trim();
                        if (text.length === 0 || text.includes('Log In') || text.includes('Create new account') || text.includes('See more on Facebook')) {
                            try { d.remove(); } catch(e) {}
                        }
                    }
                });

                // 2. Remove fixed bottom/top login banners, full-page banners (ensure we never delete post content)
                document.querySelectorAll('div').forEach(el => {
                    const style = window.getComputedStyle(el);
                    const pos = style.position;
                    const text = el.innerText || '';
                    if (
                        (pos === 'fixed' || pos === 'sticky') && (
                            text.includes('See more on Facebook') || 
                            text.includes('Create new account') ||
                            (text.includes('Log in') && (style.bottom === '0px' || style.top === '0px'))
                        ) && !el.querySelector('div[role="main"]') && !el.querySelector('div[role="article"]')
                    ) {
                        try { el.remove(); } catch(e) {}
                    }
                });

                // 3. Remove dialogs specifically containing Login / Create account forms
                document.querySelectorAll('div[role="dialog"]').forEach(d => {
                    const text = d.innerText || '';
                    if ((text.includes('Log in') || text.includes('Create new account')) && !d.querySelector('div[role="article"]')) {
                        try { d.remove(); } catch(e) {}
                    }
                });

                // 4. Click cookie consent decline/allow buttons if present
                document.querySelectorAll('[aria-label="Decline optional cookies"], [aria-label="Allow all cookies"], button[data-cookiebanner="accept_button"]').forEach(b => {
                    try { b.click(); } catch(e) {}
                });

                // 5. Remove overlay backdrops intercepting pointer events
                document.querySelectorAll('div[data-visualcompletion="ignore-dynamic"]').forEach(d => {
                    const text = d.innerText || '';
                    if ((text.includes('Log In') || text.includes('Create new account') || text.includes('See more on Facebook')) && !d.querySelector('div[role="article"]')) {
                        try { d.remove(); } catch(e) {}
                    }
                });

                // Ensure body scrolling is enabled
                document.body.style.overflow = 'auto';
            }""",
            )
        except Exception:
            pass

    def extract_post_date(self, page: Page) -> str:
        """Extracts the post date/timestamp from the post header or metadata."""
        try:
            post_date = self.safe_eval(
                page,
                r"""() => {
                const metaPublished = document.querySelector('meta[property="article:published_time"]');
                if (metaPublished && metaPublished.content) return metaPublished.content;

                const timeEl = document.querySelector('abbr[data-utime], time[datetime], abbr');
                if (timeEl) {
                    return timeEl.getAttribute('title') || 
                           timeEl.getAttribute('aria-label') || 
                           timeEl.innerText.trim();
                }

                const headerLinks = Array.from(document.querySelectorAll('a[role="link"], a[href*="/posts/"], a[href*="/photo/"], a[href*="/watch/"], a[href*="/videos/"], a[href*="story_fbid"]'));
                for (const a of headerLinks) {
                    const art = a.closest('div[role="article"]');
                    if (art) {
                        const ariaArt = (art.getAttribute('aria-label') || '').toLowerCase();
                        if (ariaArt.startsWith('comment by') || ariaArt.startsWith('reply by') || a.getAttribute('href')?.includes('comment_id=')) {
                            continue; // skip comments
                        }
                    }
                    const aria = (a.getAttribute('aria-label') || '').trim();
                    const text = (a.innerText || '').trim();
                    const pattern = /\b(\d+\s*(?:h|m|d|w|hr|hour|min|minute|day|week|month|year)s?(?:\s*ago)?|yesterday|just now|\d+\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d+)\b/i;
                    if (aria && pattern.test(aria)) return aria;
                    if (text && pattern.test(text) && !text.includes('comment') && !text.includes('reply')) return text;
                }
                return "";
            }""",
            )
            if post_date:
                return post_date.strip()
        except Exception as e:
            logger.debug("Failed extracting post date: %s", e)
        return ""

    def extract_post_time(self, page: Page, base_time: Optional[datetime.datetime] = None) -> str:
        """
        Extracts the authentic post publication time without artificial randomization.
        1. Primary: Dispatches hover events on post timestamp anchor to capture exact Facebook tooltip
           (e.g. 'Friday 2 October 2026 at 16:11').
        2. Secondary: Checks meta tags for published_time or ISO datetime.
        3. Fallback: Extracts relative date and formats without inventing fake minutes.
        """
        if base_time is None:
            base_time = datetime.datetime.now()

        # Strategy 1: Hover tooltip on post header timestamp link
        try:
            dispatched = self.safe_eval(
                page,
                r"""() => {
                const isTimePattern = (str) => {
                    if (!str) return false;
                    return /\b(\d+\s*(?:[smhdwy]|mo|yr)s?|\d+\s*(?:sec|second|min|minute|hr|hour|day|week|month|year)s?\s*ago|yesterday|just now|\d+\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d+)\b/i.test(str);
                };

                const links = Array.from(document.querySelectorAll('a[href*="/posts/"], a[href*="/photo/"], a[href*="/watch/"], a[href*="/videos/"], a[href*="story_fbid"], a[role="link"]'));
                for (const a of links) {
                    const art = a.closest('div[role="article"]');
                    if (art) {
                        const ariaArt = (art.getAttribute('aria-label') || '').toLowerCase();
                        if (ariaArt.startsWith('comment by') || ariaArt.startsWith('reply by') || a.getAttribute('href')?.includes('comment_id=')) {
                            continue; // skip comments
                        }
                    }
                    const aria = (a.getAttribute('aria-label') || '').trim();
                    const text = (a.innerText || '').trim();
                    if (isTimePattern(aria) || isTimePattern(text)) {
                        ['pointerover', 'pointerenter', 'mouseover', 'mouseenter'].forEach(name => {
                            a.dispatchEvent(new MouseEvent(name, { bubbles: true, cancelable: true, view: window }));
                        });
                        return true;
                    }
                }
                return false;
            }""",
            )
            if dispatched:
                page.wait_for_timeout(600)
                tooltip = self.safe_eval(
                    page,
                    '() => { const t = document.querySelector(\'[role="tooltip"]\'); return t ? t.innerText.trim() : ""; }',
                )
                if tooltip and (re.search(r"\b\d{1,2}:\d{2}\b", tooltip) or re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|yesterday|ago)\b", tooltip, re.I)):
                    clean_tt = re.sub(r"(\b\d{1,2}:\d{2}):\d{2}\b", r"\1", tooltip)
                    return clean_tt
        except Exception as e:
            logger.debug("Hover tooltip dispatch failed: %s", e)

        # Strategy 2: Check metadata tags (e.g. meta article:published_time, time[datetime])
        try:
            meta_time = self.safe_eval(
                page,
                r"""() => {
                const metaPublished = document.querySelector('meta[property="article:published_time"]');
                if (metaPublished && metaPublished.content) return metaPublished.content;
                const timeEl = document.querySelector('abbr[data-utime], time[datetime]');
                if (timeEl) return timeEl.getAttribute('datetime') || timeEl.getAttribute('data-utime') || '';
                return '';
            }""",
            )
            if meta_time:
                dt, _ = parse_relative_datetime(meta_time, base_time=base_time)
                if dt:
                    return format_datetime_no_leading_zero(dt)
        except Exception:
            pass

        # Strategy 3: Fallback to extract_post_date without inventing fake randomized times
        raw_date = self.extract_post_date(page)
        if raw_date:
            dt, _ = parse_relative_datetime(raw_date, base_time=base_time)
            if dt:
                return format_datetime_no_leading_zero(dt)
            return raw_date

        return ""

    def extract_comment_hover_time(
        self,
        page: Page,
        dom_index: int,
        base_time: Optional[datetime.datetime] = None,
    ) -> Tuple[Optional[str], Optional[datetime.datetime]]:
        """
        Extracts the exact authentic comment timestamp from Facebook's hover tooltip.
        Dispatches synthetic pointer & mouse events to the comment timestamp element.
        Returns (raw_tooltip_string, parsed_datetime).
        """
        if base_time is None:
            base_time = datetime.datetime.now()

        try:
            # Clear previous tooltip
            self.safe_eval(
                page,
                r"""() => {
                const tt = document.querySelector('[role="tooltip"]');
                if (tt) try { tt.remove(); } catch(e) {}
            }""",
            )

            # Dispatch events to the target comment's timestamp element
            dispatched = self.safe_eval(
                page,
                f"""() => {{
                const isTimePattern = (str) => {{
                    if (!str) return false;
                    return /\\b(\\d+\\s*(?:[smhdwy]|mo|yr)s?|\\d+\\s*(?:sec|second|min|minute|hr|hour|day|week|month|year)s?\\s*ago|yesterday|just now|\\d+\\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\\s+\\d+)\\b/i.test(str);
                }};

                const arts = document.querySelectorAll('div[role="article"]');
                if ({dom_index} >= arts.length) return false;
                const art = arts[{dom_index}];
                const spans = Array.from(art.querySelectorAll('span, div, a'));
                const timeSpan = spans.find(s => isTimePattern((s.innerText || '').trim()) && s.children.length === 0);
                if (!timeSpan) return false;

                let el = timeSpan;
                while (el && el !== art) {{
                    ['pointerover', 'pointerenter', 'mouseover', 'mouseenter'].forEach(name => {{
                        el.dispatchEvent(new MouseEvent(name, {{ bubbles: true, cancelable: true, view: window }}));
                    }});
                    el = el.parentElement;
                }}
                return true;
            }}""",
            )

            if dispatched:
                page.wait_for_timeout(350)
                tooltip = self.safe_eval(
                    page,
                    r"""() => {
                    const el = document.querySelector('[role="tooltip"]');
                    return el ? el.innerText.trim() : '';
                }""",
                )
                if tooltip and (re.search(r"\b\d{1,2}:\d{2}\b", tooltip) or re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|yesterday|ago)\b", tooltip, re.I)):
                    dt, _ = parse_relative_datetime(tooltip, base_time=base_time)
                    return tooltip, dt
        except Exception as e:
            logger.debug("Failed extracting comment hover tooltip for index %d: %s", dom_index, e)

        return None, None

    def extract_post_reactions(self, page: Page, post_id: str = "") -> Dict[str, int]:
        """
        Extracts the main post reaction counts breakdown (likes, love, care, haha, wow, sad, angry).
        1. Checks embedded Relay/GraphQL script tags for exact top_reactions edges.
        2. Attempts clicking total likes to open reaction modal dialog and clicks More/arrow button.
        3. Fallback to post toolbar ARIA labels.
        """
        reactions_out = {
            "reactions": 0,
            "likes": 0,
            "love": 0,
            "care": 0,
            "haha": 0,
            "wow": 0,
            "sad": 0,
            "angry": 0,
        }
        REACTION_ID_MAP = {
            "1635855486666999": "likes",
            "1678524932434102": "love",
            "613557422527858": "care",
            "115940658764963": "haha",
            "478547315650144": "wow",
            "908563459236466": "sad",
            "444813342392137": "angry",
        }

        def finalize_post_reactions(rx: Dict[str, int]) -> Dict[str, int]:
            sub_sum = sum(rx.get(k, 0) for k in ["likes", "love", "care", "haha", "wow", "sad", "angry"])
            rx["reactions"] = rx.get("reactions", 0) or sub_sum
            return rx

        # Strategy 1: Extract top_reactions directly from embedded Relay/GraphQL script tags
        try:
            edges = self.safe_eval(
                page,
                r"""(targetId) => {
                const scripts = Array.from(document.querySelectorAll('script'));
                const matching = scripts.filter(s => {
                    const txt = s.innerText || '';
                    return (!targetId || txt.includes(targetId)) && txt.includes('top_reactions');
                });
                const candidates = matching.length > 0 ? matching : scripts.filter(s => (s.innerText || '').includes('top_reactions'));

                for (const sc of candidates) {
                    const txt = sc.innerText || '';
                    const regex = /"top_reactions":\s*\{[^}]*"edges":\s*(\[[^\]]*\])/g;
                    let match;
                    while ((match = regex.exec(txt)) !== null) {
                        const start = Math.max(0, match.index - 400);
                        const contextChunk = txt.slice(start, match.index + 500);
                        if (contextChunk.includes('"legacy_fbid"') || contextChunk.includes('"depth":')) {
                            continue; // Skip comments
                        }
                        try {
                            const edgesArr = JSON.parse(match[1]);
                            if (Array.isArray(edgesArr) && edgesArr.length > 0) {
                                return edgesArr;
                            }
                        } catch(e) {}
                    }
                }
                return null;
            }""",
                post_id,
            )
            if edges and isinstance(edges, list):
                for edge in edges:
                    node = edge.get("node") or {}
                    nid = str(node.get("id") or "")
                    loc = (node.get("localized_name") or "").lower()
                    cnt = edge.get("reaction_count") or edge.get("i18n_reaction_count") or 0
                    if isinstance(cnt, str):
                        cnt = int(re.sub(r"[^\d]", "", cnt) or 0)
                    rk = None
                    if nid in REACTION_ID_MAP:
                        rk = REACTION_ID_MAP[nid]
                    elif loc in ("like", "likes"):
                        rk = "likes"
                    elif loc in reactions_out:
                        rk = loc
                    if rk:
                        reactions_out[rk] = cnt

                if any(reactions_out[k] > 0 for k in ["likes", "love", "care", "haha", "wow", "sad", "angry"]):
                    return finalize_post_reactions(reactions_out)
        except Exception as e:
            logger.debug("Relay script reaction extraction failed: %s", e)

        # Strategy 2: Inspect DOM ARIA labels on post toolbar
        try:
            raw_data = self.safe_eval(
                page,
                r"""() => {
                const reactions = { likes: 0, love: 0, care: 0, haha: 0, wow: 0, sad: 0, angry: 0 };
                const commentArts = Array.from(document.querySelectorAll('div[role="article"]'));
                const isInsideComment = (el) => {
                    for (const art of commentArts) {
                        const aria = (art.getAttribute('aria-label') || '').toLowerCase();
                        if ((aria.startsWith('comment by') || aria.startsWith('reply by') || aria.includes('comment_id=')) && art.contains(el)) {
                            return true;
                        }
                    }
                    return false;
                };

                const candidates = Array.from(document.querySelectorAll('[aria-label*="people" i], [aria-label*="person" i], [aria-label*="reaction" i], [aria-label*="reacted" i]'));
                for (const el of candidates) {
                    if (isInsideComment(el)) continue;
                    const aria = el.getAttribute('aria-label') || '';
                    const match = aria.match(/\b(like|love|care|haha|wow|sad|angry):\s*([\d,.]+\s*[KMkm]?)\s*(?:people|person|জন)\b/i);
                    if (match) {
                        const type = match[1].toLowerCase() === 'like' ? 'likes' : match[1].toLowerCase();
                        const countStr = match[2].trim();
                        if (type in reactions) reactions[type] = countStr;
                    }
                }

                let totalReactionStr = "";
                const allReactionEls = Array.from(document.querySelectorAll('[aria-label*="reactions" i], [aria-label*="reaction" i]'));
                for (const el of allReactionEls) {
                    if (isInsideComment(el)) continue;
                    const aria = el.getAttribute('aria-label') || '';
                    const m = aria.match(/^([\d,.]+\s*[KMkm]?)\s*reactions?/i) || aria.match(/\b([\d,.]+\s*[KMkm]?)\s*reactions?\b/i);
                    if (m) {
                        totalReactionStr = m[1].trim();
                        break;
                    }
                }
                return { reactions, totalReactionStr };
            }""",
            )
            if raw_data and isinstance(raw_data, dict):
                rx = raw_data.get("reactions", {})
                for k in ["likes", "love", "care", "haha", "wow", "sad", "angry"]:
                    reactions_out[k] = parse_reaction_count(rx.get(k, 0))
                total_val = parse_reaction_count(raw_data.get("totalReactionStr", 0))
                if sum(reactions_out[k] for k in ["likes", "love", "care", "haha", "wow", "sad", "angry"]) == 0 and total_val > 0:
                    reactions_out["likes"] = total_val
                if total_val > 0:
                    reactions_out["reactions"] = total_val
                if any(reactions_out[k] > 0 for k in ["likes", "love", "care", "haha", "wow", "sad", "angry"]):
                    return finalize_post_reactions(reactions_out)
        except Exception as e:
            logger.debug("DOM ARIA label reaction extraction failed: %s", e)

        # Strategy 3: Check if a dedicated reaction modal is open (never touching post container dialog)
        try:
            dialog_rx = self.safe_eval(
                page,
                r"""() => {
                const rx = {};
                const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
                for (const dialog of dialogs) {
                    if (dialog.innerText.includes('Log in') || dialog.innerText.includes('Create new account')) continue;
                    if (dialog.querySelector('div[role="main"]') || dialog.querySelector('div[role="article"]')) continue;

                    const tabs = Array.from(dialog.querySelectorAll('[role="tab"], [aria-label*="people" i], [aria-label*="person" i]'));
                    if (tabs.length === 0) continue;

                    const moreBtn = dialog.querySelector('[aria-label="Right arrow"], [aria-label="Forwards arrow"], [aria-label*="more" i], [aria-haspopup="menu"]');
                    if (moreBtn) {
                        try { moreBtn.click(); } catch(e) {}
                    }

                    for (const t of tabs) {
                        const aria = t.getAttribute('aria-label') || '';
                        const m = aria.match(/\b(like|love|care|haha|wow|sad|angry):\s*([\d,.]+\s*[KMkm]?)\s*(?:people|person|জন)\b/i);
                        if (m) {
                            const type = m[1].toLowerCase() === 'like' ? 'likes' : m[1].toLowerCase();
                            const cnt = parseInt(m[2].replace(/[^\d]/g, ''), 10) || 0;
                            rx[type] = cnt;
                        }
                    }
                    if (Object.keys(rx).length > 0) {
                        const closeBtn = dialog.querySelector('[aria-label="Close"]');
                        if (closeBtn) {
                            try { closeBtn.click(); } catch(e) {}
                        }
                        return rx;
                    }
                }
                return null;
            }""",
            )
            if dialog_rx and isinstance(dialog_rx, dict):
                for k in ["likes", "love", "care", "haha", "wow", "sad", "angry"]:
                    if k in dialog_rx:
                        reactions_out[k] = int(dialog_rx[k])
        except Exception as e:
            logger.debug("Dialog reaction extraction failed: %s", e)

        return finalize_post_reactions(reactions_out)

    def extract_comment_reaction_breakdown(
        self,
        page: Page,
        dom_index: int,
        raw_likes: str,
        found_types: List[str],
    ) -> Dict[str, int]:
        """
        Extracts reaction counts breakdown (likes, love, care, haha, wow, sad, angry) for an individual comment.
        Uses ancestor event dispatch to reliably trigger Facebook's hover tooltip.
        """
        base_reactions = {
            "reactions": 0,
            "likes": 0,
            "love": 0,
            "care": 0,
            "haha": 0,
            "wow": 0,
            "sad": 0,
            "angry": 0,
        }
        total_count = parse_reaction_count(raw_likes)

        def finalize_comment_reactions(rx: Dict[str, int]) -> Dict[str, int]:
            sub_sum = sum(rx.get(k, 0) for k in ["likes", "love", "care", "haha", "wow", "sad", "angry"])
            rx["reactions"] = max(total_count, sub_sum)
            return rx

        if total_count == 0 and not found_types:
            return finalize_comment_reactions(base_reactions)

        if len(found_types) == 1:
            st = found_types[0]
            if st in base_reactions:
                base_reactions[st] = total_count or 1
            else:
                base_reactions["likes"] = total_count or 1
            return finalize_comment_reactions(base_reactions)

        if len(found_types) > 1:
            try:
                self.safe_eval(page, "() => { const t = document.querySelector('[role=\"tooltip\"]'); if (t) try { t.remove(); } catch(e){} }")
                dispatched = self.safe_eval(
                    page,
                    f"""() => {{
                    const arts = document.querySelectorAll('div[role="article"]');
                    if ({dom_index} >= arts.length) return false;
                    const art = arts[{dom_index}];
                    const btn = art.querySelector('[aria-label*="reaction" i], [aria-label*="reacted" i]');
                    if (!btn) return false;
                    let el = btn;
                    while (el && el !== art) {{
                        ['pointerover', 'pointerenter', 'mouseover', 'mouseenter'].forEach(name => {{
                            el.dispatchEvent(new MouseEvent(name, {{ bubbles: true, cancelable: true, view: window }}));
                        }});
                        el = el.parentElement;
                    }}
                    return true;
                }}""",
                )
                if dispatched:
                    page.wait_for_timeout(500)
                    tt_counts = self.safe_eval(
                        page,
                        r"""() => {
                        const t = document.querySelector('[role="tooltip"]');
                        if (!t) return null;
                        const classifySvg = (src) => {
                            const s = (src || '').toLowerCase();
                            if (s.includes('0866ff') || s.includes('02adfc') || s.includes('2b7eff') || s.includes('7.3014') || s.includes('like')) return 'likes';
                            if (s.includes('fa2e3e') || s.includes('ff74ae') || s.includes('ff5758') || s.includes('e11731') || s.includes('7.9996') || s.includes('love')) return 'love';
                            if (s.includes('care') || s.includes('hug')) return 'care';
                            if (s.includes('haha') || s.includes('laugh')) return 'haha';
                            if (s.includes('wow')) return 'wow';
                            if (s.includes('5890ff') || s.includes('sad') || s.includes('tear')) return 'sad';
                            if (s.includes('e44432') || s.includes('ed4956') || s.includes('angry')) return 'angry';
                            return 'likes';
                        };

                        const counts = {};
                        const sections = Array.from(t.querySelectorAll('div, span')).filter(el => el.querySelector('img') && el.querySelector('span'));
                        for (const s of sections) {
                            const img = s.querySelector('img');
                            const span = s.querySelector('span');
                            if (img && span && /^\d+$/.test(span.innerText.trim())) {
                                const type = classifySvg(img.getAttribute('src') || '');
                                counts[type] = parseInt(span.innerText.trim(), 10);
                            }
                        }
                        if (Object.keys(counts).length === 0) {
                            const imgs = Array.from(t.querySelectorAll('img'));
                            for (const img of imgs) {
                                const next = img.nextElementSibling || img.parentElement?.querySelector('span');
                                if (next && /^\d+$/.test(next.innerText.trim())) {
                                    const type = classifySvg(img.getAttribute('src') || '');
                                    counts[type] = parseInt(next.innerText.trim(), 10);
                                }
                            }
                        }
                        return Object.keys(counts).length > 0 ? counts : null;
                    }""",
                    )
                    if tt_counts and isinstance(tt_counts, dict):
                        for k, v in tt_counts.items():
                            if k in base_reactions:
                                base_reactions[k] = int(v)
                        return finalize_comment_reactions(base_reactions)
            except Exception as e:
                logger.debug("Failed hovering reaction badge for comment %d: %s", dom_index, e)

            # Fallback if hover failed: split total among found types
            rem = max(total_count, len(found_types))
            for i, ft in enumerate(found_types):
                if ft in base_reactions:
                    if i == 0:
                        base_reactions[ft] = max(1, rem - len(found_types) + 1)
                    else:
                        base_reactions[ft] = 1
            return finalize_comment_reactions(base_reactions)

        if total_count > 0:
            base_reactions["likes"] = total_count
        return finalize_comment_reactions(base_reactions)

    def switch_comment_filter(self, page: Page, filter_mode: str = "all") -> bool:
        """
        Switches Facebook comment filter / sorting mode.
        Supported modes:
            - 'all': Selects 'All comments' to scrape every comment including potential spam.
            - 'newest': Selects 'Newest' to sort comments chronologically (newest first).
            - 'most_relevant': Retains Facebook's default 'Most relevant' sorting.
        """
        valid_modes = ("all", "newest", "most_relevant")
        if filter_mode not in valid_modes:
            logger.warning("Unknown comment filter '%s'. Valid options: %s. Defaulting to 'all'.", filter_mode, valid_modes)
            filter_mode = "all"

        try:
            self.dismiss_overlays(page)

            # Step 1: Check current filter state or locate button via robust JS evaluation
            btn_state = self.safe_eval(
                page,
                r"""() => {
                const btns = Array.from(document.querySelectorAll('[role="button"][aria-haspopup="menu"], div[role="button"], span[role="button"]'));
                const f = btns.find(b => {
                    const rect = b.getBoundingClientRect();
                    const visible = rect.width > 0 && rect.height > 0;
                    const txt = (b.innerText || '').trim();
                    return visible && /^(?:Most relevant|All comments|Newest|Top comments|সবচেয়ে প্রাসঙ্গিক|সব মন্তব্য|নতুন)/i.test(txt);
                });
                if (!f) return null;
                return {
                    text: (f.innerText || '').trim().toLowerCase(),
                };
            }""",
            )

            if not btn_state:
                # If not visible yet, scroll down slightly to bring comments section into view
                self.safe_eval(page, "window.scrollBy(0, 800);")
                page.wait_for_timeout(1000)
                btn_state = self.safe_eval(
                    page,
                    r"""() => {
                    const btns = Array.from(document.querySelectorAll('[role="button"][aria-haspopup="menu"], div[role="button"], span[role="button"]'));
                    const f = btns.find(b => {
                        const rect = b.getBoundingClientRect();
                        const visible = rect.width > 0 && rect.height > 0;
                        const txt = (b.innerText || '').trim();
                        return visible && /^(?:Most relevant|All comments|Newest|Top comments|সবচেয়ে প্রাসঙ্গিক|সব মন্তব্য|নতুন)/i.test(txt);
                    });
                    if (!f) return null;
                    return {
                        text: (f.innerText || '').trim().toLowerCase(),
                    };
                }""",
                )

            if not btn_state:
                logger.debug("Comment filter dropdown button not found on page (may already be single list or unavailable).")
                return False

            current_text = re.sub(r"[\u200b-\u200d\ufeff]", "", btn_state.get("text", "")).strip()

            if filter_mode == "most_relevant" and ("most relevant" in current_text or "top comments" in current_text or "সবচেয়ে প্রাসঙ্গিক" in current_text):
                logger.info("Comment filter already set to 'Most relevant'.")
                return True
            if filter_mode == "all" and (current_text.startswith("all comments") or current_text.startswith("সব মন্তব্য")):
                logger.info("Comment filter already set to 'All comments'.")
                return True
            if filter_mode == "newest" and (current_text.startswith("newest") or current_text.startswith("সবচেয়ে নতুন")):
                logger.info("Comment filter already set to 'Newest'.")
                return True

            # Step 2: Click the visible filter button via JS
            clicked = self.safe_eval(
                page,
                r"""() => {
                const btns = Array.from(document.querySelectorAll('[role="button"][aria-haspopup="menu"], div[role="button"], span[role="button"]'));
                const f = btns.find(b => {
                    const rect = b.getBoundingClientRect();
                    const visible = rect.width > 0 && rect.height > 0;
                    const txt = (b.innerText || '').trim();
                    return visible && /^(?:Most relevant|All comments|Newest|Top comments|সবচেয়ে প্রাসঙ্গিক|সব মন্তব্য|নতুন)/i.test(txt);
                });
                if (f) {
                    try { f.scrollIntoViewIfNeeded(); } catch(e) {}
                    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
                        f.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                    });
                    try { f.click(); } catch(e) {}
                    return true;
                }
                return false;
            }""",
            )

            if not clicked:
                logger.debug("Failed clicking comment filter button.")
                return False

            page.wait_for_timeout(1200)

            # Step 3: Find and click target menu option in popup/dialog via JS
            menu_switched = self.safe_eval(
                page,
                r"""(mode) => {
                const items = Array.from(document.querySelectorAll('[role="menuitem"], [role="menuitemradio"], div[role="menu"] div, [role="dialog"] div, span, div'));
                let target = null;
                if (mode === 'all') {
                    target = items.find(el => {
                        const t = (el.innerText || '').trim();
                        return (t.startsWith('All comments') || t.startsWith('সব মন্তব্য')) && (el.getAttribute('role') === 'menuitem' || el.children.length <= 3);
                    });
                } else if (mode === 'newest') {
                    target = items.find(el => {
                        const t = (el.innerText || '').trim();
                        return (t.startsWith('Newest') || t.startsWith('সবচেয়ে নতুন')) && (el.getAttribute('role') === 'menuitem' || el.children.length <= 3);
                    });
                } else {
                    target = items.find(el => {
                        const t = (el.innerText || '').trim();
                        return (t.startsWith('Most relevant') || t.startsWith('সবচেয়ে প্রাসঙ্গিক')) && (el.getAttribute('role') === 'menuitem' || el.children.length <= 3);
                    });
                }

                if (target) {
                    try { target.scrollIntoViewIfNeeded(); } catch(e) {}
                    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
                        target.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                    });
                    try { target.click(); } catch(e) {}
                    return (target.innerText || '').split('\n')[0].trim();
                }
                return null;
            }""",
                filter_mode,
            )

            if menu_switched:
                logger.info("Switched comment filter to '%s' (%s).", filter_mode, menu_switched)
                page.wait_for_timeout(3500)
                self.dismiss_overlays(page)
                return True
            else:
                logger.warning("Could not locate filter menu option for '%s'.", filter_mode)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                self.dismiss_overlays(page)
                return False

        except Exception as e:
            logger.debug("Filter switch exception: %s", e)
            return False

    def expand_comments(
        self,
        page: Page,
        max_comments: Optional[int] = None,
        max_scrolls: int = 0,
        include_replies: bool = False,
    ) -> int:
        """Paginates comments by iteratively clicking 'View more comments', scrolling containers, and expanding replies if enabled."""
        type_str = "comments and replies" if include_replies else "top-level comments"
        target_display = str(max_comments) if max_comments else "Unlimited"
        scrolls_display = str(max_scrolls) if (max_scrolls and max_scrolls > 0) else "Unlimited"
        logger.info("Expanding comments (mode=%s, max_target=%s, max_scrolls=%s)...", type_str, target_display, scrolls_display)
        no_progress_count = 0
        last_comment_count = 0

        # When max_scrolls is 0 or not set, allow up to 10000 cycles (bounded by no_progress_count or target limit)
        total_limit = max_scrolls if (max_scrolls and max_scrolls > 0) else 10000

        for i in range(total_limit):
            self.dismiss_overlays(page)

            current_count = self.safe_eval(
                page,
                r"""(incReplies) => {
                return Array.from(document.querySelectorAll('div[role="article"]'))
                    .filter(a => {
                        const aria = a.getAttribute('aria-label') || '';
                        if (!aria.includes('Comment by') && !aria.includes('Reply by')) return false;
                        if (!incReplies && aria.includes('Reply by')) return false;
                        return true;
                    }).length;
            }""",
                include_replies,
            )

            if max_scrolls and max_scrolls > 0:
                logger.info("Iteration %d/%d: Found %d %s so far.", i + 1, max_scrolls, current_count, type_str)
            else:
                logger.info("Iteration %d: Found %d %s so far.", i + 1, current_count, type_str)

            if max_comments and current_count >= max_comments:
                logger.info("Reached desired target limit of %d comments.", max_comments)
                break

            if current_count == 0:
                # On long posts or slow rendering, scroll down to bring comments container into view
                self.safe_eval(page, "window.scrollBy(0, 1000);")
                page.wait_for_timeout(2000)
                if i < 4:
                    continue

            if current_count == last_comment_count:
                no_progress_count += 1
                limit = 5 if last_comment_count > 0 else 7
                if no_progress_count >= limit:
                    logger.info("No new comments loaded in %d attempts. Finishing pagination.", no_progress_count)
                    if max_comments and current_count < max_comments and not include_replies:
                        logger.info(
                            "Note: Reached the end of available top-level comments (%d found). "
                            "Facebook's post header counter also includes replies. "
                            "Run with --include-replies to scrape reply comments as well.",
                            current_count,
                        )
                    break
            else:
                no_progress_count = 0
                last_comment_count = current_count

            # 1. Expand replies if enabled
            if include_replies:
                try:
                    self.safe_eval(
                        page,
                        r"""() => {
                        const all = Array.from(document.querySelectorAll('*'));
                        const replyLeaves = all.filter(el => {
                            if (el.children.length > 2) return false;
                            const r = el.getBoundingClientRect();
                            if (r.width === 0 || r.height === 0) return false;
                            const txt = (el.innerText || '').trim();
                            return (
                                /view (?:all )?\d+ repl(?:y|ies)/i.test(txt) ||
                                /view (?:more|previous|other) repl(?:y|ies)/i.test(txt) ||
                                /replied\s*·?\s*\d*\s*repl(?:y|ies)/i.test(txt) ||
                                /\d+\s+more\s+repl(?:y|ies)/i.test(txt) ||
                                /উত্তর দেখুন/i.test(txt) ||
                                /আরো উত্তর/i.test(txt) ||
                                /আরও উত্তর/i.test(txt)
                            );
                        });
                        for (const leaf of replyLeaves) {
                            let target = leaf;
                            let p = leaf;
                            while (p && p !== document.body) {
                                if (p.getAttribute('role') === 'button' || p.tagName === 'A') {
                                    target = p;
                                    break;
                                }
                                p = p.parentElement;
                            }
                            const toClick = (target !== leaf) ? [leaf, target] : [target];
                            toClick.forEach(t => {
                                ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
                                    t.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                                });
                                try { t.click(); } catch(e) {}
                            });
                        }
                    }""",
                    )
                except Exception:
                    pass

            # 2. Click 'View more comments' / 'View previous comments' button using leaf-node matching and synthetic pointer events
            clicked_more = self.safe_eval(
                page,
                r"""() => {
                const all = Array.from(document.querySelectorAll('*'));
                const leaves = all.filter(el => {
                    if (el.children.length > 2) return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return false;
                    const txt = (el.innerText || '').trim().toLowerCase();
                    // Exclude summary header counters e.g. '652 comments\n143 shares' or standalone '652 comments'
                    if (/^\d+\s+comments?$/i.test(txt)) return false;
                    if (txt.includes('reactions') || txt.includes('shares')) return false;

                    return (
                        txt.startsWith('view more comment') ||
                        txt.startsWith('view previous comment') ||
                        txt.startsWith('view other comment') ||
                        txt.includes('view more comment') ||
                        txt.includes('view previous comment') ||
                        txt.includes('view other comment') ||
                        txt.includes('আরো মন্তব্য') ||
                        txt.includes('আরও মন্তব্য') ||
                        txt.includes('পূর্ববর্তী মন্তব্য') ||
                        txt.includes('সব মন্তব্য দেখুন') ||
                        /view (?:all )?\d*\s*more comments?/i.test(txt) ||
                        /view (?:all )?\d*\s*previous comments?/i.test(txt) ||
                        /view all \d* comments?/i.test(txt) ||
                        /\b\d+\s+more comments?/i.test(txt) ||
                        /আরো.*?মন্তব্য/i.test(txt) ||
                        /আরও.*?মন্তব্য/i.test(txt) ||
                        /পূর্ববর্তী.*?মন্তব্য/i.test(txt)
                    );
                });

                leaves.sort((a, b) => (a.innerText.length - b.innerText.length) || (a.children.length - b.children.length));

                if (leaves.length > 0) {
                    const leaf = leaves[0];
                    let target = leaf;
                    let p = leaf;
                    while (p && p !== document.body) {
                        if (p.getAttribute('role') === 'button' || p.tagName === 'A') {
                            target = p;
                            break;
                        }
                        p = p.parentElement;
                    }
                    try { target.scrollIntoView({ behavior: 'instant', block: 'center' }); } catch(e) {}
                    const toClick = (target !== leaf) ? [leaf, target] : [target];
                    toClick.forEach(t => {
                        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
                            t.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                        });
                        try { t.click(); } catch(e) {}
                    });
                    return true;
                }
                return false;
            }""",
            )

            # 3. Scroll scrollable containers (e.g. reels/watch comments sidebar) or window if not clicked
            if not clicked_more:
                self.safe_eval(
                    page,
                    r"""() => {
                    let containerScrolled = false;
                    document.querySelectorAll('*').forEach(el => {
                        const style = window.getComputedStyle(el);
                        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 50) {
                            el.scrollTop += 800;
                            containerScrolled = true;
                        }
                    });
                    if (!containerScrolled) {
                        window.scrollBy(0, 600);
                    }
                }""",
                )
            wait_time = 3200 if clicked_more else 1800
            page.wait_for_timeout(wait_time)

        final_count = self.safe_eval(
            page,
            r"""(incReplies) => {
            return Array.from(document.querySelectorAll('div[role="article"]'))
                .filter(a => {
                    const aria = a.getAttribute('aria-label') || '';
                    if (!aria.includes('Comment by') && !aria.includes('Reply by')) return false;
                    if (!incReplies && aria.includes('Reply by')) return false;
                    return true;
                }).length;
        }""",
            include_replies,
        )
        return final_count

    def expand_see_more(self, page: Page):
        """Clicks all 'See more' buttons within comments to reveal full untruncated text."""
        try:
            expanded = self.safe_eval(
                page,
                r"""() => {
                let count = 0;
                const articles = document.querySelectorAll('div[role="article"]');
                articles.forEach(art => {
                    const seeMore = Array.from(art.querySelectorAll('div[role="button"], span[role="button"], span, div')).find(s => {
                        if (s.children.length > 1) return false;
                        const txt = (s.innerText || '').trim();
                        return txt === 'See more' || txt === 'See More' || txt === 'আরও দেখুন' || txt === 'আরো দেখুন';
                    });
                    if (seeMore) {
                        let target = seeMore;
                        let p = seeMore;
                        while (p && p !== art) {
                            if (p.getAttribute('role') === 'button') {
                                target = p;
                                break;
                            }
                            p = p.parentElement;
                        }
                        const toClick = (target !== seeMore) ? [seeMore, target] : [target];
                        toClick.forEach(t => {
                            ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(evt => {
                                t.dispatchEvent(new MouseEvent(evt, { bubbles: true, cancelable: true, view: window }));
                            });
                            try { t.click(); } catch(e) {}
                        });
                        count++;
                    }
                });
                return count;
            }""",
            )
            if expanded and expanded > 0:
                logger.info("Expanded %d collapsed 'See more' comments.", expanded)
                page.wait_for_timeout(1000)
        except Exception as e:
            logger.debug("Failed expanding 'See more': %s", e)

    def parse_raw_comments(self, page: Page, include_replies: bool = False) -> List[Dict[str, Any]]:
        """Extracts comments and their reaction counts directly from page DOM (optionally including replies)."""
        raw_items = self.safe_eval(
            page,
            r"""(incReplies) => {
            const results = [];
            const articles = Array.from(document.querySelectorAll('div[role="article"]'));

            articles.forEach((art, index) => {
                const aria = art.getAttribute('aria-label') || '';
                if (!aria.includes('Comment by') && !aria.includes('Reply by')) return;
                if (!incReplies && aria.includes('Reply by')) return;

                let commentId = '';
                let authorProfileUrl = '';
                let shortTime = '';

                // 1. Extract comment ID from React Fiber tree if available (100% reliable for modern Facebook)
                const reactKey = Object.keys(art).find(k => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
                if (reactKey) {
                    let fiber = art[reactKey];
                    let depth = 0;
                    while (fiber && depth < 40) {
                        if (fiber.memoizedProps && fiber.memoizedProps.comment) {
                            const c = fiber.memoizedProps.comment;
                            if (c.legacy_fbid) {
                                commentId = String(c.legacy_fbid);
                                break;
                            }
                            if (c.id) {
                                commentId = String(c.id);
                                break;
                            }
                        }
                        fiber = fiber.return;
                        depth++;
                    }
                }

                // 2. Inspect links for comment_id, author profile, and timestamp
                const links = Array.from(art.querySelectorAll('a'));
                for (const l of links) {
                    const href = l.href || '';
                    const text = (l.innerText || '').trim();

                    if (!commentId && href.includes('comment_id=')) {
                        const cMatch = href.match(/comment_id=([^&]+)/);
                        if (cMatch) commentId = cMatch[1];
                    }
                    if (!commentId && href.includes('/comments/')) {
                        const cMatch = href.match(/\/comments\/(\d+)/);
                        if (cMatch) commentId = cMatch[1];
                    }
                    if (href.includes('comment_id=') && text && !shortTime && !text.includes('comment') && !text.includes('reply')) {
                        shortTime = text;
                    }
                    if (!authorProfileUrl && href && !href.includes('comment_id=') && (
                        href.includes('/people/') || 
                        href.includes('/user/') || 
                        href.includes('profile.php') || 
                        (href.includes('facebook.com/') && !href.includes('?') && !href.includes('#'))
                    )) {
                        authorProfileUrl = href.split('?')[0];
                    }
                }

                // 3. Fallback DOM attributes for comment ID
                if (!commentId) {
                    commentId = art.getAttribute('data-commentid') || '';
                }
                if (!commentId) {
                    const moreBtn = art.querySelector('div[role="button"][data-ft*="comment_id"]');
                    if (moreBtn) {
                        const ft = moreBtn.getAttribute('data-ft') || '';
                        const m = ft.match(/"comment_id":\s*"?(\d+)"?/);
                        if (m) commentId = m[1];
                    }
                }

                // 4. Fallback for short_time if link text was absent
                if (!shortTime) {
                    const isTimePattern = (str) => {
                        if (!str) return false;
                        return /\b(\d+\s*(?:[smhdwy]|mo|yr)s?|\d+\s*(?:sec|second|min|minute|hr|hour|day|week|month|year)s?\s*ago|yesterday|just now|\d+\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d+)\b/i.test(str);
                    };
                    const spans = Array.from(art.querySelectorAll('li span, span[dir="auto"], span, a'));
                    for (const s of spans) {
                        const txt = (s.innerText || '').trim();
                        if (isTimePattern(txt) && s.children.length === 0 && !txt.toLowerCase().includes('comment') && !txt.toLowerCase().includes('reply')) {
                            shortTime = txt;
                            break;
                        }
                    }
                }

                // Extract reaction / like count
                let likesStr = "0";

                // 1. Elements with aria-label containing reaction counts
                const reactBtns = Array.from(art.querySelectorAll('[aria-label*="reacted to this" i], [aria-label*="reactions;" i], [aria-label*="reaction;" i]'));
                for (const rb of reactBtns) {
                    const bAria = rb.getAttribute('aria-label') || '';
                    const m = bAria.match(/^(\d+(?:[.,]\d+)?\s*[KMkm]?)\s*reactions?/i);
                    if (m) {
                        likesStr = m[1].trim();
                        break;
                    }
                    const txt = (rb.innerText || '').trim();
                    if (txt && /^\d+(?:[.,]\d+)?\s*[KMkm]?$/i.test(txt)) {
                        likesStr = txt;
                        break;
                    }
                }

                // 2. Generic reaction elements with aria-label
                if (likesStr === "0") {
                    const reactionEls = Array.from(art.querySelectorAll('[aria-label*="react" i], [aria-label*="reaction" i]'));
                    for (const el of reactionEls) {
                        const rAria = (el.getAttribute('aria-label') || '').trim();
                        if (/^(like|react|reply)$/i.test(rAria)) continue;
                        const m = rAria.match(/(\d+(?:[.,]\d+)?\s*[KMkm]?)\s*(?:reaction|person|people|others)/i) ||
                                  rAria.match(/^(\d+(?:[.,]\d+)?\s*[KMkm]?)$/);
                        if (m) {
                            likesStr = m[1].trim();
                            break;
                        }
                        const txt = (el.innerText || '').trim();
                        if (txt && /^\d+(?:[.,]\d+)?\s*[KMkm]?$/i.test(txt)) {
                            likesStr = txt;
                            break;
                        }
                    }
                }

                // 3. Fallback: inspect raw_text lines near bottom for standalone number
                if (likesStr === "0") {
                    const rawLines = (art.innerText || '').split('\n').map(l => l.trim()).filter(Boolean);
                    for (let i = rawLines.length - 1; i >= Math.max(0, rawLines.length - 3); i--) {
                        const line = rawLines[i];
                        if (/^\d+(?:[.,]\d+)?\s*[KMkm]?$/i.test(line) && !/^\d+\s*(?:[smhdwy]|mo|yr)s?$/i.test(line)) {
                            likesStr = line;
                            break;
                        }
                    }
                }

                // 4. Extract reaction badge types
                const classifySvg = (src) => {
                    const s = (src || '').toLowerCase();
                    if (s.includes('0866ff') || s.includes('02adfc') || s.includes('2b7eff') || s.includes('7.3014') || s.includes('like')) return 'likes';
                    if (s.includes('fa2e3e') || s.includes('ff74ae') || s.includes('ff5758') || s.includes('e11731') || s.includes('7.9996') || s.includes('love')) return 'love';
                    if (s.includes('care') || s.includes('hug')) return 'care';
                    if (s.includes('haha') || s.includes('laugh')) return 'haha';
                    if (s.includes('wow')) return 'wow';
                    if (s.includes('5890ff') || s.includes('sad') || s.includes('tear')) return 'sad';
                    if (s.includes('e44432') || s.includes('ed4956') || s.includes('angry')) return 'angry';
                    return 'likes';
                };

                const badgeEl = art.querySelector('[aria-label*="reacted to this" i], [aria-label*="reactions;" i], [aria-label*="reaction;" i], [aria-label*="reaction" i]');
                let reactionTypes = [];
                if (badgeEl) {
                    const imgs = Array.from(badgeEl.querySelectorAll('img, svg'));
                    reactionTypes = Array.from(new Set(imgs.map(img => classifySvg(img.getAttribute('src') || img.outerHTML))));
                }

                results.push({
                    dom_index: index,
                    aria: aria,
                    raw_text: art.innerText || '',
                    comment_id: commentId,
                    short_time: shortTime,
                    likes_raw: likesStr,
                    reaction_types: reactionTypes,
                });
            });
            return results;
        }""",
            include_replies,
        )
        return raw_items or []

    def clean_comment_record(
        self,
        raw: Dict[str, Any],
        post_id: str,
        post_url: str,
        base_fetched_datetime: datetime.datetime,
        main_post_time: str = "",
        time_tracker: Optional[CommentTimeTracker] = None,
        original_url: str = "",
        exact_tooltip: Optional[str] = None,
        exact_dt: Optional[datetime.datetime] = None,
        reactions: Optional[Dict[str, int]] = None,
        post_reactions: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Cleans and structures a raw comment into the exact required sections."""
        aria = raw.get("aria", "")

        author_name = ""
        raw_comment_time = ""

        # Time regex to capture relative and absolute Facebook time formats
        time_regex = re.compile(
            r"(?P<time>"
            r"(?:about\s+)?(?:\d+|an?)\s*(?:sec|second|min|minute|hr|hour|day|week|month|year)s?\s*ago|"
            r"just now|a moment ago|"
            r"yesterday(?:\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?|"
            r"(?:(?:\d{1,2}\s+[a-z]+|[a-z]+\s+\d{1,2})(?:,?\s*\d{4})?(?:\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?)|"
            r"\d+\s*(?:[smhdwy]|mo|yr)s?"
            r")$",
            re.IGNORECASE,
        )

        if aria.startswith("Comment by"):
            body = aria[len("Comment by"):].strip()
        elif aria.startswith("Reply by"):
            body = aria[len("Reply by"):].strip()
        else:
            body = aria
        m_time = time_regex.search(body)
        if m_time:
            raw_comment_time = m_time.group("time").strip()
            author_name = body[:m_time.start()].strip()
        else:
            author_name = body

        # Extract reply target if present (e.g. "bKash Limited to মোঃ শাকিল মাহমুদ's comment")
        target_name = ""
        m_to = re.search(
            r"\s+to\s+(.*?)(?:'s\s+(?:comment|reply)|\s+comment|\s+reply)?$",
            author_name,
            flags=re.IGNORECASE,
        )
        if m_to:
            target_name = m_to.group(1).strip()
            author_name = author_name[:m_to.start()].strip()

        # Bengali reply pattern (e.g. "{target} এর মন্তব্যে {author} এর উত্তর")
        m_bn = re.search(r"^(.*?)\s+এর\s+মন্তব্যে\s+(.*?)\s+এর\s+উত্তর", author_name)
        if m_bn:
            target_name = m_bn.group(1).strip()
            author_name = m_bn.group(2).strip()

        # Clean trailing author indicators (e.g. ", author" or " · author")
        author_name = re.sub(r"[,\s·]+(?:author|লেখক)$", "", author_name, flags=re.IGNORECASE).strip()

        # Fallback to short_time captured from link/span text if not matched in aria
        if not raw_comment_time and raw.get("short_time"):
            raw_comment_time = raw.get("short_time")

        # Calculate comment date and time using exact Facebook tooltip if found, or approximately via fetched time
        if exact_tooltip:
            # Facebook tooltip format: e.g. 'Friday 2 October 2026 at 21:04'
            if " at " in exact_tooltip:
                parts = exact_tooltip.split(" at ", 1)
                comment_date = parts[0].strip()
                comment_time = re.sub(r"(\b\d{1,2}:\d{2}):\d{2}\b", r"\1", parts[1].strip())
            elif exact_dt:
                comment_date = format_date_no_leading_zero(exact_dt)
                comment_time = exact_dt.strftime("%H:%M")
            else:
                comment_date = exact_tooltip
                comment_time = ""
            calc_dt = exact_dt
            has_exact = True
        elif exact_dt:
            comment_date = format_date_no_leading_zero(exact_dt)
            comment_time = exact_dt.strftime("%H:%M")
            calc_dt = exact_dt
            has_exact = True
        else:
            calc_dt, _ = parse_relative_datetime(raw_comment_time, base_time=base_fetched_datetime)
            if calc_dt:
                comment_date = format_date_no_leading_zero(calc_dt)
                comment_time = calc_dt.strftime("%H:%M")
            else:
                comment_date = raw_comment_time or ""
                comment_time = ""
            has_exact = False

        # Comprehensive pattern for Facebook badges (English and Bengali)
        badge_words_pattern = (
            r"Rising\s+star|Rising\s+fan|Top\s+fan|Valued\s+commenter|Conversation\s+starter|"
            r"New\s+follower|Anniversary\s+follower|Milestone\s+follower|Top\s+contributor|"
            r"Group\s+expert|Author|Follower|Follow|Admin|Moderator|"
            r"টপ\s+ফ্যান|রাইজিং\s+স্টার|রাইজিং\s+ফ্যান|লেখক|অনুসারী|মডারেটর|অ্যাডমিন"
        )

        # Clean private-use unicode glyphs, zero-width characters, and badge prefixes/suffixes from author_name
        author_name = re.sub(r"[\u200b-\u200d\ufeff\uE000-\uF8FF\U000F0000-\U0010FFFD]", "", author_name).strip()
        author_name = re.sub(rf"^(?:{badge_words_pattern})(?:\s*·|\s+)", "", author_name, flags=re.IGNORECASE).strip()
        author_name = re.sub(rf"(?:\s*·|\s+)(?:{badge_words_pattern})$", "", author_name, flags=re.IGNORECASE).strip()

        if target_name:
            target_name = re.sub(r"[\u200b-\u200d\ufeff\uE000-\uF8FF\U000F0000-\U0010FFFD]", "", target_name).strip()

        # Parse text lines from raw_text
        raw_lines = [line.strip() for line in raw.get("raw_text", "").split("\n") if line.strip()]

        if not author_name and raw_lines:
            author_name = raw_lines[0]
            author_name = re.sub(r"[\u200b-\u200d\ufeff\uE000-\uF8FF\U000F0000-\U0010FFFD]", "", author_name).strip()
            author_name = re.sub(rf"^(?:{badge_words_pattern})(?:\s*·|\s+)", "", author_name, flags=re.IGNORECASE).strip()
            author_name = re.sub(rf"(?:\s*·|\s+)(?:{badge_words_pattern})$", "", author_name, flags=re.IGNORECASE).strip()

        clean_author = author_name.lower()
        clean_target = target_name.lower() if target_name else ""

        filtered_lines = []
        for line in raw_lines:
            clean_l = re.sub(r"[\u200b-\u200d\ufeff\uE000-\uF8FF\U000F0000-\U0010FFFD]", "", line).strip()
            if not clean_l:
                continue
            cl_lower = clean_l.lower()

            # Exclude author header line
            if clean_author:
                if (
                    cl_lower == clean_author
                    or cl_lower.startswith(clean_author + " ·")
                    or cl_lower.endswith(" · " + clean_author)
                    or cl_lower == (clean_author + " ·")
                    or re.sub(rf"^(?:{badge_words_pattern})\s*", "", clean_l, flags=re.IGNORECASE).strip().lower() == clean_author
                ):
                    continue

            # Exclude replied-to target header line in replies
            if clean_target:
                if (
                    cl_lower == clean_target
                    or cl_lower.startswith(clean_target + " ·")
                    or cl_lower == (clean_target + "'s comment")
                ):
                    continue

            # Exclude Facebook badges & interactive action buttons & timestamps & reaction counts
            if re.match(
                rf"^(?:Like|Reply|Share|See\s+translation|"
                rf"{badge_words_pattern}|"
                r"\d+\s*(?:[smhdwy]|mo|yr)s?|"
                r"\d+\s*(?:sec|second|min|minute|hr|hour|day|week|month|year)s?(?:\s*ago)?|"
                r"(?:yesterday|just now)(?:\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?|"
                r"(?:\d{1,2}\s+[a-z]+|[a-z]+\s+\d{1,2})(?:,?\s*\d{4})?(?:\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?|"
                r"[\d,.]+[KM]?)(?:\s*·)?$",
                clean_l,
                re.IGNORECASE,
            ):
                continue
            if raw_comment_time and cl_lower == raw_comment_time.strip().lower():
                continue
            filtered_lines.append(clean_l)

        # In case the author name was concatenated at the start of the first line without a newline
        if author_name and filtered_lines:
            pattern_author_prefix = rf"^(?:{re.escape(author_name)})\s*"
            if re.match(pattern_author_prefix, filtered_lines[0], re.IGNORECASE):
                filtered_lines[0] = re.sub(pattern_author_prefix, "", filtered_lines[0], flags=re.IGNORECASE).strip()

        # In case the replied-to target name was also prepended to the first line
        if target_name and filtered_lines:
            pattern_target_prefix = rf"^(?:{re.escape(target_name)})\s*"
            if re.match(pattern_target_prefix, filtered_lines[0], re.IGNORECASE):
                filtered_lines[0] = re.sub(pattern_target_prefix, "", filtered_lines[0], flags=re.IGNORECASE).strip()

        while filtered_lines and not filtered_lines[0].strip():
            filtered_lines.pop(0)

        comment_text = "\n".join(filtered_lines).strip()
        # Remove any lingering standalone relative timestamp from comment text (e.g. trailing "1y", "2w")
        comment_text = re.sub(
            r"(?:\n|\r\n)?\b(?:\d+\s*(?:[smhdwy]|mo|yr)s?(?:\s*ago)?|yesterday|just now|(?:\d{1,2}\s+[a-z]+|[a-z]+\s+\d{1,2})(?:,?\s*\d{4})?(?:\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?)\s*$",
            "",
            comment_text,
            flags=re.IGNORECASE,
        ).strip()
        # If the remaining comment text is solely a relative timestamp, clear it so filter_blank properly identifies blank comments
        if re.match(
            r"^(?:\d+\s*(?:[smhdwy]|mo|yr)s?(?:\s*ago)?|yesterday|just now|(?:\d{1,2}\s+[a-z]+|[a-z]+\s+\d{1,2})(?:,?\s*\d{4})?(?:\s+at\s+\d{1,2}:\d{2}\s*(?:am|pm)?)?)$",
            comment_text,
            re.IGNORECASE,
        ):
            comment_text = ""

        # Handle comment ID
        raw_cid = raw.get("comment_id", "")
        if raw_cid:
            comment_id = decode_comment_id(raw_cid)
        else:
            comment_id = f"comment_{raw['dom_index'] + 1}"

        rx = reactions or {}
        likes = rx.get("likes", 0)
        love = rx.get("love", 0)
        care = rx.get("care", 0)
        haha = rx.get("haha", 0)
        wow = rx.get("wow", 0)
        sad = rx.get("sad", 0)
        angry = rx.get("angry", 0)
        sub_sum = sum([likes, love, care, haha, wow, sad, angry])
        raw_likes_cnt = parse_reaction_count(raw.get("likes_raw", 0))
        total_reactions = rx.get("reactions", 0) or max(sub_sum, raw_likes_cnt)
        if total_reactions > 0 and sub_sum == 0:
            likes = total_reactions

        # Determine post total reactions
        post_rx = post_reactions or getattr(self, "last_post_reactions", {}) or {}
        post_total = post_rx.get("reactions")
        if post_total is None or post_total == 0:
            post_total = sum(int(post_rx.get(k, 0)) for k in ["likes", "love", "care", "haha", "wow", "sad", "angry"])

        return {
            "Postid": post_id,
            "posturl": post_url,
            "main post time": main_post_time,
            "post total reactions": post_total,
            "commentid": comment_id,
            "commenter name": author_name,
            "comment text": comment_text,
            "comment date": comment_date,
            "comment time": comment_time,
            "reactions": total_reactions,
            "likes": likes,
            "love": love,
            "care": care,
            "haha": haha,
            "wow": wow,
            "sad": sad,
            "angry": angry,
            "original url": original_url or post_url,
            "_calc_dt": calc_dt,
            "_has_exact_time": has_exact,
        }

    def scrape(
        self,
        post_url: str,
        max_comments: Optional[int] = None,
        max_scrolls: int = 0,
        comment_filter: str = "all",
        output_csv: Optional[str] = None,
        filter_blank: bool = True,
        excel_safe: bool = False,
        include_replies: bool = False,
        append_csv: bool = False,
        original_url: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Main method to navigate, expand, parse comments, and optionally save to CSV."""
        if not self._browser or not self._context:
            raise RuntimeError("Scraper must be initialized. Use 'with FacebookCommentScraper() as scraper:'")

        raw_original_url = original_url or post_url
        post_url = normalize_facebook_url(post_url)
        logger.info("Navigating to Facebook post: %s", post_url)
        page = self._context.new_page()

        try:
            try:
                page.goto(post_url, wait_until="domcontentloaded", timeout=self.timeout)
            except Exception:
                logger.debug("Page goto timeout or exception, proceeding with loaded DOM...")
            page.wait_for_timeout(3000)

            # Step 1: Dismiss login overlays & consent banners
            self.dismiss_overlays(page)

            # Base timestamp for approximate relative time calculation
            now_dt = datetime.datetime.now()

            # Step 2: Extract post ID and main post timestamp
            post_id = extract_post_id(post_url, page)
            main_post_time = self.extract_post_time(page, base_time=now_dt)
            self.last_post_reactions = self.extract_post_reactions(page, post_id=post_id)
            logger.info("Target Post ID: '%s' | Main Post Time: '%s' | Reactions: %s", post_id, main_post_time, self.last_post_reactions)

            # Step 3: Switch comment filter to requested mode ('all', 'newest', 'most_relevant')
            self.switch_comment_filter(page, filter_mode=comment_filter)

            # Step 4: Expand comments
            self.expand_comments(
                page,
                max_comments=max_comments,
                max_scrolls=max_scrolls,
                include_replies=include_replies,
            )

            # Step 5: Expand truncated 'See more' comment text
            self.expand_see_more(page)

            # Step 6: Parse comments from DOM
            raw_items = self.parse_raw_comments(page, include_replies=include_replies)
            comment_type_desc = "comment & reply" if include_replies else "top-level comment"
            logger.info("Found %d %s article nodes.", len(raw_items), comment_type_desc)

            time_tracker = CommentTimeTracker(main_post_time=main_post_time)
            structured_records: List[Dict[str, Any]] = []
            seen_cids = set()

            articles_loc = page.locator('div[role="article"]')
            total_articles = articles_loc.count()

            for raw in raw_items:
                dom_idx = raw.get("dom_index", -1)
                exact_tooltip = None
                exact_hover_dt = None

                # Extract exact authentic comment timestamp via hover tooltip if available
                if 0 <= dom_idx < total_articles:
                    try:
                        exact_tooltip, exact_hover_dt = self.extract_comment_hover_time(page, dom_idx, base_time=now_dt)
                        if exact_tooltip:
                            logger.info("Found authentic Facebook comment timestamp: '%s'", exact_tooltip)
                    except Exception as e:
                        logger.debug("Hover timestamp extraction failed for comment %d: %s", dom_idx, e)

                # Extract comment reactions breakdown (likes, love, care, haha, wow, sad, angry)
                comment_reactions = self.extract_comment_reaction_breakdown(
                    page,
                    dom_index=dom_idx,
                    raw_likes=raw.get("likes_raw", "0"),
                    found_types=raw.get("reaction_types", []),
                )

                record = self.clean_comment_record(
                    raw=raw,
                    post_id=post_id,
                    post_url=post_url,
                    base_fetched_datetime=now_dt,
                    main_post_time=main_post_time,
                    time_tracker=time_tracker,
                    original_url=raw_original_url,
                    exact_tooltip=exact_tooltip,
                    exact_dt=exact_hover_dt,
                    reactions=comment_reactions,
                    post_reactions=self.last_post_reactions,
                )

                # Filter out blank comments (e.g. photo/sticker/gif only with no text)
                if filter_blank and (not record["comment text"] or not record["comment text"].strip()):
                    logger.debug("Skipping blank comment (image/sticker only) by '%s'.", record["commenter name"])
                    continue

                # Deduplicate by commentid, commenter name, and text
                cid_key = (record["commentid"], record["commenter name"], record["comment text"][:30])
                if cid_key in seen_cids:
                    continue
                seen_cids.add(cid_key)

                structured_records.append(record)
                if max_comments and len(structured_records) >= max_comments:
                    break

            # Facebook Comet DOM orders comments in reverse-chronological order (newest first).
            # We reverse structured_records so comments progress chronologically from oldest to newest.
            structured_records.reverse()

            # Assign realistic engagement timeline across all comments
            if structured_records:
                structured_records = time_tracker.assign_timeline(structured_records, base_time=now_dt)

            logger.info("Successfully processed %d unique %ss.", len(structured_records), comment_type_desc)

            if output_csv:
                self.save_to_csv(structured_records, output_csv, excel_safe=excel_safe, append=append_csv)

            return structured_records

        finally:
            page.close()

    @staticmethod
    def save_to_csv(records: List[Dict[str, Any]], filepath: str, excel_safe: bool = False, append: bool = False):
        """Saves or appends structured comment records into a CSV file with the required sections."""
        if not records and append:
            return

        if not records:
            logger.warning("No records to save. Creating empty CSV with headers.")

        fieldnames = [
            "Postid",
            "posturl",
            "main post time",
            "post total reactions",
            "commentid",
            "commenter name",
            "comment text",
            "comment date",
            "comment time",
            "reactions",
            "likes",
            "love",
            "care",
            "haha",
            "wow",
            "sad",
            "angry",
            "original url",
        ]

        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        file_exists = os.path.exists(filepath) and os.path.getsize(filepath) > 0
        if append and file_exists:
            try:
                with open(filepath, "r", encoding="utf-8-sig") as f_check:
                    reader = csv.reader(f_check)
                    first_line = next(reader, None)
                    if first_line and len(first_line) > 1:
                        fieldnames = [c.strip() for c in first_line]
            except Exception:
                pass
        mode = "a" if append else "w"
        with open(filepath, mode, newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if not append or not file_exists:
                writer.writeheader()
            for r in records:
                row = dict(r)
                if excel_safe:
                    if row.get("Postid"):
                        row["Postid"] = f'="{row["Postid"]}"'
                    if row.get("commentid"):
                        row["commentid"] = f'="{row["commentid"]}"'
                writer.writerow(row)

        action_word = "Appended" if append else "Saved"
        logger.info("%s %d records to CSV: %s", action_word, len(records), os.path.abspath(filepath))

