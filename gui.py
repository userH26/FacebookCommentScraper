#!/usr/bin/env python3
"""
Facebook Comment Scraper - Modern Desktop GUI
Cross-platform graphical interface using Tkinter & ttk.
Zero external GUI dependencies (built into Python on Windows).
"""

import csv
import datetime
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from typing import List, Optional, Tuple

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
except ImportError:
    print("[ERROR] Tkinter is not installed on this system.")
    print(" - On Ubuntu/Debian: sudo apt-get install python3-tk")
    print(" - On Windows: Tkinter is included by default with standard Python.")
    sys.exit(1)

# Import scraper & helper functions
from facebook_scraper import FacebookCommentScraper, extract_post_id, logger, __version__
from main import CSVProgressTracker, read_urls_from_file, sanitize_filename


class QueueLoggingHandler(logging.Handler):
    """Thread-safe logging handler that directs log records to a Queue for GUI display."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self.log_queue.put(msg)
        except Exception:
            self.handleError(record)


def open_file_or_folder(path: str):
    """Cross-platform utility to open a file or directory with the default system application."""
    if not os.path.exists(path):
        messagebox.showerror("Not Found", f"Path does not exist:\n{path}")
        return

    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=True)
        else:
            subprocess.run(["xdg-open", path], check=True)
    except Exception as e:
        messagebox.showerror("Error Opening Path", f"Could not open path:\n{e}")


class ScraperGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"Facebook Comment Scraper v{__version__} - Portable Edition")
        self.root.geometry("820x760")
        self.root.minsize(740, 650)

        # Apply modern style theme
        self.style = ttk.Style()
        available_themes = self.style.theme_names()
        if "vista" in available_themes:
            self.style.theme_use("vista")
        elif "clam" in available_themes:
            self.style.theme_use("clam")

        self._setup_styles()

        # State variables
        self.mode_var = tk.StringVar(value="single")  # "single" or "batch"
        self.url_var = tk.StringVar()
        self.input_file_var = tk.StringVar()
        self.output_file_var = tk.StringVar()
        self.batch_size_var = tk.StringVar(value="0")
        self.max_comments_var = tk.StringVar(value="0")
        self.filter_var = tk.StringVar(value="all")
        self.include_replies_var = tk.BooleanVar(value=False)
        self.filter_blank_var = tk.BooleanVar(value=True)
        self.excel_safe_var = tk.BooleanVar(value=True)
        self.headful_var = tk.BooleanVar(value=False)
        self.retry_failed_var = tk.BooleanVar(value=False)
        self.delay_var = tk.StringVar(value="3.0")
        self.browser_var = tk.StringVar(value="auto")

        # Worker control
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.active_scraper: Optional[FacebookCommentScraper] = None
        self.log_queue: queue.Queue = queue.Queue()
        self.last_output_csv: Optional[str] = None

        # Build UI
        self._create_widgets()

        # Attach logging handler
        self.queue_handler = QueueLoggingHandler(self.log_queue)
        self.queue_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        )
        logger.addHandler(self.queue_handler)

        # Start log polling timer
        self.root.after(100, self._poll_log_queue)

    def _setup_styles(self):
        """Configures ttk styles for clean appearance."""
        self.style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"), foreground="#1877F2")
        self.style.configure("SubHeader.TLabel", font=("Segoe UI", 9), foreground="#64748B")
        self.style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"), foreground="#1E293B")
        self.style.configure("Status.TLabel", font=("Segoe UI", 9, "bold"))
        self.style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))

    def _create_widgets(self):
        # Top banner
        header_frame = ttk.Frame(self.root, padding="12 10 12 6")
        header_frame.pack(fill=tk.X)

        title_lbl = ttk.Label(
            header_frame,
            text=f"Facebook Public Post Comment Scraper v{__version__}",
            style="Header.TLabel",
        )
        title_lbl.pack(anchor=tk.W)

        sub_lbl = ttk.Label(
            header_frame,
            text="Made by amitesH26",
            style="SubHeader.TLabel",
        )
        sub_lbl.pack(anchor=tk.W, pady=(2, 0))

        # Main scrollable container or standard frame
        main_frame = ttk.Frame(self.root, padding="12 4 12 10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. Target Input Section
        input_group = ttk.LabelFrame(main_frame, text=" 1. Target Post or File ", style="Section.TLabelframe", padding=10)
        input_group.pack(fill=tk.X, pady=(0, 8))

        # Mode selector radio buttons
        mode_frame = ttk.Frame(input_group)
        mode_frame.pack(fill=tk.X, pady=(0, 6))

        single_rb = ttk.Radiobutton(
            mode_frame,
            text="Single Post URL",
            variable=self.mode_var,
            value="single",
            command=self._on_mode_change,
        )
        single_rb.pack(side=tk.LEFT, padx=(0, 20))

        batch_rb = ttk.Radiobutton(
            mode_frame,
            text="Batch URLs from File (.csv / .txt)",
            variable=self.mode_var,
            value="batch",
            command=self._on_mode_change,
        )
        batch_rb.pack(side=tk.LEFT)

        # Single URL field
        self.single_frame = ttk.Frame(input_group)
        self.single_frame.pack(fill=tk.X, pady=2)
        ttk.Label(self.single_frame, text="Facebook URL:", width=15).pack(side=tk.LEFT)
        self.url_entry = ttk.Entry(self.single_frame, textvariable=self.url_var)
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        paste_btn = ttk.Button(self.single_frame, text="Paste", width=7, command=self._paste_clipboard)
        paste_btn.pack(side=tk.RIGHT)

        # Batch File field
        self.batch_frame = ttk.Frame(input_group)
        ttk.Label(self.batch_frame, text="Links File:", width=15).pack(side=tk.LEFT)
        self.file_entry = ttk.Entry(self.batch_frame, textvariable=self.input_file_var)
        self.file_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        browse_file_btn = ttk.Button(self.batch_frame, text="Browse...", width=9, command=self._browse_input_file)
        browse_file_btn.pack(side=tk.RIGHT)

        # Batch Limit row (shown inside batch frame)
        self.batch_options_frame = ttk.Frame(input_group)
        ttk.Label(self.batch_options_frame, text="Batch Limit:", width=15).pack(side=tk.LEFT)
        batch_limit_spin = ttk.Spinbox(self.batch_options_frame, from_=0, to=99999, textvariable=self.batch_size_var, width=8)
        batch_limit_spin.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(self.batch_options_frame, text="(0 = scrape all pending links in file)").pack(side=tk.LEFT)
        retry_chk = ttk.Checkbutton(self.batch_options_frame, text="Retry previously failed links", variable=self.retry_failed_var)
        retry_chk.pack(side=tk.RIGHT)

        # 2. Output File Section
        output_group = ttk.LabelFrame(main_frame, text=" 2. Destination CSV ", style="Section.TLabelframe", padding=10)
        output_group.pack(fill=tk.X, pady=(0, 8))

        out_row = ttk.Frame(output_group)
        out_row.pack(fill=tk.X)
        ttk.Label(out_row, text="Save Comments To:", width=17).pack(side=tk.LEFT)
        out_entry = ttk.Entry(out_row, textvariable=self.output_file_var)
        out_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        browse_out_btn = ttk.Button(out_row, text="Browse...", width=9, command=self._browse_output_file)
        browse_out_btn.pack(side=tk.RIGHT)

        ttk.Label(output_group, text="Leave blank to automatically name based on Post ID or timestamp.", foreground="#64748B").pack(anchor=tk.W, pady=(4, 0))

        # 3. Scraping Options Section
        opts_group = ttk.LabelFrame(main_frame, text=" 3. Scraping Options ", style="Section.TLabelframe", padding=10)
        opts_group.pack(fill=tk.X, pady=(0, 8))

        row1 = ttk.Frame(opts_group)
        row1.pack(fill=tk.X, pady=2)

        ttk.Label(row1, text="Max Comments:", width=13).pack(side=tk.LEFT)
        max_c_spin = ttk.Spinbox(row1, from_=0, to=1000000, textvariable=self.max_comments_var, width=7)
        max_c_spin.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(row1, text="(0=All)").pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(row1, text="Sort:").pack(side=tk.LEFT, padx=(0, 4))
        filter_combo = ttk.Combobox(
            row1,
            textvariable=self.filter_var,
            values=["all", "newest", "most_relevant"],
            state="readonly",
            width=13,
        )
        filter_combo.pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(row1, text="Browser:").pack(side=tk.LEFT, padx=(0, 4))
        browser_combo = ttk.Combobox(
            row1,
            textvariable=self.browser_var,
            values=["auto", "brave", "chrome", "msedge", "firefox", "chromium"],
            state="readonly",
            width=10,
        )
        browser_combo.pack(side=tk.LEFT)

        row2 = ttk.Frame(opts_group)
        row2.pack(fill=tk.X, pady=(6, 2))

        ttk.Checkbutton(row2, text="Excel-Safe ID Protection (='ID')", variable=self.excel_safe_var).pack(side=tk.LEFT, padx=(0, 15))
        ttk.Checkbutton(row2, text="Filter Blank / Sticker-only Comments", variable=self.filter_blank_var).pack(side=tk.LEFT, padx=(0, 15))
        ttk.Checkbutton(row2, text="Include Comment Replies", variable=self.include_replies_var).pack(side=tk.LEFT)

        row3 = ttk.Frame(opts_group)
        row3.pack(fill=tk.X, pady=(4, 2))

        ttk.Checkbutton(row3, text="Show Visible Browser Window (Headful)", variable=self.headful_var).pack(side=tk.LEFT, padx=(0, 20))
        ttk.Label(row3, text="Delay between posts:").pack(side=tk.LEFT, padx=(0, 4))
        delay_spin = ttk.Spinbox(row3, from_=0.0, to=60.0, increment=0.5, textvariable=self.delay_var, width=5)
        delay_spin.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(row3, text="sec").pack(side=tk.LEFT)

        # 4. Controls & Progress
        ctl_frame = ttk.Frame(main_frame, padding=(0, 4, 0, 8))
        ctl_frame.pack(fill=tk.X)

        self.start_btn = tk.Button(
            ctl_frame,
            text="▶  START SCRAPING",
            bg="#10B981",
            fg="white",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=16,
            pady=6,
            command=self._start_scraping,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.stop_btn = tk.Button(
            ctl_frame,
            text="⏹  STOP",
            bg="#EF4444",
            fg="white",
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT,
            padx=16,
            pady=6,
            state=tk.DISABLED,
            command=self._stop_scraping,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 15))

        self.status_lbl = ttk.Label(ctl_frame, text="Status: Ready", style="Status.TLabel")
        self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.progress_bar = ttk.Progressbar(main_frame, orient=tk.HORIZONTAL, mode="indeterminate")
        self.progress_bar.pack(fill=tk.X, pady=(0, 8))

        # 5. Live Activity Log View
        log_group = ttk.LabelFrame(main_frame, text=" Live Activity Log ", style="Section.TLabelframe", padding=6)
        log_group.pack(fill=tk.BOTH, expand=True)

        self.log_text = ScrolledText(
            log_group,
            wrap=tk.WORD,
            height=10,
            bg="#0F172A",
            fg="#F1F5F9",
            insertbackground="white",
            font=("Consolas", 9),
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # Log toolbar
        log_tools = ttk.Frame(log_group)
        log_tools.pack(fill=tk.X)

        self.open_folder_btn = ttk.Button(log_tools, text="📂 Open Output Folder", command=self._open_output_folder)
        self.open_folder_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.open_csv_btn = ttk.Button(log_tools, text="📄 Open Comments CSV", command=self._open_output_csv)
        self.open_csv_btn.pack(side=tk.LEFT, padx=(0, 6))

        clear_log_btn = ttk.Button(log_tools, text="🧹 Clear Log", command=self._clear_log)
        clear_log_btn.pack(side=tk.RIGHT)

        # Initial mode setup
        self._on_mode_change()

    def _on_mode_change(self):
        """Toggles between single URL and batch file inputs."""
        mode = self.mode_var.get()
        if mode == "single":
            self.batch_frame.pack_forget()
            self.batch_options_frame.pack_forget()
            self.single_frame.pack(fill=tk.X, pady=2)
        else:
            self.single_frame.pack_forget()
            self.batch_frame.pack(fill=tk.X, pady=2)
            self.batch_options_frame.pack(fill=tk.X, pady=(4, 2))

    def _paste_clipboard(self):
        try:
            text = self.root.clipboard_get()
            self.url_var.set(text.strip())
        except Exception:
            pass

    def _browse_input_file(self):
        filename = filedialog.askopenfilename(
            title="Select Facebook URLs File",
            filetypes=[("CSV or Text Files", "*.csv *.txt"), ("CSV Files", "*.csv"), ("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if filename:
            self.input_file_var.set(filename)
            # Auto-suggest default output csv if not already set
            if not self.output_file_var.get():
                base, _ = os.path.splitext(filename)
                self.output_file_var.set(f"{base}_comments.csv")

    def _browse_output_file(self):
        filename = filedialog.asksaveasfilename(
            title="Choose Output CSV Destination",
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
        )
        if filename:
            self.output_file_var.set(filename)

    def _clear_log(self):
        self.log_text.delete("1.0", tk.END)

    def _poll_log_queue(self):
        """Continuously pulls log entries from queue and writes them to the ScrolledText widget."""
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
            except queue.Empty:
                break
        self.root.after(100, self._poll_log_queue)

    def _open_output_folder(self):
        target = self.last_output_csv or self.output_file_var.get()
        if target and os.path.exists(target):
            folder = os.path.dirname(os.path.abspath(target))
        else:
            folder = os.getcwd()
        open_file_or_folder(folder)

    def _open_output_csv(self):
        target = self.last_output_csv or self.output_file_var.get()
        if target and os.path.exists(target):
            open_file_or_folder(target)
        else:
            messagebox.showinfo("File Not Found", "No output CSV file has been generated yet.")

    def _set_ui_running(self, running: bool):
        if running:
            self.start_btn.config(state=tk.DISABLED, bg="#9CA3AF")
            self.stop_btn.config(state=tk.NORMAL, bg="#EF4444")
            self.progress_bar.config(mode="indeterminate")
            self.progress_bar.start(10)
        else:
            self.start_btn.config(state=tk.NORMAL, bg="#10B981")
            self.stop_btn.config(state=tk.DISABLED, bg="#9CA3AF")
            self.progress_bar.stop()
            self.progress_bar.config(mode="determinate", value=100)

    def _stop_scraping(self):
        if self.worker_thread and self.worker_thread.is_alive():
            self.status_lbl.config(text="Status: Stopping scraper...")
            self.stop_event.set()
            if self.active_scraper:
                try:
                    self.active_scraper.stop()
                except Exception:
                    pass

    def _start_scraping(self):
        mode = self.mode_var.get()
        tasks: List[Tuple[Optional[int], str]] = []
        tracker: Optional[CSVProgressTracker] = None
        input_file = None

        if mode == "single":
            url = self.url_var.get().strip()
            if not url:
                messagebox.showwarning("Missing URL", "Please enter a Facebook post URL.")
                return
            tasks = [(None, url)]
        else:
            input_file = self.input_file_var.get().strip()
            if not input_file or not os.path.exists(input_file):
                messagebox.showwarning("Invalid File", "Please select a valid CSV or TXT file.")
                return

            try:
                batch_limit = int(self.batch_size_var.get() or "0")
            except ValueError:
                batch_limit = 0

            _, ext = os.path.splitext(input_file.lower())
            if ext == ".csv":
                try:
                    tracker = CSVProgressTracker(
                        input_file,
                        retry_failed=self.retry_failed_var.get(),
                        reset_status=False,
                        fresh_start=False,
                    )
                    tasks = tracker.get_pending_tasks(batch_size=batch_limit)
                except Exception as e:
                    messagebox.showerror("CSV Error", f"Failed to load input CSV:\n{e}")
                    return
            else:
                try:
                    urls = read_urls_from_file(input_file)
                    if batch_limit > 0:
                        urls = urls[:batch_limit]
                    tasks = [(None, u) for u in urls]
                except Exception as e:
                    messagebox.showerror("File Error", f"Failed to read file:\n{e}")
                    return

        if not tasks:
            messagebox.showinfo("No Tasks", "No pending Facebook post URLs found to scrape!")
            return

        # Output CSV path
        output_csv = self.output_file_var.get().strip()
        if not output_csv:
            if input_file and input_file.lower().endswith(".csv"):
                base_name = os.path.splitext(os.path.basename(input_file))[0]
                output_csv = f"{base_name}_comments.csv"
            elif len(tasks) > 1:
                batch_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                output_csv = f"fb_comments_batch_{batch_ts}.csv"
            else:
                output_csv = sanitize_filename(tasks[0][1])

        self.last_output_csv = os.path.abspath(output_csv)

        # Parse numerical params
        try:
            max_comments = int(self.max_comments_var.get() or "0")
        except ValueError:
            max_comments = 0
        max_comments_val = None if max_comments <= 0 else max_comments

        try:
            delay_sec = float(self.delay_var.get() or "3.0")
        except ValueError:
            delay_sec = 3.0

        headless = not self.headful_var.get()
        filter_mode = self.filter_var.get()
        filter_blank = self.filter_blank_var.get()
        excel_safe = self.excel_safe_var.get()
        include_replies = self.include_replies_var.get()
        browser_choice = self.browser_var.get().strip().lower()

        self.stop_event.clear()
        self._set_ui_running(True)
        self.status_lbl.config(text=f"Status: Scraping {len(tasks)} post(s)...")

        # Launch in background thread
        self.worker_thread = threading.Thread(
            target=self._worker_scrape,
            args=(tasks, tracker, output_csv, max_comments_val, filter_mode, filter_blank, excel_safe, include_replies, headless, delay_sec, browser_choice),
            daemon=True,
        )
        self.worker_thread.start()

    def _worker_scrape(
        self,
        tasks: List[Tuple[Optional[int], str]],
        tracker: Optional[CSVProgressTracker],
        output_csv: str,
        max_comments_val: Optional[int],
        filter_mode: str,
        filter_blank: bool,
        excel_safe: bool,
        include_replies: bool,
        headless: bool,
        delay_sec: float,
        browser_choice: str = "auto",
    ):
        total_tasks = len(tasks)
        total_comments = 0
        successful_posts = 0
        failed_posts = 0

        self.active_scraper = FacebookCommentScraper(headless=headless, browser_name=browser_choice)

        try:
            with self.active_scraper as scraper:
                for idx, (row_idx, post_url) in enumerate(tasks, 1):
                    if self.stop_event.is_set():
                        logger.info("Scraping stopped by user.")
                        break

                    self.root.after(
                        0,
                        lambda i=idx, t=total_tasks: self.status_lbl.config(
                            text=f"Status: Scraping post {i} of {t}..."
                        ),
                    )

                    try:
                        records = scraper.scrape(
                            post_url=post_url,
                            max_comments=max_comments_val,
                            max_scrolls=0,
                            comment_filter=filter_mode,
                            output_csv=output_csv,
                            filter_blank=filter_blank,
                            excel_safe=excel_safe,
                            include_replies=include_replies,
                            append_csv=True,
                            original_url=post_url,
                        )
                        total_comments += len(records)
                        successful_posts += 1

                        if tracker and row_idx is not None:
                            tracker.update_task(
                                row_idx,
                                status="done",
                                comments_count=len(records),
                                reactions=scraper.last_post_reactions,
                            )

                        rx_str = ", ".join(f"{k}: {v}" for k, v in scraper.last_post_reactions.items() if v > 0)
                        logger.info(
                            "[%d/%d] Scraped %d comments from: %s (Reactions: [%s])",
                            idx,
                            total_tasks,
                            len(records),
                            post_url,
                            rx_str or "none",
                        )

                    except Exception as post_err:
                        failed_posts += 1
                        logger.error("Error scraping %s: %s", post_url, post_err)
                        if tracker and row_idx is not None:
                            tracker.update_task(row_idx, status="failed", comments_count=0)

                    # Inter-post delay
                    if idx < total_tasks and delay_sec > 0:
                        for _ in range(int(delay_sec * 10)):
                            if self.stop_event.is_set():
                                break
                            time.sleep(0.1)

            # Finished
            status_text = "Status: Completed!" if not self.stop_event.is_set() else "Status: Stopped by user."
            self.root.after(0, lambda: self.status_lbl.config(text=status_text))
            logger.info("Run finished. %d/%d succeeded, %d comments extracted.", successful_posts, total_tasks, total_comments)
            logger.info("Output saved to: %s", os.path.abspath(output_csv))

            self.root.after(
                0,
                lambda: messagebox.showinfo(
                    "Scraping Finished",
                    f"Run completed!\n\n"
                    f"• Posts: {successful_posts}/{total_tasks} succeeded\n"
                    f"• Total Comments: {total_comments}\n"
                    f"• Saved To:\n{os.path.abspath(output_csv)}",
                ),
            )

        except Exception as e:
            logger.error("Scraper execution encountered an error: %s", e, exc_info=True)
            self.root.after(0, lambda err=str(e): messagebox.showerror("Scraping Error", f"An error occurred:\n{err}"))
        finally:
            self.active_scraper = None
            self.root.after(0, lambda: self._set_ui_running(False))


def main():
    root = tk.Tk()
    app = ScraperGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
