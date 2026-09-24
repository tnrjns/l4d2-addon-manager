#!/usr/bin/env bash
# Sets up and launches the L4D2 Addon Manager on Linux.
#
# Run this from the folder containing l4d2_addon_manager.py:
#   chmod +x run_linux.sh
#   ./run_linux.sh
#
# What this does:
#   1. Installs the pywebview Python package
#   2. Installs GTK3 + WebKit2 (the most common Linux desktop backend for
#      pywebview) via whichever package manager it finds -- apt or pacman
#   3. Launches the app
#
# If you're on a distro/package-manager this script doesn't recognize, or
# GTK doesn't work for you, install the Qt fallback instead and run the
# app directly:
#   pip install --user PyQt5 PyQtWebEngine qtpy
#   python3 l4d2_addon_manager.py

set -e

echo "Installing pywebview..."
pip install --user pywebview 2>/dev/null || pip3 install --user pywebview

if python3 -c "import gi; gi.require_version('Gtk','3.0'); gi.require_version('WebKit2','4.1')" 2>/dev/null; then
    echo "GTK3 + WebKit2 already available."
elif command -v apt >/dev/null 2>&1; then
    echo "Installing GTK3 + WebKit2 via apt (Ubuntu/Debian/Mint/Pop!_OS) -- may ask for your password..."
    sudo apt install -y gir1.2-gtk-3.0 gir1.2-webkit2-4.1
elif command -v pacman >/dev/null 2>&1; then
    echo "Installing GTK3 + WebKit2 via pacman (Arch/CachyOS/Manjaro/EndeavourOS) -- may ask for your password..."
    sudo pacman -S --needed --noconfirm python-gobject webkit2gtk-4.1
else
    echo "Couldn't detect apt or pacman on this system."
    echo "Install GTK3 + WebKit2 GObject-introspection bindings for your distro manually,"
    echo "or use the Qt fallback instead:"
    echo "  pip install --user PyQt5 PyQtWebEngine qtpy"
    echo "Then run: python3 l4d2_addon_manager.py"
    exit 1
fi

echo ""
echo "If you're on a Wayland compositor (e.g. Hyprland, Sway) and the window"
echo "renders oddly or refuses to open, try forcing XWayland instead:"
echo "  GDK_BACKEND=x11 python3 l4d2_addon_manager.py"
echo ""
echo "Launching..."
python3 l4d2_addon_manager.py
