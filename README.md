# Facebook Comment Scraper - Portable Editions (Windows EXE & Linux AppImage)

Standalone, **on-the-go executables with zero installation** for both major operating systems:
* 🪟 **Windows**: `FacebookCommentScraper_GUI.exe` (Portable `.exe`, double-click to run)
* 🐧 **Linux**: `FacebookCommentScraper-x86_64.AppImage` (Universal portable `.AppImage`, runs on **Ubuntu, Debian, Fedora, Arch, Mint, openSUSE**, etc.)

---

## ⚡ What Makes It "On The Go" (Zero Installation)

1. **Self-Contained Executables**:
   * Windows: A single `.exe` file.
   * Linux: A single `.AppImage` file.
   * Neither requires Python, Git, Node.js, or terminal commands on the end user's PC.
2. **Built-in Multi-Browser Support & Fallback**:
   * Works with **Mozilla Firefox**, **Microsoft Edge**, **Google Chrome**, and **Chromium**.
   * In `auto` mode, it auto-detects whichever browser is present on the system.
   * Eliminates the need to download 150MB+ Playwright browser binaries on most machines.

---


## 💻 Local Build Scripts

* **Windows EXE**:
  ```cmd
  build_exe.bat
  ```
* **Linux AppImage**:
  ```bash
  ./build_appimage.sh
  ```

## 🚀 How to Run

### 1. Windows (`.exe`)
1. Double-click it. The desktop application window opens immediately.

### 2. Linux (`.AppImage`)
1. Make it executable:
   * **GUI**: Right-click the file ➔ Properties ➔ Permissions ➔ Check "Allow executing file as program".
   * **Terminal**:
     ```bash
     chmod +x FacebookCommentScraper-x86_64.AppImage
     ```
2. Double-click it (or run `./FacebookCommentScraper-x86_64.AppImage`).
   * *Tip*: Pass `--cli` to run in terminal CLI mode:
     ```bash
     ./FacebookCommentScraper-x86_64.AppImage --cli "POST_URL"
     ```

---
