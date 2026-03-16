#!/bin/bash
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_PARENT="$PROJECT_ROOT/src"
SRC_DIR="$SRC_PARENT/lufus"
GUI_DIR="$SRC_DIR/gui"
MAIN_SCRIPT="$SRC_DIR/__main__.py"

APPIMAGE_NAME="lufus-x86_64.AppImage"
LINUXDEPLOY_URL="https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage"
LINUXDEPLOY_QT_URL="https://github.com/linuxdeploy/linuxdeploy-plugin-qt/releases/download/continuous/linuxdeploy-plugin-qt-x86_64.AppImage"

# Prerequisites
command -v python3 >/dev/null 2>&1 || { echo "Python 3 required"; exit 1; }
command -v pip >/dev/null 2>&1 || { echo "pip required"; exit 1; }

# Install dependencies
pip install --upgrade pyinstaller pyqt6 psutil pyudev

# Download linuxdeploy (if missing)
[ -f linuxdeploy-x86_64.AppImage ] || wget "$LINUXDEPLOY_URL"
[ -f linuxdeploy-plugin-qt-x86_64.AppImage ] || wget "$LINUXDEPLOY_QT_URL"
chmod +x linuxdeploy*.AppImage

# Clean previous builds
rm -rf build dist AppDir

# PyInstaller – note: no comments after backslashes!
pyinstaller "$MAIN_SCRIPT" \
    --name lufus \
    --windowed \
    --paths "$SRC_PARENT" \
    --strip \
    --exclude-module tkinter \
    --collect-all PyQt6 \
    --collect-all psutil \
    --collect-all lufus \
    --hidden-import lufus.drives.autodetect_usb \
    --hidden-import lufus.drives.states \
    --hidden-import lufus.drives.find_usb \
    --hidden-import lufus.drives.formatting \
    --hidden-import lufus.gui.gui \
    --hidden-import lufus.gui.start_gui \
    --hidden-import lufus.writing.flash_usb \
    --hidden-import lufus.writing.flash_woeusb \
    --hidden-import lufus.writing.check_file_sig \
    --hidden-import lufus.writing.detect_windows \
    --hidden-import lufus.writing.flash_windows \
    --hidden-import lufus.writing.install_ventoy \
    --add-data "$GUI_DIR/themes:themes" \
    --add-data "$GUI_DIR/languages:languages" \
    --add-data "$GUI_DIR/assets:assets" \
    --noconfirm

# ===== DEBUG: Show bundle structure =====
echo "=== Bundle Contents ==="
if command -v tree &> /dev/null; then
    tree -L 4 dist/lufus/
else
    echo "dist/lufus/ top level:"
    ls -la dist/lufus/
    echo "dist/lufus/_internal/ :"
    ls -la dist/lufus/_internal/
    echo "Searching for lufus modules:"
    find dist/lufus -name "*.pyc" | head -20
fi
echo "========================"

# ===== Locate gui module =====
GUI_LOCATION=$(find dist/lufus -type d -name "gui" 2>/dev/null | head -1)
if [ -n "$GUI_LOCATION" ]; then
    echo "✅ gui module found at: $GUI_LOCATION"
else
    echo "❌ ERROR: gui module not found anywhere in dist/lufus/"
    echo "This means PyInstaller failed to include lufus.gui.gui."
    echo "Check that:"
    echo "  - All __init__.py files exist in drives/, gui/, writing/"
    echo "  - The import in start_gui.py is correct (e.g., from lufus.gui.gui import ...)"
    echo "  - There are no circular imports"
    exit 1
fi

# ===== Locate data folders =====
for folder in themes languages assets; do
    if [ -d "dist/lufus/$folder" ]; then
        echo "✅ $folder found at dist/lufus/$folder"
    elif [ -d "dist/lufus/_internal/$folder" ]; then
        echo "✅ $folder found at dist/lufus/_internal/$folder"
    else
        echo "⚠️  WARNING: $folder not found in dist/lufus/ or dist/lufus/_internal/"
    fi
done

# Prepare AppDir
mkdir -p AppDir/usr/bin
cp -r dist/lufus/* AppDir/usr/bin/

# Copy .desktop and icon
DESKTOP_SOURCE="$GUI_DIR/lufus.desktop"
ICON_SOURCE="$GUI_DIR/assets/lufus.png"

if [ -f "$DESKTOP_SOURCE" ]; then
    cp "$DESKTOP_SOURCE" AppDir/
else
    cat > AppDir/lufus.desktop <<EOF
[Desktop Entry]
Name=lufus
Exec=lufus
Icon=lufus
Type=Application
Categories=Utility;
EOF
fi

if [ -f "$ICON_SOURCE" ]; then
    cp "$ICON_SOURCE" AppDir/
else
    echo "Warning: lufus.png not found – icon will be missing."
fi

# Build AppImage
ARCH=x86_64 ./linuxdeploy-x86_64.AppImage \
    --appdir AppDir \
    --executable AppDir/usr/bin/lufus \
    --desktop-file AppDir/lufus.desktop \
    --icon-file AppDir/lufus.png \
    --output appimage

ls -lh "$APPIMAGE_NAME"
