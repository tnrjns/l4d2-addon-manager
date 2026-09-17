"""
L4D2 Workshop Addon Manager (fancy edition)
--------------------------------------------
Paste a Steam Workshop URL for a Left 4 Dead 2 addon, click Install, and
this tool will:

  1. Extract the workshop item ID from the URL
  2. Download SteamCMD automatically (first run only)
  3. Use SteamCMD (anonymous login) to download the workshop item
  4. Copy/rename the .vpk into your L4D2 addons folder as pak01_dir.vpk
     (or pak01_XXX.vpk parts, if the addon ships as a multi-part vpk)
  5. Add a SearchPaths entry for it in gameinfo.txt (with an automatic
     one-time backup: gameinfo.txt.bak)
  6. Track installed addons so you can remove them again with one click

Requires: Windows, Python 3.8+, and the 'customtkinter' package:

    pip install customtkinter

Run with:  python l4d2_addon_manager.py
"""

import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from typing import Optional

try:
    import customtkinter as ctk
    import tkinter as tk
    from tkinter import filedialog, messagebox
except ImportError:
    print("This app needs the 'customtkinter' package.")
    print("Install it with:  pip install customtkinter")
    sys.exit(1)

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
STEAMCMD_DIR = APP_DIR / "steamcmd"
STEAMCMD_EXE = STEAMCMD_DIR / "steamcmd.exe"
STEAMCMD_ZIP_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
L4D2_APPID = "550"
L4D2_EXE_NAME = "left4dead2.exe"

DEFAULT_L4D2_PATHS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Left 4 Dead 2",
    r"C:\Steam\steamapps\common\Left 4 Dead 2",
]

# Purple & grey theme
ACCENT = "#8b5cf6"        # violet-500
ACCENT_HOVER = "#7c3aed"  # violet-600
ACCENT_GLOW = "#a78bfa"   # violet-400, brighter — used for the pulse animation

DANGER = "#ef4444"        # red, reserved for destructive actions (Remove)
DANGER_HOVER = "#3a1f22"
BG_CARD = "#1d1d22"
BG_APP = "#131316"
TEXT_DIM = "#9d9aa8"
NEUTRAL_BTN = "#2a2a30"
NEUTRAL_BTN_HOVER = "#38383f"


def blend_hex(c1: str, c2: str, t: float) -> str:
    """Linearly blend two '#rrggbb' colors; t=0 -> c1, t=1 -> c2."""
    c1, c2 = c1.lstrip("#"), c2.lstrip("#")
    r1, g1, b1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
    r2, g2, b2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config():
    cfg = {"game_path": "", "installed": {}}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except Exception:
            pass
    cfg.setdefault("game_path", "")
    cfg.setdefault("installed", {})
    cfg.setdefault("launch_options", "")
    # Back-compat: older versions stored installed[item_id] as a plain
    # folder-name string. Normalize to {"folder_name": ..., "enabled": ...}.
    for item_id, value in list(cfg["installed"].items()):
        if isinstance(value, str):
            cfg["installed"][item_id] = {"folder_name": value, "enabled": True}
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def guess_game_path():
    for p in DEFAULT_L4D2_PATHS:
        if Path(p).exists():
            return p
    return ""


# --------------------------------------------------------------------------
# Core logic (unchanged from the simple version, just called from a
# fancier UI)
# --------------------------------------------------------------------------

def extract_workshop_id(url_or_id: str) -> str:
    url_or_id = url_or_id.strip()
    if url_or_id.isdigit():
        return url_or_id
    match = re.search(r"[?&]id=(\d+)", url_or_id)
    if match:
        return match.group(1)
    raise ValueError(
        "Couldn't find a workshop item ID in that input. Paste the full "
        "workshop URL (e.g. https://steamcommunity.com/sharedfiles/filedetails/?id=123456789) "
        "or just the numeric ID."
    )


INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_folder_name(name: str) -> str:
    name = INVALID_FOLDER_CHARS.sub("", name).strip()
    name = re.sub(r"\s+", " ", name)
    name = name.rstrip(". ")  # Windows disallows trailing dots/spaces
    if not name:
        name = "addon"
    return name[:60]


def fetch_workshop_title(item_id: str, log) -> Optional[str]:
    """Look up the addon's real title via Steam's public web API.
    Returns None (and logs a note) if it can't be fetched — the caller
    should fall back to a workshop_<id> style name in that case."""
    url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    data = urllib.parse.urlencode(
        {"itemcount": 1, "publishedfileids[0]": item_id}
    ).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        details = payload.get("response", {}).get("publishedfiledetails", [])
        if details and details[0].get("result") == 1:
            title = details[0].get("title", "").strip()
            if title:
                return title
        log("Couldn't look up this item's title from Steam (item may be missing/private); using its ID instead.")
    except Exception as e:
        log(f"Couldn't reach Steam to look up the addon title ({e}); using its ID instead.")
    return None


def ensure_steamcmd(log):
    if STEAMCMD_EXE.exists():
        return
    log("SteamCMD not found. Downloading it (one-time setup)...")
    STEAMCMD_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = STEAMCMD_DIR / "steamcmd.zip"
    urllib.request.urlretrieve(STEAMCMD_ZIP_URL, zip_path)
    log("Extracting SteamCMD...")
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(STEAMCMD_DIR)
    zip_path.unlink(missing_ok=True)
    log("Running SteamCMD once to let it finish bootstrapping/updating...")
    subprocess.run(
        [str(STEAMCMD_EXE), "+quit"],
        cwd=str(STEAMCMD_DIR),
        capture_output=True,
        text=True,
    )
    if not STEAMCMD_EXE.exists():
        raise RuntimeError("SteamCMD download/extract failed.")
    log("SteamCMD is ready.")


def download_workshop_item(item_id: str, log):
    log(f"Downloading workshop item {item_id} via SteamCMD (anonymous login)...")
    cmd = [
        str(STEAMCMD_EXE),
        "+login", "anonymous",
        "+workshop_download_item", L4D2_APPID, item_id,
        "+quit",
    ]
    proc = subprocess.run(
        cmd, cwd=str(STEAMCMD_DIR), capture_output=True, text=True
    )
    log(proc.stdout[-3000:])
    if proc.returncode != 0 or "Success" not in proc.stdout:
        raise RuntimeError(
            "SteamCMD did not report success downloading that item. "
            "It may be private, removed, or require you to own the game / "
            "be logged into an account that can access it."
        )

    content_dir = STEAMCMD_DIR / "steamapps" / "workshop" / "content" / L4D2_APPID / item_id
    if not content_dir.exists():
        raise RuntimeError(f"Expected downloaded content at {content_dir} but it wasn't found.")
    return content_dir


def install_addon_files(content_dir: Path, game_path: Path, folder_name: str, log):
    all_files = sorted(f for f in content_dir.rglob("*") if f.is_file())
    log(f"Downloaded {len(all_files)} file(s):")
    for f in all_files[:25]:
        log(f"  - {f.relative_to(content_dir)}")
    if len(all_files) > 25:
        log(f"  ...and {len(all_files) - 25} more")

    if not all_files:
        raise RuntimeError(
            f"The download folder is empty: {content_dir}\n"
            "SteamCMD reported success but no files showed up. Try running the "
            "install again — Workshop downloads occasionally need a retry."
        )

    vpk_files = [f for f in all_files if f.suffix.lower() == ".vpk"]

    dest_dir = game_path / folder_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    if vpk_files:
        already_multipart = any(re.search(r"pak\d+_\d+\.vpk$", f.name, re.I) for f in vpk_files)
        if len(vpk_files) == 1 and not already_multipart:
            dest = dest_dir / "pak01_dir.vpk"
            shutil.copy2(vpk_files[0], dest)
            log(f"Copied {vpk_files[0].name} -> {dest}")
        else:
            for f in vpk_files:
                dest = dest_dir / f.name
                shutil.copy2(f, dest)
                log(f"Copied {f.name} -> {dest}")
    elif len(all_files) == 1:
        # Some older Workshop uploads get stored by Steam without their
        # original .vpk extension (often showing up as a numbered file or
        # a ".bin"). It's not actually loose content — it's vpk data that
        # lost its extension somewhere in Steam's pipeline. Treat a lone
        # unrecognized file the same as a single vpk.
        only_file = all_files[0]
        dest = dest_dir / "pak01_dir.vpk"
        shutil.copy2(only_file, dest)
        log(
            f"Only file found is '{only_file.name}' (no .vpk extension) — this is a known "
            f"quirk with some older Workshop uploads where Steam strips the original "
            f"extension. Treating it as vpk content and saving it as {dest}"
        )
    else:
        # Multiple loose files with no vpk among them (materials/, models/,
        # sound/, etc.) — a real loose-file addon. Source engine can load a
        # loose-file folder the same way it loads a vpk via a SearchPaths
        # "Game" entry, so just mirror the folder as-is.
        log("No .vpk found and multiple files present — this addon ships as loose files. Copying the folder as-is.")
        for f in all_files:
            rel = f.relative_to(content_dir)
            dest = dest_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        log(f"Copied {len(all_files)} loose file(s) into {dest_dir}")


def update_gameinfo(game_path: Path, folder_name: str, log, remove=False):
    gameinfo_path = game_path / "left4dead2" / "gameinfo.txt"
    if not gameinfo_path.exists():
        raise RuntimeError(f"Could not find gameinfo.txt at {gameinfo_path}")

    backup_path = gameinfo_path.with_suffix(".txt.bak")
    if not backup_path.exists():
        shutil.copy2(gameinfo_path, backup_path)
        log(f"Backed up original gameinfo.txt to {backup_path.name}")

    text = gameinfo_path.read_text(encoding="utf-8", errors="ignore")
    line_marker = f"{folder_name}"

    if remove:
        if line_marker not in text:
            log("gameinfo.txt entry already absent, nothing to remove.")
            return
        new_lines = [ln for ln in text.splitlines() if line_marker not in ln]
        gameinfo_path.write_text("\n".join(new_lines), encoding="utf-8")
        log(f"Removed SearchPaths entry for {folder_name}")
        return

    if line_marker in text:
        log("gameinfo.txt already contains an entry for this addon, skipping edit.")
        return

    idx = text.find("SearchPaths")
    if idx == -1:
        raise RuntimeError("Could not find a SearchPaths block in gameinfo.txt")
    brace_open = text.find("{", idx)
    if brace_open == -1:
        raise RuntimeError("Malformed gameinfo.txt: no '{' after SearchPaths")

    depth = 0
    i = brace_open
    brace_close = -1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                brace_close = i
                break
        i += 1
    if brace_close == -1:
        raise RuntimeError("Malformed gameinfo.txt: unmatched braces in SearchPaths")

    # Insert as the FIRST entry in the block, right after the opening "{",
    # rather than at the end just before "}".
    insert_pos = brace_open + 1
    if insert_pos < len(text) and text[insert_pos] == "\r":
        insert_pos += 1
    if insert_pos < len(text) and text[insert_pos] == "\n":
        insert_pos += 1

    new_line = f'\t\tGame\t\t\t"{folder_name}"\n'
    updated = text[:insert_pos] + new_line + text[insert_pos:]
    gameinfo_path.write_text(updated, encoding="utf-8")
    log(f"Added SearchPaths entry: Game {folder_name}")


def uninstall_addon(item_id: str, game_path: Path, folder_name: str, log):
    update_gameinfo(game_path, folder_name, log, remove=True)
    dest_dir = game_path / folder_name
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
        log(f"Deleted {dest_dir}")


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")


class AddonRow(ctk.CTkFrame):
    def __init__(self, master, item_id, folder_name, enabled, on_remove, on_toggle):
        super().__init__(master, fg_color=BG_CARD, corner_radius=10)
        self.item_id = item_id

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=12, pady=8)
        name_color = None if enabled else TEXT_DIM
        ctk.CTkLabel(
            info, text=folder_name,
            font=ctk.CTkFont(size=13, weight="bold"), anchor="w",
            text_color=name_color,
        ).pack(anchor="w")
        status = "Enabled" if enabled else "Disabled"
        ctk.CTkLabel(
            info, text=f"Workshop ID {item_id}  ·  {status}", font=ctk.CTkFont(size=11),
            text_color=TEXT_DIM, anchor="w"
        ).pack(anchor="w")

        ctk.CTkButton(
            self, text="Remove", width=80, height=28,
            fg_color="transparent", border_width=1, border_color=DANGER,
            hover_color=DANGER_HOVER, text_color=DANGER,
            command=lambda: on_remove(item_id),
        ).pack(side="right", padx=(0, 12), pady=8)

        ctk.CTkButton(
            self, text="Workshop page", width=110, height=28,
            fg_color="transparent", border_width=1, border_color=TEXT_DIM,
            hover_color=NEUTRAL_BTN_HOVER, text_color=TEXT_DIM,
            command=lambda: webbrowser.open(
                f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"
            ),
        ).pack(side="right", padx=(0, 8), pady=8)

        self.toggle_var = ctk.BooleanVar(value=enabled)
        ctk.CTkSwitch(
            self, text="", width=40, variable=self.toggle_var,
            progress_color=ACCENT, onvalue=True, offvalue=False,
            command=lambda: on_toggle(item_id, self.toggle_var.get()),
        ).pack(side="right", padx=(12, 4), pady=8)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("L4D2 Workshop Addon Manager")
        self.geometry("760x680")
        self.minsize(620, 560)
        self.configure(fg_color=BG_APP)

        self.cfg = load_config()
        if not self.cfg.get("game_path"):
            self.cfg["game_path"] = guess_game_path()

        self._busy = False
        self._build_widgets()
        self._refresh_installed_list()

    # ---------------------------------------------------------------- UI --

    def _build_widgets(self):
        # Toast notification (floats over the UI; used instead of Windows
        # message boxes for routine success messages so it doesn't play the
        # OS alert sound).
        self.toast_label = ctk.CTkLabel(
            self, text="", font=ctk.CTkFont(size=12, weight="bold"),
            corner_radius=8, fg_color="#22c55e", text_color="white",
            padx=14, pady=8,
        )

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(22, 10))
        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x")
        ctk.CTkLabel(
            title_row, text="L4D2 Addon Manager",
            font=ctk.CTkFont(size=24, weight="bold")
        ).pack(side="left")
        ctk.CTkLabel(
            title_row, text="made by tanner",
            font=ctk.CTkFont(size=11), text_color=TEXT_DIM
        ).pack(side="right", pady=(10, 0))
        ctk.CTkLabel(
            header, text="Paste a workshop link. It handles the download, the vpk rename, and gameinfo.txt.",
            font=ctk.CTkFont(size=12), text_color=TEXT_DIM
        ).pack(anchor="w", pady=(2, 0))

        # Game path card
        path_card = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=14)
        path_card.pack(fill="x", padx=24, pady=8)
        ctk.CTkLabel(
            path_card, text="GAME FOLDER", font=ctk.CTkFont(size=11, weight="bold"),
            text_color=TEXT_DIM
        ).pack(anchor="w", padx=16, pady=(12, 0))
        row = ctk.CTkFrame(path_card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(4, 10))
        self.game_path_var = ctk.StringVar(value=self.cfg.get("game_path", ""))
        ctk.CTkEntry(
            row, textvariable=self.game_path_var, height=36,
            placeholder_text=r"C:\...\Steam\steamapps\common\Left 4 Dead 2"
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(
            row, text="Browse", width=90, height=36,
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
            command=self._browse_game_path
        ).pack(side="left")

        launch_row = ctk.CTkFrame(path_card, fg_color="transparent")
        launch_row.pack(fill="x", padx=16, pady=(0, 14))
        self.launch_opts_var = ctk.StringVar(value=self.cfg.get("launch_options", ""))
        ctk.CTkEntry(
            launch_row, textvariable=self.launch_opts_var, height=32,
            placeholder_text="Launch options (e.g. -novid -console -windowed)"
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.launch_btn = ctk.CTkButton(
            launch_row, text="▶  Launch L4D2", width=140, height=32,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._launch_game,
        )
        self.launch_btn.pack(side="left")

        # Install card
        install_card = ctk.CTkFrame(self, fg_color=BG_CARD, corner_radius=14)
        install_card.pack(fill="x", padx=24, pady=8)
        ctk.CTkLabel(
            install_card, text="WORKSHOP URL OR ID", font=ctk.CTkFont(size=11, weight="bold"),
            text_color=TEXT_DIM
        ).pack(anchor="w", padx=16, pady=(12, 0))
        row2 = ctk.CTkFrame(install_card, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(4, 8))
        self.url_var = ctk.StringVar()
        self.url_entry = ctk.CTkEntry(
            row2, textvariable=self.url_var, height=40,
            placeholder_text="https://steamcommunity.com/sharedfiles/filedetails/?id=..."
        )
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(
            row2, text="Paste", width=90, height=40,
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER,
            command=self._paste_clipboard,
        ).pack(side="left", padx=(0, 8))
        self.install_btn = ctk.CTkButton(
            row2, text="⬇  Install", width=120, height=40,
            fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._on_install
        )
        self.install_btn.pack(side="left")

        self.progress = ctk.CTkProgressBar(install_card, height=6, mode="indeterminate")
        self.progress.pack(fill="x", padx=16, pady=(0, 14))
        self.progress.set(0)

        self.status_var = ctk.StringVar(value="Ready.")
        ctk.CTkLabel(
            install_card, textvariable=self.status_var, font=ctk.CTkFont(size=11),
            text_color=TEXT_DIM
        ).pack(anchor="w", padx=16, pady=(0, 12))

        # --- Installed addons + Log, in a vertically resizable split ---
        self.paned = tk.PanedWindow(
            self, orient="vertical", sashwidth=6, sashrelief="flat",
            bg=BG_APP, bd=0, sashpad=0, opaqueresize=True
        )
        self.paned.pack(fill="both", expand=True, padx=24, pady=(10, 20))

        # Top pane: installed addons
        top_pane = ctk.CTkFrame(self.paned, fg_color="transparent")
        installed_header = ctk.CTkFrame(top_pane, fg_color="transparent")
        installed_header.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            installed_header, text="INSTALLED ADDONS", font=ctk.CTkFont(size=11, weight="bold"),
            text_color=TEXT_DIM
        ).pack(side="left")
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *args: self._refresh_installed_list())
        ctk.CTkEntry(
            installed_header, textvariable=self.search_var, height=24, width=200,
            placeholder_text="Search installed addons..."
        ).pack(side="right")
        ctk.CTkButton(
            installed_header, text="Disable All", width=90, height=24,
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER, font=ctk.CTkFont(size=10),
            command=lambda: self._set_all_enabled(False)
        ).pack(side="right", padx=(0, 8))
        ctk.CTkButton(
            installed_header, text="Enable All", width=90, height=24,
            fg_color=NEUTRAL_BTN, hover_color=NEUTRAL_BTN_HOVER, font=ctk.CTkFont(size=10),
            command=lambda: self._set_all_enabled(True)
        ).pack(side="right", padx=(0, 8))

        self.installed_frame = ctk.CTkScrollableFrame(top_pane, fg_color="transparent")
        self.installed_frame.pack(fill="both", expand=True)
        self.paned.add(top_pane, minsize=90, stretch="always")

        # Bottom pane: log
        bottom_pane = ctk.CTkFrame(self.paned, fg_color="transparent")
        log_header = ctk.CTkFrame(bottom_pane, fg_color="transparent")
        log_header.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            log_header, text="LOG", font=ctk.CTkFont(size=11, weight="bold"),
            text_color=TEXT_DIM
        ).pack(side="left")
        ctk.CTkButton(
            log_header, text="Clear", width=60, height=22, fg_color=NEUTRAL_BTN,
            hover_color=NEUTRAL_BTN_HOVER, font=ctk.CTkFont(size=10),
            command=self._clear_log
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            log_header, text="Copy log", width=80, height=22, fg_color=NEUTRAL_BTN,
            hover_color=NEUTRAL_BTN_HOVER, font=ctk.CTkFont(size=10),
            command=self._copy_log
        ).pack(side="right")

        # Left as state="normal" on purpose: customtkinter's textbox blocks
        # mouse clicks/selection entirely when disabled, which makes it
        # impossible to select+copy error text. Instead we block *typing*
        # via a key handler further down, but allow click/select/scroll/copy.
        self.log_box = ctk.CTkTextbox(
            bottom_pane, fg_color="#0e0e10", text_color="#c9c9cc",
            font=ctk.CTkFont(family="Consolas", size=11), corner_radius=12
        )
        self.log_box.pack(fill="both", expand=True)
        self.log_box.bind("<Key>", self._block_log_typing)
        self.paned.add(bottom_pane, minsize=90, stretch="always")

    # ------------------------------------------------------------ helpers --

    def _browse_game_path(self):
        path = filedialog.askdirectory(title="Select your Left 4 Dead 2 folder")
        if path:
            self.game_path_var.set(path)
            self.cfg["game_path"] = path
            save_config(self.cfg)

    def _paste_clipboard(self):
        try:
            text = self.clipboard_get().strip()
        except Exception:
            text = ""
        if text:
            self.url_var.set(text)
            self.status_var.set("Pasted from clipboard.")

    def _launch_game(self):
        game_path = self._validate_game_path()
        if not game_path:
            return
        exe_path = game_path / L4D2_EXE_NAME
        if not exe_path.exists():
            messagebox.showerror("Error", f"Couldn't find {L4D2_EXE_NAME} in:\n{game_path}")
            return
        opts = self.launch_opts_var.get().strip()
        self.cfg["launch_options"] = opts
        save_config(self.cfg)
        try:
            args = [str(exe_path)] + (shlex.split(opts) if opts else [])
            subprocess.Popen(args, cwd=str(game_path))
            self.log(f"Launched {L4D2_EXE_NAME}" + (f" with options: {opts}" if opts else "") + ".")
            self.status_var.set("Game launched.")
        except Exception as e:
            messagebox.showerror("Launch failed", str(e))

    def log(self, msg):
        def _write():
            self.log_box.insert("end", str(msg).rstrip() + "\n")
            self.log_box.see("end")
        self.after(0, _write)

    def _show_toast(self, message, success=True):
        """Show a small in-window banner instead of a Windows message box —
        avoids the OS 'ding'/alert sound that messagebox popups trigger."""
        self.toast_label.configure(
            text=message, fg_color="#22c55e" if success else DANGER
        )
        self.toast_label.place(relx=0.5, rely=0.04, anchor="n")
        self.after(3500, self.toast_label.place_forget)

    def _clear_log(self):
        self.log_box.delete("1.0", "end")

    def _copy_log(self):
        text = self.log_box.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set("Log copied to clipboard.")

    def _block_log_typing(self, event):
        # Allow copy (Ctrl+C), select-all (Ctrl+A), and navigation/scroll
        # keys, but block anything that would actually edit the text.
        allowed_keysyms = {
            "Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End",
            "Shift_L", "Shift_R", "Control_L", "Control_R",
        }
        if event.state & 0x4 and event.keysym.lower() in ("c", "a"):  # Ctrl held
            return None
        if event.keysym in allowed_keysyms:
            return None
        return "break"

    def _set_busy(self, busy: bool, status: str = ""):
        def _apply():
            self._busy = busy
            self.install_btn.configure(state="disabled" if busy else "normal")
            if busy:
                self.progress.start()
            else:
                self.progress.stop()
                self.progress.set(0)
            if status:
                self.status_var.set(status)
        self.after(0, _apply)

    def _refresh_installed_list(self):
        for child in self.installed_frame.winfo_children():
            child.destroy()
        installed = self.cfg.get("installed", {})
        query = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        if query:
            installed = {
                iid: entry for iid, entry in installed.items()
                if query in entry["folder_name"].lower() or query in iid.lower()
            }
        if not installed:
            msg = "No addons match your search." if query else "No addons installed yet."
            ctk.CTkLabel(
                self.installed_frame, text=msg,
                text_color=TEXT_DIM, font=ctk.CTkFont(size=12)
            ).pack(anchor="w", pady=8)
            self._apply_fast_scroll(self.installed_frame)
            return
        for item_id, entry in installed.items():
            row = AddonRow(
                self.installed_frame, item_id, entry["folder_name"], entry.get("enabled", True),
                self._remove_addon, self._toggle_addon,
            )
            row.pack(fill="x", pady=4)
        self._apply_fast_scroll(self.installed_frame)

    def _apply_fast_scroll(self, widget):
        """Make mouse-wheel scrolling over the installed-addons list move a
        clearly bigger distance per notch. customtkinter's own scroll
        binding is fixed at a couple of tiny units, so instead of fighting
        its delta math we set a concrete pixel size per 'unit' on the
        canvas and then scroll a fixed number of those units per notch —
        that's deterministic and easy to tune (see SCROLL_UNIT_PX /
        SCROLL_UNITS_PER_NOTCH below)."""
        canvas = getattr(self.installed_frame, "_parent_canvas", None)
        if canvas is None:
            return

        SCROLL_UNIT_PX = 22
        SCROLL_UNITS_PER_NOTCH = 4
        try:
            canvas.configure(yscrollincrement=SCROLL_UNIT_PX)
        except Exception:
            pass

        def _on_wheel(event):
            delta = getattr(event, "delta", 0)
            if delta:
                direction = -1 if delta > 0 else 1
            else:
                direction = -1 if getattr(event, "num", 5) == 4 else 1
            canvas.yview_scroll(direction * SCROLL_UNITS_PER_NOTCH, "units")
            return "break"

        def _bind_all(w):
            w.bind("<MouseWheel>", _on_wheel)
            w.bind("<Button-4>", _on_wheel)
            w.bind("<Button-5>", _on_wheel)
            for c in w.winfo_children():
                _bind_all(c)

        canvas.bind("<MouseWheel>", _on_wheel)
        canvas.bind("<Button-4>", _on_wheel)
        canvas.bind("<Button-5>", _on_wheel)
        _bind_all(widget)

    def _validate_game_path(self):
        game_path = Path(self.game_path_var.get().strip())
        if not game_path.exists():
            messagebox.showerror("Error", f"Game folder not found:\n{game_path}")
            return None
        if not (game_path / "left4dead2").exists():
            messagebox.showerror(
                "Error",
                f"'{game_path}' doesn't look like a Left 4 Dead 2 folder "
                "(no left4dead2 subfolder found).",
            )
            return None
        self.cfg["game_path"] = str(game_path)
        save_config(self.cfg)
        return game_path

    # -------------------------------------------------------------- install --

    def _on_install(self):
        if self._busy:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Missing input", "Paste a workshop URL or ID first.")
            return
        game_path = self._validate_game_path()
        if not game_path:
            return
        threading.Thread(target=self._install_worker, args=(url, game_path), daemon=True).start()

    def _install_worker(self, url, game_path):
        self._set_busy(True, "Working...")
        try:
            item_id = extract_workshop_id(url)
            self.log(f"Workshop item ID: {item_id}")
            self._set_busy(True, "Looking up addon title...")
            title = fetch_workshop_title(item_id, self.log)
            if title:
                self.log(f"Addon title: {title}")
                folder_name = f"{sanitize_folder_name(title)} ({item_id})"
            else:
                folder_name = f"workshop_{item_id}"
            self._set_busy(True, "Setting up SteamCMD...")
            ensure_steamcmd(self.log)
            self._set_busy(True, "Downloading from Workshop...")
            content_dir = download_workshop_item(item_id, self.log)
            self._set_busy(True, "Installing files...")
            install_addon_files(content_dir, game_path, folder_name, self.log)
            self._set_busy(True, "Updating gameinfo.txt...")
            update_gameinfo(game_path, folder_name, self.log)
            self.cfg.setdefault("installed", {})[item_id] = {"folder_name": folder_name, "enabled": True}
            save_config(self.cfg)
            self.after(0, self._refresh_installed_list)
            self.log(f"Done! '{folder_name}' is installed and enabled. Restart L4D2 if it's running.")
            self._set_busy(False, "Installed successfully.")
            self.after(0, lambda: self._show_toast(f"{folder_name} installed!", success=True))
            self.after(0, lambda: self.url_var.set(""))
        except Exception as e:
            err_msg = str(e)
            self.log(f"ERROR: {err_msg}")
            self._set_busy(False, "Failed — see log.")
            self.after(0, lambda: messagebox.showerror("Install failed", err_msg))

    # -------------------------------------------------------------- remove --

    def _remove_addon(self, item_id):
        entry = self.cfg["installed"].get(item_id)
        if not entry:
            return
        folder_name = entry["folder_name"]
        game_path = self._validate_game_path()
        if not game_path:
            return
        if not messagebox.askyesno("Confirm", f"Remove addon {item_id}? This deletes its files."):
            return
        threading.Thread(
            target=self._remove_worker, args=(item_id, folder_name, game_path), daemon=True
        ).start()

    def _remove_worker(self, item_id, folder_name, game_path):
        self._set_busy(True, f"Removing {item_id}...")
        try:
            uninstall_addon(item_id, game_path, folder_name, self.log)
            del self.cfg["installed"][item_id]
            save_config(self.cfg)
            self.after(0, self._refresh_installed_list)
            self.log(f"Removed addon {item_id}.")
            self._set_busy(False, "Removed.")
        except Exception as e:
            err_msg = str(e)
            self.log(f"ERROR removing addon: {err_msg}")
            self._set_busy(False, "Failed — see log.")
            self.after(0, lambda: messagebox.showerror("Remove failed", err_msg))

    # -------------------------------------------------------------- toggle --

    def _toggle_addon(self, item_id, want_enabled):
        entry = self.cfg["installed"].get(item_id)
        if not entry:
            return
        folder_name = entry["folder_name"]
        game_path = self._validate_game_path()
        if not game_path:
            self.after(0, self._refresh_installed_list)  # revert switch visual
            return
        threading.Thread(
            target=self._toggle_worker, args=(item_id, folder_name, game_path, want_enabled),
            daemon=True,
        ).start()

    def _toggle_worker(self, item_id, folder_name, game_path, want_enabled):
        verb = "Enabling" if want_enabled else "Disabling"
        self._set_busy(True, f"{verb} {folder_name}...")
        try:
            # remove=False adds the SearchPaths line (skips if already there);
            # remove=True strips it out, but files on disk are left untouched
            # either way, so this never deletes anything.
            update_gameinfo(game_path, folder_name, self.log, remove=not want_enabled)
            self.cfg["installed"][item_id]["enabled"] = want_enabled
            save_config(self.cfg)
            self.after(0, self._refresh_installed_list)
            state = "enabled" if want_enabled else "disabled"
            self.log(f"{folder_name} is now {state}.")
            self._set_busy(False, f"{folder_name} {state}.")
        except Exception as e:
            err_msg = str(e)
            self.log(f"ERROR toggling addon: {err_msg}")
            self._set_busy(False, "Failed — see log.")
            self.after(0, lambda: messagebox.showerror("Toggle failed", err_msg))
            self.after(0, self._refresh_installed_list)

    def _set_all_enabled(self, want_enabled):
        if not self.cfg.get("installed"):
            return
        game_path = self._validate_game_path()
        if not game_path:
            return
        threading.Thread(
            target=self._set_all_worker, args=(game_path, want_enabled), daemon=True
        ).start()

    def _set_all_worker(self, game_path, want_enabled):
        verb = "Enabling" if want_enabled else "Disabling"
        self._set_busy(True, f"{verb} all addons...")
        try:
            for item_id, entry in list(self.cfg.get("installed", {}).items()):
                update_gameinfo(game_path, entry["folder_name"], self.log, remove=not want_enabled)
                entry["enabled"] = want_enabled
            save_config(self.cfg)
            self.after(0, self._refresh_installed_list)
            state = "enabled" if want_enabled else "disabled"
            self.log(f"All addons {state}.")
            self._set_busy(False, f"All addons {state}.")
        except Exception as e:
            err_msg = str(e)
            self.log(f"ERROR: {err_msg}")
            self._set_busy(False, "Failed — see log.")
            self.after(0, lambda: messagebox.showerror("Failed", err_msg))
            self.after(0, self._refresh_installed_list)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("This tool automates SteamCMD + gameinfo.txt editing and is built for Windows,")
        print("which is where L4D2 and its addons folder normally live. It may not work as-is")
        print("on other platforms.")
    app = App()
    app.mainloop()
