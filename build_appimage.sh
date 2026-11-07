#!/usr/bin/env bash
set -e

echo "============================================================"
echo "  Facebook Comment Scraper - Linux AppImage Builder"
echo "  (Portable on-the-go executable for all Linux distributions)"
echo "============================================================"
echo

# 1. Check Python
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] python3 not found."
    exit 1
fi

echo "[1/4] Ensuring Python requirements are installed..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

# 2. Compile to native C extensions via Cython (if available)
echo
echo "[2/4] Compiling modules & building standalone Linux binaries with PyInstaller..."
if [ -f "compile_native.py" ] && python3 compile_native.py 2>/dev/null; then
    echo "[OK] Native machine code compilation succeeded."
    GUI_STUB="build_stubs/launch_gui.py"
    CLI_STUB="build_stubs/launch_cli.py"
else
    echo "[NOTE] Using standard bytecode packaging."
    GUI_STUB="gui.py"
    CLI_STUB="main.py"
fi

python3 -OO -m PyInstaller --clean --noconfirm --onefile --windowed \
    --name "FacebookCommentScraper_GUI" \
    --collect-all playwright \
    "${GUI_STUB}"

python3 -OO -m PyInstaller --clean --noconfirm --onefile --console \
    --name "FacebookCommentScraper_CLI" \
    --collect-all playwright \
    "${CLI_STUB}"

if command -v strip &>/dev/null; then
    echo "Stripping binary debug symbols..."
    strip --strip-all dist/FacebookCommentScraper_GUI 2>/dev/null || true
    strip --strip-all dist/FacebookCommentScraper_CLI 2>/dev/null || true
fi

# 3. Assemble AppDir
echo
echo "[3/4] Assembling AppDir structure..."
APPDIR="$(pwd)/AppDir"
rm -rf "${APPDIR}"
mkdir -p "${APPDIR}"/usr/bin
mkdir -p "${APPDIR}"/usr/share/applications
mkdir -p "${APPDIR}"/usr/share/icons/hicolor/scalable/apps

# Copy binaries
cp dist/FacebookCommentScraper_GUI "${APPDIR}/usr/bin/"
cp dist/FacebookCommentScraper_CLI "${APPDIR}/usr/bin/"

# Copy desktop and icon assets
cp assets/facebook-comment-scraper.desktop "${APPDIR}/"
cp assets/facebook-comment-scraper.desktop "${APPDIR}/usr/share/applications/"
cp assets/facebook-comment-scraper.svg "${APPDIR}/"
cp assets/facebook-comment-scraper.svg "${APPDIR}/.DirIcon"
cp assets/facebook-comment-scraper.svg "${APPDIR}/usr/share/icons/hicolor/scalable/apps/"

# Create standard AppRun executable
cat << 'EOF' > "${APPDIR}/AppRun"
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}/usr/bin:${PATH}"

# Support running CLI mode if first arg is --cli or script called as cli
if [ "$1" = "--cli" ]; then
    shift
    exec "${HERE}/usr/bin/FacebookCommentScraper_CLI" "$@"
else
    exec "${HERE}/usr/bin/FacebookCommentScraper_GUI" "$@"
fi
EOF
chmod +x "${APPDIR}/AppRun"

# 4. Package with appimagetool
echo
echo "[4/4] Generating single-file .AppImage..."
mkdir -p dist

# Download appimagetool if not available
if ! command -v appimagetool &>/dev/null; then
    if [ ! -f "appimagetool" ]; then
        echo "Downloading appimagetool..."
        curl -fsSL -o appimagetool "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
        chmod +x appimagetool
    fi
    APPIMAGETOOL="./appimagetool"
else
    APPIMAGETOOL="appimagetool"
fi

export ARCH=x86_64
"${APPIMAGETOOL}" --appimage-extract-and-run "${APPDIR}" dist/FacebookCommentScraper-x86_64.AppImage

# Clean up temporary AppDir
rm -rf "${APPDIR}"

echo
echo "============================================================"
echo "  APPIMAGE BUILD SUCCEEDED!"
echo "============================================================"
echo " Output AppImage: dist/FacebookCommentScraper-x86_64.AppImage"
echo
echo " How to run on ANY Linux distribution:"
echo "   chmod +x dist/FacebookCommentScraper-x86_64.AppImage"
echo "   ./dist/FacebookCommentScraper-x86_64.AppImage"
echo "============================================================"
