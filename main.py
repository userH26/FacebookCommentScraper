#!/usr/bin/env python3
"""
CLI runner for the Facebook​‌‌​​​​‌​‌‌​‌‌​‌​‌‌​‌​​‌​‌‌‌​‌​​​‌‌​​‌​‌​‌‌‌​​‌‌​‌‌​‌​​​​​‌‌‌​‌​​‌‌​‌​​​​​‌‌​​‌​​​‌‌​‌‌​​​‌‌‌​‌​​‌‌​​​​‌​‌‌​‌‌​‌​‌‌​‌​​‌​‌‌‌​‌​​​​‌‌‌​‌​​‌‌​​​​‌​‌‌​‌‌‌​​‌‌‌​‌​​​‌‌​‌​​‌​‌‌​​‌‌‌​‌‌‌​​‌​​‌‌​​​​‌​‌‌‌​‌‌​​‌‌​‌​​‌​‌‌‌​‌​​​‌‌‌‌​​‌ Public Post Comment Scraper.
Runs locally using headless Playwright without touching any system components.
Outputs comments into CSV with the sections:
Postid, posturl, main post time, commentid, commenter name, comment text, comment date, comment time, comment likes.
Supports single URLs as well as batch reading from CSV or TXT files.
"""

import argparse
import csv
import datetime
import os
import sys
import time
from typing import List, Optional

from facebook_scraper import FacebookCommentScraper, extract_post_id, logger, __version__, _FB_COMET_ROUTING_KEY


def _pause_if_frozen():
    """Keeps the console window open when running as a packaged executable."""
    if getattr(sys, "frozen", False):
        try:
            input("\nPress Enter to exit...")
        except (KeyboardInterrupt, EOFError):
            pass


def sanitize_filename(url: str) -> str:
    """Generates a clean CSV filename based on post ID and timestamp."""
    post_id = extract_post_id(url)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if post_id:
        return f"fb_comments_{post_id}_{timestamp}.csv"
    return f"fb_comments_{timestamp}.csv"


def read_urls_from_file(filepath: str) -> List[str]:
    """Reads and parses Facebook URLs from a .csv or .txt file."""
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Input file not found: {filepath}")

    urls: List[str] = []
    _, ext = os.path.splitext(filepath.lower())

    if ext == ".csv":
        with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
            reader = csv.reader(f)
            url_col_idx = None

            for row in reader:
                if not row:
                    continue
                # Detect header row if url column index is not yet determined
                if url_col_idx is None:
                    for idx, cell in enumerate(row):
                        cleaned = cell.strip().lower()
                        if cleaned in ("url", "link", "post_url", "posturl", "permalink", "post_link", "fb_link", "facebook_url"):
                            url_col_idx = idx
                            break
                    if url_col_idx is not None:
                        continue  # Skip header row

                # Extract from detected column or search cells for URL patterns
                if url_col_idx is not None and url_col_idx < len(row):
                    val = row[url_col_idx].strip()
                    if val and (val.startswith("http") or "facebook.com" in val):
                        urls.append(val)
                else:
                    for cell in row:
                        val = cell.strip()
                        if val and (val.startswith("http") or "facebook.com" in val):
                            urls.append(val)
                            break
    else:
        # Default to text file parsing (.txt or other text format)
        with open(filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                urls.append(line)

    # Deduplicate while preserving original order
    seen = set()
    unique_urls = []
    for u in urls:
        u_clean = u.strip().strip('"').strip("'")
        if u_clean and u_clean not in seen:
            seen.add(u_clean)
            unique_urls.append(u_clean)

    return unique_urls


SUBSECTION_REACTION_COLS = ["likes", "love", "care", "haha", "wow", "sad", "angry"]
REACTION_COLS = ["reactions"] + SUBSECTION_REACTION_COLS


class CSVProgressTracker:
    """
    Manages loading, filtering, and updating input CSV tasks.
    Tracks 'status', 'scraped_at', and 'comments_scraped' columns directly in the input CSV.
    """

    def __init__(
        self,
        filepath: str,
        retry_failed: bool = False,
        reset_status: bool = False,
        fresh_start: bool = False,
    ):
        self.filepath = filepath
        self.retry_failed = retry_failed
        self.reset_status = reset_status
        self.fresh_start = fresh_start
        self.fieldnames: List[str] = []
        self.rows: List[dict] = []
        self.url_col: str = "url"
        self._load()

    def _load(self):
        if not os.path.exists(self.filepath):
            raise FileNotFoundError(f"Input CSV not found: {self.filepath}")

        with open(self.filepath, "r", encoding="utf-8-sig", errors="ignore") as f:
            reader = csv.reader(f)
            raw_rows = [r for r in reader if r and any(c.strip() for c in r)]

        if not raw_rows:
            self.fieldnames = ["url", "status", "scraped_at", "comments_scraped"] + REACTION_COLS
            self.rows = []
            return

        first_row = raw_rows[0]
        header_candidates = {"url", "link", "post_url", "posturl", "permalink", "post_link", "fb_link", "facebook_url"}
        is_header = False
        detected_url_col = None

        for cell in first_row:
            cleaned = cell.strip().lower()
            if cleaned in header_candidates:
                is_header = True
                detected_url_col = cell.strip()
                break

        if not is_header:
            has_link = any("facebook.com" in cell or cell.strip().startswith("http") for cell in first_row)
            if not has_link and len(first_row) > 1:
                is_header = True
                detected_url_col = first_row[0].strip()

        if is_header:
            self.fieldnames = [c.strip() for c in first_row]
            data_rows = raw_rows[1:]
            self.url_col = detected_url_col or self.fieldnames[0]
        else:
            self.fieldnames = ["url"]
            self.url_col = "url"
            data_rows = raw_rows

        if self.fresh_start:
            url_idx = 0
            if is_header and self.url_col in self.fieldnames:
                url_idx = self.fieldnames.index(self.url_col)
            urls = []
            for r in data_rows:
                if url_idx < len(r):
                    u = r[url_idx].strip().strip('"').strip("'")
                    if u:
                        urls.append(u)
                elif r:
                    u = r[0].strip().strip('"').strip("'")
                    if u:
                        urls.append(u)

            # Rewrite input CSV on disk keeping ONLY Column 1 (URL list)
            tmp_path = self.filepath + ".tmp"
            with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([self.url_col])
                for u in urls:
                    writer.writerow([u])
            os.replace(tmp_path, self.filepath)

            # Setup memory structures for tracking during run
            self.fieldnames = [self.url_col] + ["status", "scraped_at", "comments_scraped"] + REACTION_COLS
            self.rows = []
            for u in urls:
                row_dict = {col: "" for col in self.fieldnames}
                row_dict[self.url_col] = u
                self.rows.append(row_dict)
            return

        for col in ["status", "scraped_at", "comments_scraped"] + REACTION_COLS:
            if col not in self.fieldnames:
                self.fieldnames.append(col)

        self.rows = []
        for r in data_rows:
            row_dict = {}
            for idx, col in enumerate(self.fieldnames):
                if idx < len(r):
                    row_dict[col] = r[idx].strip()
                else:
                    row_dict[col] = ""

            if self.reset_status:
                row_dict["status"] = ""
                row_dict["scraped_at"] = ""
                row_dict["comments_scraped"] = ""
                for col in REACTION_COLS:
                    row_dict[col] = ""

            self.rows.append(row_dict)

        if self.reset_status:
            self._save()

    def get_pending_tasks(self, batch_size: int = 0) -> List[tuple]:
        """
        Returns a list of (row_index, url) for posts that have not yet been marked 'done'.
        If batch_size > 0, returns only up to batch_size items.
        """
        pending = []
        for idx, row in enumerate(self.rows):
            status = (row.get("status") or "").strip().lower()
            url = (row.get(self.url_col) or "").strip().strip('"').strip("'")
            if not url or not (url.startswith("http") or "facebook.com" in url):
                continue

            if status in ("done", "completed", "success"):
                continue

            if status in ("failed", "error") and not self.retry_failed:
                continue

            pending.append((idx, url))

        if batch_size > 0:
            return pending[:batch_size]
        return pending

    def update_task(
        self,
        row_idx: int,
        status: str,
        comments_count: int = 0,
        reactions: Optional[dict] = None,
    ):
        """Updates row status, post reaction breakdown, and saves immediately to disk."""
        if 0 <= row_idx < len(self.rows):
            self.rows[row_idx]["status"] = status
            self.rows[row_idx]["scraped_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.rows[row_idx]["comments_scraped"] = str(comments_count)
            if reactions:
                total_rx = reactions.get("reactions")
                if total_rx is None or total_rx == 0:
                    total_rx = sum(int(reactions.get(k, 0)) for k in SUBSECTION_REACTION_COLS)
                self.rows[row_idx]["reactions"] = str(total_rx)
                for k in SUBSECTION_REACTION_COLS:
                    self.rows[row_idx][k] = str(reactions.get(k, 0))
            self._save()

    def _save(self):
        """Atomically saves rows back to the input CSV file."""
        tmp_path = self.filepath + ".tmp"
        try:
            with open(tmp_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self.fieldnames, extrasaction="ignore")
                writer.writeheader()
                for r in self.rows:
                    writer.writerow(r)
            os.replace(tmp_path, self.filepath)
        except Exception as e:
            logger.error("Failed saving progress to input CSV '%s': %s", self.filepath, e)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def stats(self) -> tuple:
        """Returns (total, completed, failed, pending)."""
        done = sum(1 for r in self.rows if (r.get("status") or "").strip().lower() in ("done", "completed", "success"))
        failed = sum(1 for r in self.rows if (r.get("status") or "").strip().lower() in ("failed", "error"))
        pending = len(self.rows) - done - (0 if self.retry_failed else failed)
        return len(self.rows), done, failed, max(0, pending)


def main():
    parser = argparse.ArgumentParser(
        description="Facebook Public Post Comment Scraper using Headless Playwright (Runs 100% locally)."
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program's version number and exit.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="URL of the public Facebook post to scrape, OR path to a .csv / .txt file containing URLs.",
    )
    parser.add_argument(
        "-i",
        "--input-file",
        help="Path to CSV or TXT file containing Facebook post URLs to scrape.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Path to output CSV file (default: auto-generated based on post ID/timestamp or batch timestamp).",
    )
    parser.add_argument(
        "-b",
        "--batch-size",
        type=int,
        default=0,
        help="Number of pending links to scrape in this run (e.g. -b 10). Default is 0 (all pending links).",
    )
    parser.add_argument(
        "-m",
        "--max-comments",
        type=int,
        default=0,
        help="Maximum number of comments to scrape per post (default: 0 for unlimited, or specify a limit like -m 100).",
    )
    parser.add_argument(
        "-s",
        "--max-scrolls",
        type=int,
        default=0,
        help="Maximum pagination scrolls for loading comments (default: 0 for unlimited until no more comments exist).",
    )
    parser.add_argument(
        "-f",
        "--filter",
        choices=["all", "newest", "most_relevant"],
        default="all",
        help="Comment filter / sorting mode: 'all' (All comments, default), 'newest' (Newest first), or 'most_relevant' (Top comments).",
    )
    parser.add_argument(
        "-r",
        "--include-replies",
        action="store_true",
        help="Include comment replies as well as top-level comments (default is top-level comments only).",
    )
    parser.add_argument(
        "--include-blank",
        action="store_true",
        help="Include blank comments (e.g. image-only or sticker-only comments without text). By default, blank comments are filtered out.",
    )
    parser.add_argument(
        "--excel-safe",
        action="store_true",
        help="Format large ID numbers (Postid, commentid) as =\"ID\" in CSV to prevent Excel / LibreOffice Calc from converting them to scientific notation.",
    )
    parser.add_argument(
        "--browser",
        choices=["auto", "chromium", "chrome", "msedge", "brave", "firefox"],
        default="auto",
        help="Browser engine to use: 'auto' (default: tries Chromium -> Brave -> Edge -> Chrome -> Firefox), 'chromium', 'chrome', 'msedge', 'brave', or 'firefox'.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run browser with visible UI (default is headless).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="Delay in seconds between scraping multiple posts (default: 3.0s).",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry scraping posts previously marked as 'failed' in the input CSV.",
    )
    parser.add_argument(
        "--reset-status",
        action="store_true",
        help="Reset status of all links in the input CSV back to pending.",
    )
    parser.add_argument(
        "--fresh-start",
        action="store_true",
        help="Fresh start: strips all tracking/reaction columns from the input CSV (retaining only column 1 / URLs) and deletes the destination comments CSV.",
    )
    parser.add_argument(
        "--reset-only",
        action="store_true",
        help="Used with --fresh-start or --reset-status: resets the files and exits immediately without scraping.",
    )
    parser.add_argument(
        "--cookies",
        help="Path to a JSON file containing Facebook session cookies (optional).",
    )
    parser.add_argument(
        "--storage-state",
        help="Path to Playwright storage_state.json file (optional).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=35000,
        help="Page navigation timeout in milliseconds (default: 35000).",
    )

    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the desktop graphical user interface.",
    )

    args = parser.parse_args()

    if args.gui:
        try:
            from gui import main as launch_gui
            launch_gui()
            sys.exit(0)
        except Exception as e:
            print(f"Error launching GUI: {e}")
            _pause_if_frozen()
            sys.exit(1)

    input_file = args.input_file
    raw_target = args.url

    # Check if positional argument is actually an existing file or a file path
    if not input_file and raw_target:
        if os.path.isfile(raw_target) or raw_target.lower().endswith((".csv", ".txt")):
            input_file = raw_target
            raw_target = None

    tracker: Optional[CSVProgressTracker] = None
    tasks: List[tuple] = []  # List of (row_idx_or_None, url)

    if input_file:
        _, ext = os.path.splitext(input_file.lower())
        if ext == ".csv":
            try:
                tracker = CSVProgressTracker(
                    input_file,
                    retry_failed=args.retry_failed,
                    reset_status=args.reset_status,
                    fresh_start=args.fresh_start,
                )
                tasks = tracker.get_pending_tasks(batch_size=args.batch_size)
                total_rows, done_count, failed_count, pending_count = tracker.stats()
                print("=" * 65)
                print(f" CSV Progress Tracker: '{input_file}'")
                print("=" * 65)
                print(f"Total Rows:        {total_rows}")
                print(f"Already Done:      {done_count}")
                if failed_count > 0:
                    print(f"Previously Failed: {failed_count}" + (" (Retrying)" if args.retry_failed else " (Skipping, pass --retry-failed to re-attempt)"))
                print(f"Pending to Scrape: {pending_count}")
                if args.batch_size > 0:
                    print(f"Batch Limit:       Processing next {len(tasks)} pending post(s) this run")
                print("=" * 65)
            except Exception as e:
                print(f"Error loading input CSV '{input_file}': {e}")
                sys.exit(1)
        else:
            try:
                urls = read_urls_from_file(input_file)
                if args.batch_size > 0:
                    urls = urls[:args.batch_size]
                tasks = [(None, u) for u in urls]
                print(f"Loaded {len(tasks)} post URL(s) from '{input_file}'.")
            except Exception as e:
                print(f"Error reading file '{input_file}': {e}")
                sys.exit(1)
    elif raw_target:
        tasks = [(None, raw_target)]
    else:
        print("=" * 65)
        print(" Facebook Public Post Comment Scraper (Headless Playwright)")
        print("=" * 65)
        try:
            entered = input("Enter Facebook post URL or path to links file (.csv/.txt): ").strip()
            if os.path.isfile(entered) or entered.lower().endswith(".csv"):
                tracker = CSVProgressTracker(
                    entered,
                    retry_failed=args.retry_failed,
                    reset_status=args.reset_status,
                    fresh_start=args.fresh_start,
                )
                tasks = tracker.get_pending_tasks(batch_size=args.batch_size)
            elif os.path.isfile(entered) or entered.lower().endswith(".txt"):
                urls = read_urls_from_file(entered)
                if args.batch_size > 0:
                    urls = urls[:args.batch_size]
                tasks = [(None, u) for u in urls]
            elif entered:
                tasks = [(None, entered)]
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)

    if not tasks:
        if tracker:
            print("All posts in this CSV are already marked as 'done'! Nothing to scrape.")
            print("Tip: Use --reset-status to clear progress, or --retry-failed to re-attempt failed posts.")
        else:
            print("Error: No valid Facebook post URLs found.")
        _pause_if_frozen()
        sys.exit(0)

    total_tasks = len(tasks)
    max_comments = None if args.max_comments <= 0 else args.max_comments
    max_scrolls = 0 if args.max_scrolls <= 0 else args.max_scrolls
    headless = not args.headful

    # Default output CSV name
    if args.output:
        output_csv = args.output
    elif input_file and ext == ".csv":
        base_name = os.path.splitext(os.path.basename(input_file))[0]
        output_csv = f"{base_name}_comments.csv"
    elif total_tasks > 1:
        batch_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_csv = f"fb_comments_batch_{batch_ts}.csv"
    else:
        output_csv = sanitize_filename(tasks[0][1])

    if args.fresh_start:
        if os.path.exists(output_csv):
            try:
                os.remove(output_csv)
                print(f"[FRESH START] Deleted previous comments output file: '{output_csv}'")
            except Exception as e:
                logger.warning("Could not remove previous comments file '%s': %s", output_csv, e)
        else:
            print(f"[FRESH START] Comments output file '{output_csv}' is ready for a clean start.")
        if input_file and tracker:
            print(f"[FRESH START] Cleaned '{input_file}': all extra columns deleted, retaining only Column 1 ('{tracker.url_col}').")

    if args.reset_only:
        print("=" * 65)
        print(" RESET COMPLETE (--reset-only)")
        if input_file and tracker:
            if args.fresh_start:
                print(f" Input File:   '{input_file}' reset to Column 1 only ({total_tasks} pending posts).")
                print(f" Comments:     '{output_csv}' deleted.")
            else:
                print(f" Input File:   '{input_file}' status cleared to pending ({total_tasks} pending posts).")
        print("=" * 65)
        _pause_if_frozen()
        sys.exit(0)

    print("-" * 65)
    print(f"Posts in this run: {total_tasks} post(s)")
    print(f"Output CSV:        {output_csv}")
    print(f"Max comments/post: {'Unlimited' if max_comments is None else max_comments}")
    print(f"Comment filter:    {args.filter}")
    print(f"Blank comments:    {'Included' if args.include_blank else 'Filtered out (No text/image only)'}")
    print(f"Excel safe IDs:    {args.excel_safe}")
    print(f"Replies:           {'Included' if args.include_replies else 'Disabled (Top-level comments only)'}")
    print(f"Browser engine:    {args.browser.capitalize()}")
    print(f"Headless mode:     {headless}")
    if total_tasks > 1:
        print(f"Delay per post:    {args.delay}s")
    print("-" * 65)

    scraper = FacebookCommentScraper(
        headless=headless,
        timeout=args.timeout,
        cookies_file=args.cookies,
        storage_state_file=args.storage_state,
        browser_name=args.browser,
    )

    total_scraped_records = 0
    successful_posts = 0
    failed_posts = 0

    try:
        with scraper:
            for idx, (row_idx, post_url) in enumerate(tasks, 1):
                if total_tasks > 1:
                    print(f"\n[{idx}/{total_tasks}] Scraping: {post_url}")
                try:
                    records = scraper.scrape(
                        post_url=post_url,
                        max_comments=max_comments,
                        max_scrolls=max_scrolls,
                        comment_filter=args.filter,
                        output_csv=output_csv,
                        filter_blank=not args.include_blank,
                        excel_safe=args.excel_safe,
                        include_replies=args.include_replies,
                        append_csv=True,
                        original_url=post_url,
                    )
                    total_scraped_records += len(records)
                    successful_posts += 1

                    # Immediately mark as 'done' in input CSV
                    if tracker and row_idx is not None:
                        tracker.update_task(
                            row_idx,
                            status="done",
                            comments_count=len(records),
                            reactions=scraper.last_post_reactions,
                        )

                    if total_tasks > 1:
                        rx_summary = ", ".join(f"{k}: {v}" for k, v in scraper.last_post_reactions.items() if v > 0)
                        rx_str = f" | Reacts: [{rx_summary}]" if rx_summary else ""
                        print(f"  -> Extracted {len(records)} comments (Status: done{rx_str}).")
                except Exception as e:
                    failed_posts += 1
                    logger.error("Failed scraping URL '%s': %s", post_url, e)

                    # Mark as 'failed' in input CSV
                    if tracker and row_idx is not None:
                        tracker.update_task(row_idx, status="failed", comments_count=0)

                    if total_tasks == 1:
                        raise e
                    print(f"  -> Skipping to next URL due to error (Status: failed).")

                # Polite delay between multiple posts
                if idx < total_tasks and args.delay > 0:
                    time.sleep(args.delay)

        print("\n" + "=" * 65)
        print(" BATCH RUN COMPLETED")
        print("=" * 65)
        print(f"Posts in this run: {successful_posts}/{total_tasks} succeeded" + (f" ({failed_posts} failed)" if failed_posts else ""))
        print(f"Comments Scraped:  {total_scraped_records}")
        print(f"Output CSV:        {os.path.abspath(output_csv)}")

        if tracker:
            _, final_done, final_failed, remaining_pending = tracker.stats()
            print("-" * 65)
            print(f"Overall Progress:  {final_done} completed | {final_failed} failed | {remaining_pending} remaining")
            if remaining_pending > 0:
                print(f"Next step:         Run the same command again to scrape the next batch!")
            else:
                print(f"Status:            100% of links in '{tracker.filepath}' are complete!")
        print("=" * 65)
        _pause_if_frozen()

    except Exception as e:
        logger.error("Scraping execution stopped: %s", e, exc_info=True)
        _pause_if_frozen()
        sys.exit(1)


if __name__ == "__main__":
    main()
