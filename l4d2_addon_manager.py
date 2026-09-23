"""
L4D2 Workshop Addon Manager (web UI edition)
---------------------------------------------
Same functionality as the original customtkinter version, but the UI is
now HTML/CSS/JS rendered inside the OS's native WebView2 control via
`pywebview`. This gets real GPU-accelerated rendering (smooth resizing,
CSS transitions, cheap scrolling of long lists) instead of Tkinter's
CPU-only widget layout.

All of the actual game/file/network logic below (SteamCMD, gameinfo.txt
editing, theme colors, modlists, etc.) is unchanged from the Tkinter
version — none of it ever depended on Tkinter itself. Only the UI layer
was rewritten.

Requires: Windows, Python 3.8+, and:

    pip install pywebview

WebView2 itself (the actual rendering engine) is pre-installed on Windows
11 and most up-to-date Windows 10 machines. If it's missing, Windows will
prompt to install the small WebView2 Runtime automatically.

Run with:  python l4d2_addon_manager.py
"""

import hashlib
import http.server
import json
import queue
import secrets
import urllib.parse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import threading
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from datetime import date
from typing import Optional

try:
    import winreg  # Windows-only; used to locate steam.exe for launching
except ImportError:
    winreg = None

try:
    import webview
except ImportError:
    print("This app needs the 'pywebview' package.")
    print("Install it with:  pip install pywebview")
    sys.exit(1)

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
STEAMCMD_DIR = APP_DIR / "steamcmd"
STEAMCMD_EXE = STEAMCMD_DIR / "steamcmd.exe"
STEAMCMD_ZIP_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
L4D2_APPID = "550"
L4D2_EXE_NAME = "left4dead2.exe"
GITHUB_URL = "https://github.com/tnrjns/l4d2-addon-manager"
GITHUB_API_LATEST_RELEASE = "https://api.github.com/repos/tnrjns/l4d2-addon-manager/releases/latest"
APP_VERSION = "2.1.0"

DEFAULT_L4D2_PATHS = [
    r"C:\Program Files (x86)\Steam\steamapps\common\Left 4 Dead 2",
    r"C:\Steam\steamapps\common\Left 4 Dead 2",
]


# --------------------------------------------------------------------------
# Color helpers + themes (unchanged math from the Tkinter version — these
# now get sent to the frontend as CSS custom properties instead of being
# fed into ctk widget constructors)
# --------------------------------------------------------------------------

def blend_hex(c1: str, c2: str, t: float) -> str:
    c1, c2 = c1.lstrip("#"), c2.lstrip("#")
    r1, g1, b1 = int(c1[0:2], 16), int(c1[2:4], 16), int(c1[4:6], 16)
    r2, g2, b2 = int(c2[0:2], 16), int(c2[2:4], 16), int(c2[4:6], 16)
    r = int(r1 + (r2 - r1) * t)
    g = int(g1 + (g2 - g1) * t)
    b = int(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def darken(c: str, amount: float) -> str:
    return blend_hex(c, "#000000", amount)


def lighten(c: str, amount: float) -> str:
    return blend_hex(c, "#ffffff", amount)


def contrast_text_color(bg_hex: str) -> str:
    h = bg_hex.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#111111" if luminance > 0.6 else "#ffffff"


THEMES = {
    "Default": {
        "bg_app": "#1E1E22", "bg_card": "#28282E", "accent": "#E4E4E7",
        "text_dim": "#9A9AA2", "neutral_btn": "#38383F", "danger": "#F04747",
    },
    "Nord": {
        "bg_app": "#2E3440", "bg_card": "#3B4252", "accent": "#88C0D0",
        "text_dim": "#9AA7B8", "neutral_btn": "#434C5E", "danger": "#BF616A",
    },
    "Dracula": {
        "bg_app": "#282A36", "bg_card": "#343746", "accent": "#BD93F9",
        "text_dim": "#8890B0", "neutral_btn": "#3B3E4D", "danger": "#FF5555",
    },
    "Gruvbox": {
        "bg_app": "#282828", "bg_card": "#3C3836", "accent": "#FE8019",
        "text_dim": "#A89984", "neutral_btn": "#504945", "danger": "#FB4934",
    },
    "Everforest": {
        "bg_app": "#2D353B", "bg_card": "#374247", "accent": "#A7C080",
        "text_dim": "#9DA9A0", "neutral_btn": "#414B50", "danger": "#E67E80",
    },
    "Catppuccin": {
        "bg_app": "#1E1E2E", "bg_card": "#313244", "accent": "#CBA6F7",
        "text_dim": "#A6ADC8", "neutral_btn": "#45475A", "danger": "#F38BA8",
    },
    "Tokyonight": {
        "bg_app": "#1A1B26", "bg_card": "#24283B", "accent": "#7AA2F7",
        "text_dim": "#787C99", "neutral_btn": "#343A52", "danger": "#F7768E",
    },
}
THEME_NAMES = list(THEMES.keys())
DEFAULT_THEME = "Default"


def compute_palette(name: str) -> dict:
    t = THEMES.get(name, THEMES[DEFAULT_THEME])
    accent = t["accent"]
    neutral = t["neutral_btn"]
    danger = t["danger"]
    return {
        "bg_app": t["bg_app"],
        "bg_card": t["bg_card"],
        "accent": accent,
        "accent_hover": darken(accent, 0.18),
        "accent_text": contrast_text_color(accent),
        "text_dim": t["text_dim"],
        "neutral_btn": neutral,
        "neutral_btn_hover": lighten(neutral, 0.15),
        "danger": danger,
        "danger_hover": darken(danger, 0.82),
    }


ALL_PALETTES = {name: compute_palette(name) for name in THEME_NAMES}


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
    cfg.setdefault("modlists", {})
    cfg.setdefault("theme", DEFAULT_THEME)
    cfg.setdefault("confirm_remove", True)
    cfg.setdefault("log_hidden", False)
    for item_id, value in list(cfg["installed"].items()):
        if isinstance(value, str):
            cfg["installed"][item_id] = {"folder_name": value, "enabled": True}
    if cfg["theme"] not in THEMES:
        cfg["theme"] = DEFAULT_THEME
    return cfg


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def guess_game_path():
    for p in DEFAULT_L4D2_PATHS:
        if Path(p).exists():
            return p
    return ""


def find_steam_exe() -> Optional[Path]:
    if winreg is None:
        return None
    registry_locations = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamExe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ]
    for hive, key_path, value_name in registry_locations:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        value = str(value)
        candidate = Path(value) if value.lower().endswith("steam.exe") else Path(value) / "steam.exe"
        if candidate.exists():
            return candidate
    return None


def _version_tuple(v: str):
    """'2.10.1' -> (2, 10, 1). Numeric so 2.10.0 correctly sorts after 2.9.0
    (plain string comparison would get that backwards)."""
    parts = []
    for p in v.strip().split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def fetch_l4d2_player_count() -> Optional[int]:
    url = f"https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={L4D2_APPID}&format=json"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        response = payload.get("response", {})
        if response.get("result") == 1:
            return int(response.get("player_count"))
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Core addon logic (unchanged)
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
TRAILING_ID_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")


def sanitize_folder_name(name: str) -> str:
    name = INVALID_FOLDER_CHARS.sub("", name).strip()
    name = re.sub(r"\s+", " ", name)
    name = name.rstrip(". ")
    if not name:
        name = "addon"
    return name[:60]


def unique_folder_name(base_title: str, cfg: dict, game_path: Path, exclude_item_id=None) -> str:
    base = sanitize_folder_name(base_title)
    existing = {
        entry["folder_name"] for iid, entry in cfg.get("installed", {}).items()
        if iid != exclude_item_id
    }
    candidate = base
    n = 2
    while candidate in existing or (game_path / candidate).exists():
        candidate = f"{base} ({n})"
        n += 1
    return candidate


def expand_collection(item_id: str, log, _seen=None) -> Optional[list]:
    """If item_id is a Workshop *collection*, return the IDs of every addon
    inside it (recursing into nested collections), in the collection's own
    order. Returns None if it's an ordinary single addon, so callers can
    fall through to the normal install path.

    Steam marks each child with a filetype: 0 = regular item, 2 = another
    collection. _seen guards against a collection that (directly or
    indirectly) contains itself."""
    if _seen is None:
        _seen = set()
    if item_id in _seen:
        return []
    _seen.add(item_id)

    url = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
    data = urllib.parse.urlencode(
        {"collectioncount": 1, "publishedfileids[0]": item_id}
    ).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"Couldn't check whether {item_id} is a collection ({e}); treating it as a single addon.")
        return None

    details = payload.get("response", {}).get("collectiondetails", [])
    children = details[0].get("children") if details else None
    if not children:
        return None  # not a collection

    ordered = sorted(children, key=lambda c: c.get("sortorder", 0))
    ids = []
    for child in ordered:
        cid = str(child.get("publishedfileid", "")).strip()
        if not cid:
            continue
        if child.get("filetype") == 2:
            nested = expand_collection(cid, log, _seen)
            ids.extend(nested or [])
        else:
            ids.append(cid)

    # De-duplicate while keeping order (an addon can appear in two sub-collections).
    seen_ids, unique = set(), []
    for cid in ids:
        if cid not in seen_ids:
            seen_ids.add(cid)
            unique.append(cid)
    return unique


def fetch_workshop_details(item_ids, log) -> dict:
    """Looks up title + preview image for many Workshop items at once.
    Returns {item_id: {"title": str, "preview_url": str}}, only for items
    Steam actually returned (private/removed ones are simply absent).

    Steam accepts a batch per request, so a whole library or collection
    costs a handful of requests instead of one per addon. Chunked to keep
    each request a sane size."""
    ids = [str(i) for i in item_ids if str(i).isdigit()]
    out = {}
    url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    CHUNK = 50
    for start in range(0, len(ids), CHUNK):
        chunk = ids[start:start + CHUNK]
        params = {"itemcount": len(chunk)}
        for n, iid in enumerate(chunk):
            params[f"publishedfileids[{n}]"] = iid
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode("utf-8"))
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            log(f"Couldn't reach Steam for addon details ({e}).")
            continue
        for d in payload.get("response", {}).get("publishedfiledetails", []):
            if d.get("result") != 1:
                continue
            fid = str(d.get("publishedfileid", ""))
            out[fid] = {
                "title": (d.get("title") or "").strip(),
                "preview_url": (d.get("preview_url") or "").strip(),
                "file_size": int(d.get("file_size") or 0),   # bytes; used for the download progress bar
            }
    return out


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


def _tree_size(folder: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass  # file vanished mid-walk (SteamCMD moved it) - fine
    except OSError:
        pass
    return total


def download_workshop_item(item_id: str, log, progress=None):
    """Downloads one Workshop item with SteamCMD.

    SteamCMD prints nothing while a Workshop item downloads, only "Success"
    at the very end, so progress is measured by watching its working
    folders grow on disk. `progress(done_bytes)` is called a few times a
    second while that happens."""
    log(f"Downloading workshop item {item_id} via SteamCMD (anonymous login)...")
    cmd = [
        str(STEAMCMD_EXE),
        "+login", "anonymous",
        "+workshop_download_item", L4D2_APPID, item_id,
        "+quit",
    ]
    ws = STEAMCMD_DIR / "steamapps" / "workshop"
    content_dir = ws / "content" / L4D2_APPID / item_id
    # SteamCMD downloads into a staging folder, then moves the result into
    # content/. Watch all of them and take the largest.
    staging = [ws / "downloads" / L4D2_APPID / item_id, ws / "temp" / L4D2_APPID / item_id]
    # A leftover copy from an earlier download would otherwise read as
    # "100% done" instantly, so content/ only counts once it has changed.
    content_before = _tree_size(content_dir) if content_dir.exists() else None

    proc = subprocess.Popen(
        cmd, cwd=str(STEAMCMD_DIR), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
    )
    # Must drain output continuously: if nobody reads it, SteamCMD blocks
    # once the pipe buffer fills and the download hangs forever.
    out_lines = []
    reader = threading.Thread(target=lambda: out_lines.extend(proc.stdout), daemon=True)
    reader.start()

    last = -1
    while proc.poll() is None:
        done = max([_tree_size(d) for d in staging] or [0])
        if content_before is not None:
            now = _tree_size(content_dir)
            if now != content_before:
                done = max(done, now)
        elif content_dir.exists():
            done = max(done, _tree_size(content_dir))
        if progress and done != last:
            progress(done)
            last = done
        time.sleep(0.4)
    reader.join(timeout=5)
    output = "".join(out_lines)

    log(output[-3000:])
    if proc.returncode != 0 or "Success" not in output:
        raise RuntimeError(
            "SteamCMD did not report success downloading that item. "
            "It may be private, removed, or require you to own the game / "
            "be logged into an account that can access it."
        )
    if not content_dir.exists():
        raise RuntimeError(f"Expected downloaded content at {content_dir} but it wasn't found.")
    if progress:
        progress(_tree_size(content_dir))
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
        only_file = all_files[0]
        dest = dest_dir / "pak01_dir.vpk"
        shutil.copy2(only_file, dest)
        log(
            f"Only file found is '{only_file.name}' (no .vpk extension) — this is a known "
            f"quirk with some older Workshop uploads where Steam strips the original "
            f"extension. Treating it as vpk content and saving it as {dest}"
        )
    else:
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


NON_ADDON_FOLDER_NAMES = {
    "left4dead2", "left4dead2_dlc1", "left4dead2_dlc2", "left4dead2_dlc3",
    "bin", "platform", "hl2", "steamcmd", "addons", "update",
}
ADDON_CONTENT_SUBFOLDERS = {"materials", "models", "sound", "scripts", "missions", "particles", "resource", "maps"}
_TRAILING_ID = re.compile(r"\((\d{5,12})\)\s*$")
_WORKSHOP_PREFIX_ID = re.compile(r"^workshop_(\d{5,12})$", re.I)


def looks_like_addon_folder(folder: Path) -> bool:
    if any(folder.rglob("*.vpk")):
        return True
    try:
        return any((folder / sub).is_dir() for sub in ADDON_CONTENT_SUBFOLDERS)
    except Exception:
        return False


def guess_item_id_from_folder_name(folder_name: str) -> str:
    """Recovers a real Workshop ID from the folder name when possible
    (older naming schemes embedded it); otherwise makes a stable synthetic
    ID so the addon can still be tracked, just without a working Workshop
    page link."""
    m = _TRAILING_ID.search(folder_name)
    if m:
        return m.group(1)
    m = _WORKSHOP_PREFIX_ID.match(folder_name)
    if m:
        return m.group(1)
    return "local_" + hashlib.sha1(folder_name.encode("utf-8")).hexdigest()[:10]


def folder_size_bytes(folder: Path) -> int:
    """Total size of everything inside an addon folder. Used for the
    'sort by size' option — walked lazily in a background thread since
    this touches the disk for every installed addon."""
    total = 0
    try:
        for f in folder.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def folder_installed_at(folder: Path) -> float:
    """Best-effort install timestamp. Addons installed by this app record
    their own timestamp in config; this is the fallback for anything that
    predates that (or came in via Rescan), using the folder's creation
    time where the OS provides it."""
    try:
        st = folder.stat()
        return getattr(st, "st_ctime", st.st_mtime)
    except OSError:
        return 0.0


def rescan_addons(cfg: dict, game_path: Path, log) -> int:
    """Scans the game folder + gameinfo.txt directly and adds back any
    addon-looking folder that isn't already tracked in cfg — recovers from
    a lost/missing config.json without touching any files."""
    gameinfo_path = game_path / "left4dead2" / "gameinfo.txt"
    gameinfo_text = ""
    if gameinfo_path.exists():
        gameinfo_text = gameinfo_path.read_text(encoding="utf-8", errors="ignore")

    already_tracked = {entry["folder_name"] for entry in cfg.get("installed", {}).values()}
    found = 0
    for child in sorted(game_path.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name in NON_ADDON_FOLDER_NAMES or name.startswith("."):
            continue
        if name in already_tracked:
            continue
        if not looks_like_addon_folder(child):
            continue
        item_id = guess_item_id_from_folder_name(name)
        enabled = name in gameinfo_text
        cfg.setdefault("installed", {})[item_id] = {"folder_name": name, "enabled": enabled}
        log(f"Found untracked addon folder '{name}' — added back ({'enabled' if enabled else 'disabled'}).")
        found += 1
    return found


# --------------------------------------------------------------------------
# Clipboard (no GUI toolkit needed — a hidden Tk root is the simplest
# stdlib-only way to touch the OS clipboard without adding a dependency)
# --------------------------------------------------------------------------

def _clipboard_get() -> str:
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        try:
            return r.clipboard_get()
        finally:
            r.destroy()
    except Exception:
        return ""


def _clipboard_set(text: str):
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
    except Exception:
        pass


# --------------------------------------------------------------------------
# JS-facing API. Every public method here is callable from the frontend as
# `api('method_name', args)`, which returns a Promise.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# UI transport
# --------------------------------------------------------------------------
# The page is served over a loopback HTTP server and talks to Python with
# plain fetch() calls. We deliberately do NOT use pywebview's js_api
# bridge: that bridge introspects the Python object it's given and, on the
# Windows/WinForms backend, walks into the native form's self-referential
# properties, producing an endless
# "window.native.ActiveForm...SyncRoot: maximum recursion depth exceeded"
# spam and a window that never appears. No js_api, no introspection, no bug.

EVENT_QUEUE: "queue.Queue[dict]" = queue.Queue()
WINDOW = None          # set in main(); kept module-level so pywebview never serializes it
API = None             # the single Api instance the HTTP handler dispatches to
AUTH_TOKEN = secrets.token_urlsafe(24)   # guards the loopback port from other local processes


class _UIRequestHandler(http.server.BaseHTTPRequestHandler):
    server_version = "L4D2AddonManager"

    def log_message(self, *args):
        pass  # don't spam the console with request logs

    def _send(self, code, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _send_json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _authed(self) -> bool:
        return self.headers.get("X-Auth-Token") == AUTH_TOKEN

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            html = INDEX_HTML.replace("__AUTH_TOKEN__", AUTH_TOKEN)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/events":
            if not self._authed():
                self._send_json({"error": "unauthorized"}, 403)
                return
            events = []
            # Block briefly so idle polling doesn't spin, then drain whatever
            # else is queued in one go.
            try:
                events.append(EVENT_QUEUE.get(timeout=0.5))
            except queue.Empty:
                pass
            while True:
                try:
                    events.append(EVENT_QUEUE.get_nowait())
                except queue.Empty:
                    break
            self._send_json(events)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/") or not self._authed():
            self._send_json({"error": "unauthorized"}, 403)
            return
        method_name = path[len("/api/"):]
        if method_name.startswith("_") or not hasattr(API, method_name):
            self._send_json({"error": "unknown method"}, 404)
            return
        method = getattr(API, method_name)
        if not callable(method):
            self._send_json({"error": "not callable"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"[]"
            args = json.loads(raw.decode("utf-8") or "[]")
            if not isinstance(args, list):
                args = [args]
            result = method(*args)
            self._send_json({"result": result})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)


def start_ui_server() -> int:
    """Serve the UI on a random free loopback port. Returns the port."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _UIRequestHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


class Api:
    def __init__(self):
        # NOTE: the leading underscore is load-bearing. pywebview exposes
        # this object's *public* attributes to JavaScript, and serializing
        # a Window means walking into window.native (the underlying .NET
        # form), whose properties are self-referential — that's what caused
        # the endless "AccessibilityObject...SyncRoot: maximum recursion
        # depth exceeded" spam. Underscore-prefixed attributes are skipped.
        self._cfg = load_config()
        if not self._cfg.get("game_path"):
            self._cfg["game_path"] = guess_game_path()

    # ---- plumbing -------------------------------------------------------
    # Messages to the UI go onto a queue that the page polls over HTTP,
    # rather than through window.evaluate_js / the js_api bridge. That
    # bridge is what recursed endlessly into the .NET form's properties
    # ("window.native.ActiveForm...SyncRoot: maximum recursion depth
    # exceeded"), so this side of the app no longer touches it at all.

    def log(self, msg):
        text = str(msg).rstrip()
        print(text)
        self._push({"type": "log", "text": text})

    def _push(self, event: dict):
        try:
            EVENT_QUEUE.put_nowait(event)
        except Exception:
            pass

    def _set_busy(self, busy: bool, status: str = ""):
        self._push({"type": "busy", "busy": bool(busy), "status": status})

    def _toast(self, message: str, success: bool = True):
        self._push({"type": "toast", "message": str(message), "success": bool(success)})

    def _push_state(self):
        self._push({"type": "state", "state": self._state()})

    def _state(self) -> dict:
        return {
            "version": APP_VERSION,
            "date": date.today().strftime("%B %d, %Y"),
            "game_path": self._cfg.get("game_path", ""),
            "launch_options": self._cfg.get("launch_options", ""),
            "theme": self._cfg.get("theme", DEFAULT_THEME),
            "confirm_remove": self._cfg.get("confirm_remove", True),
            "log_hidden": self._cfg.get("log_hidden", False),
            "installed": self._cfg.get("installed", {}),
            "modlists": self._cfg.get("modlists", {}),
            "themes": ALL_PALETTES,
            "theme_names": THEME_NAMES,
            "github_url": GITHUB_URL,
        }

    def _validate_game_path(self) -> Optional[Path]:
        game_path = Path(self._cfg.get("game_path", "").strip())
        if not game_path.exists():
            self._toast(f"Game folder not found:\n{game_path}", success=False)
            return None
        if not (game_path / "left4dead2").exists():
            self._toast(f"'{game_path}' doesn't look like a Left 4 Dead 2 folder.", success=False)
            return None
        return game_path

    # ---- bootstrap --------------------------------------------------------

    def get_bootstrap(self) -> dict:
        return self._state()

    def load_previews(self):
        """Fetches Workshop preview images for installed addons that don't
        have one recorded yet (e.g. installed before this feature existed).
        Called once at startup, NOT on every state change like the disk
        stats, so it doesn't hit Steam every time you click something."""
        def run():
            changed = self._fetch_missing_previews(force=False)
            if changed:
                self._push_state()
        threading.Thread(target=run, daemon=True).start()

    def _fetch_missing_previews(self, force=False) -> int:
        """Looks up images for every installed addon that doesn't have one,
        in one batched request. Normally an item that Steam didn't return a
        picture for (private, removed, or just a hiccup) isn't retried again
        this session — force=True (used by Rescan) clears that memory so
        it's worth trying again, since the user asked for it explicitly."""
        if not hasattr(self, "_preview_attempted"):
            self._preview_attempted = set()
        installed = self._cfg.get("installed", {})
        if force:
            self._preview_attempted -= set(installed)
        missing = [iid for iid, e in list(installed.items())
                   if iid.isdigit() and not e.get("preview_url") and iid not in self._preview_attempted]
        if not missing:
            return 0
        self._preview_attempted.update(missing)
        found = fetch_workshop_details(missing, self.log)
        changed = 0
        for iid, d in found.items():
            if iid in installed and d.get("preview_url"):
                installed[iid]["preview_url"] = d["preview_url"]
                changed += 1
        if changed:
            save_config(self._cfg)
        return changed


    def load_addon_stats(self):
        """Walks each installed addon's folder for its size and install
        date, then pushes the results to the frontend. Runs in a background
        thread because it hits the disk once per addon, which would
        otherwise stall the UI on a large library."""
        threading.Thread(target=self._addon_stats_worker, daemon=True).start()

    def _addon_stats_worker(self):
        game_path = Path(self._cfg.get("game_path", "").strip())
        stats = {}
        for item_id, entry in list(self._cfg.get("installed", {}).items()):
            folder = game_path / entry["folder_name"]
            stats[item_id] = {
                "size": folder_size_bytes(folder) if folder.exists() else 0,
                "installed_at": entry.get("installed_at") or folder_installed_at(folder),
                "missing": not folder.exists(),
            }
        self._push({"type": "stats", "stats": stats})

    # ---- game path / launch ---------------------------------------------

    def browse_game_path(self):
        if WINDOW is None:
            return None
        folder_dialog = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if folder_dialog is None:
            folder_dialog = webview.FOLDER_DIALOG  # older pywebview versions
        result = WINDOW.create_file_dialog(folder_dialog)
        if result:
            path = result[0]
            self._cfg["game_path"] = path
            save_config(self._cfg)
            return path
        return None

    def save_launch_options(self, opts: str):
        self._cfg["launch_options"] = opts.strip()
        save_config(self._cfg)

    def save_game_path(self, path: str):
        self._cfg["game_path"] = path.strip()
        save_config(self._cfg)

    def launch_game(self):
        game_path = self._validate_game_path()
        if not game_path:
            return
        opts = self._cfg.get("launch_options", "").strip()
        steam_exe = find_steam_exe()
        try:
            if steam_exe:
                args = [str(steam_exe), "-applaunch", L4D2_APPID] + (shlex.split(opts) if opts else [])
                subprocess.Popen(args)
                self.log(
                    "Launched L4D2 through Steam (steam.exe -applaunch)"
                    + (f" with options: {opts}" if opts else "")
                    + " — this keeps the session VAC-secure."
                )
            else:
                exe_path = game_path / L4D2_EXE_NAME
                if not exe_path.exists():
                    self._toast(f"Couldn't find {L4D2_EXE_NAME} in:\n{game_path}", success=False)
                    return
                args = [str(exe_path)] + (shlex.split(opts) if opts else [])
                subprocess.Popen(args, cwd=str(game_path))
                self.log(
                    f"Couldn't locate steam.exe, so launched {L4D2_EXE_NAME} directly"
                    + (f" with options: {opts}" if opts else "")
                    + ". Note: launching the exe directly skips Steam's handshake and "
                    "may run in insecure (no-VAC) mode."
                )
        except Exception as e:
            self._toast(f"Launch failed: {e}", success=False)

    # ---- clipboard --------------------------------------------------------

    def get_clipboard(self) -> str:
        return _clipboard_get()

    def copy_log(self, text: str):
        _clipboard_set(text)

    # ---- install / remove / toggle ---------------------------------------

    def install_addon(self, url: str):
        game_path = self._validate_game_path()
        if not game_path:
            return
        threading.Thread(target=self._install_worker, args=(url, game_path), daemon=True).start()

    def _install_one(self, item_id, game_path, prefix="", details=None):
        """Downloads and installs a single addon. Raises on failure so the
        caller decides whether that's fatal (single install) or just
        something to note and move past (one item in a collection).
        `details` can be pre-fetched (collections look up every item in one
        batch); otherwise it's fetched here."""
        if details is None:
            self._set_busy(True, f"{prefix}Looking up addon details...")
            details = fetch_workshop_details([item_id], self.log).get(item_id, {})
        title = details.get("title")
        if title:
            self.log(f"Addon title: {title}")
            folder_name = unique_folder_name(title, self._cfg, game_path)
        else:
            folder_name = f"workshop_{item_id}"
        self._set_busy(True, f"{prefix}Downloading {folder_name}...")
        content_dir = download_workshop_item(
            item_id, self.log, progress=self._download_progress(folder_name, prefix, details.get("file_size", 0))
        )
        self._push({"type": "progress", "done": None})   # back to the indeterminate bar for the copy step
        self._set_busy(True, f"{prefix}Installing {folder_name}...")
        install_addon_files(content_dir, game_path, folder_name, self.log)
        update_gameinfo(game_path, folder_name, self.log)
        self._cfg.setdefault("installed", {})[item_id] = {
            "folder_name": folder_name, "enabled": True, "installed_at": time.time(),
            "preview_url": details.get("preview_url", ""),
        }
        save_config(self._cfg)
        return folder_name

    def _download_progress(self, name, prefix, total):
        """Returns a callback that turns raw byte counts from the download
        watcher into UI progress events, with a smoothed MB/s figure."""
        samples = []   # (timestamp, bytes) over the last few seconds

        def on_progress(done):
            now = time.time()
            samples.append((now, done))
            while samples and now - samples[0][0] > 3:
                samples.pop(0)
            speed = 0.0
            if len(samples) >= 2 and samples[-1][0] > samples[0][0]:
                speed = max(0.0, (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0]))
            self._push({"type": "progress", "name": name, "prefix": prefix,
                        "done": done, "total": total, "speed": speed})
        return on_progress

    def _install_worker(self, url, game_path):
        self._set_busy(True, "Working...")
        try:
            item_id = extract_workshop_id(url)
            self.log(f"Workshop ID: {item_id}")
            self._set_busy(True, "Checking whether this is a collection...")
            collection = expand_collection(item_id, self.log)
            self._set_busy(True, "Setting up SteamCMD...")
            ensure_steamcmd(self.log)

            if collection is None:
                folder_name = self._install_one(item_id, game_path)
                self.log(f"Done! '{folder_name}' is installed and enabled. Restart L4D2 if it's running.")
                self._set_busy(False, "Installed successfully.")
                self._toast(f"{folder_name} installed!", success=True)
                self._push_state()
                return

            self._install_collection(item_id, collection, game_path)
        except Exception as e:
            self.log(f"ERROR: {e}")
            self._set_busy(False, "Failed — see log.")
            self._toast(str(e), success=False)
            self._push_state()

    def _install_collection(self, collection_id, item_ids, game_path):
        if not item_ids:
            raise RuntimeError("That collection is empty (or all its items are private/removed).")

        # One batched lookup for the collection's own title and every item's
        # title/preview image, instead of a separate request per addon.
        self._set_busy(True, "Looking up collection details...")
        details = fetch_workshop_details([collection_id] + list(item_ids), self.log)
        coll_title = (details.get(collection_id, {}).get("title") or "").strip() or f"Collection {collection_id}"
        self.log(f"Collection: {coll_title}")

        already = set(self._cfg.get("installed", {}))
        todo = [i for i in item_ids if i not in already]
        skipped = len(item_ids) - len(todo)
        self.log(
            f"'{coll_title}' contains {len(item_ids)} addon(s); "
            f"{skipped} already installed, {len(todo)} to install."
        )

        installed, failed = [], []
        for n, iid in enumerate(todo, start=1):
            prefix = f"[{n}/{len(todo)}] "
            self.log(f"--- {prefix}Workshop item {iid} ---")
            try:
                installed.append(self._install_one(iid, game_path, prefix, details.get(iid, {})))
                # Refresh the list as we go, so a long collection visibly
                # fills in instead of looking frozen until the very end.
                self._push_state()
            except Exception as e:
                failed.append(iid)
                self.log(f"ERROR on item {iid}: {e} — skipping and continuing.")

        modlist_name, added = self._modlist_from_collection(coll_title, item_ids)

        summary = (f"Collection done: {len(installed)} installed, {skipped} already had, "
                   f"{len(failed)} failed. Added {added} to modlist '{modlist_name}'.")
        self.log(summary)
        if failed:
            self.log("Failed items (may be private, removed, or not a VPK addon): " + ", ".join(failed))
        self._set_busy(False, summary)
        self._toast(summary, success=not failed)
        self._push_state()

    def _modlist_from_collection(self, coll_title, item_ids):
        """Puts every successfully-installed addon from a collection into a
        modlist named after the collection, in the collection's own order.
        Re-installing the same collection later merges into that modlist
        (adding anything new) rather than creating a duplicate."""
        name = " ".join(coll_title.split())[:80] or "Collection"   # just tidy whitespace; any characters are fine in a modlist name
        installed = self._cfg.get("installed", {})
        members = self._cfg.setdefault("modlists", {}).setdefault(name, [])
        present = set(members)
        added = 0
        for iid in item_ids:
            if iid in installed and iid not in present:
                members.append(iid)
                present.add(iid)
                added += 1
        save_config(self._cfg)
        self.log(f"Modlist '{name}' now has {len(members)} addon(s) ({added} newly added).")
        return name, added

    def remove_addon(self, item_id: str):
        entry = self._cfg.get("installed", {}).get(item_id)
        if not entry:
            return
        game_path = self._validate_game_path()
        if not game_path:
            return
        threading.Thread(
            target=self._remove_worker, args=(item_id, entry["folder_name"], game_path), daemon=True
        ).start()

    def _remove_worker(self, item_id, folder_name, game_path):
        self._set_busy(True, f"Removing {folder_name}...")
        try:
            uninstall_addon(item_id, game_path, folder_name, self.log)
            del self._cfg["installed"][item_id]
            for members in self._cfg.get("modlists", {}).values():
                if item_id in members:
                    members.remove(item_id)
            save_config(self._cfg)
            self.log(f"Removed addon {item_id}.")
            self._set_busy(False, "Removed.")
            self._push_state()
        except Exception as e:
            self.log(f"ERROR removing addon: {e}")
            self._set_busy(False, "Failed — see log.")
            self._toast(str(e), success=False)

    def set_many_enabled(self, item_ids: list, enabled: bool):
        game_path = self._validate_game_path()
        if not game_path:
            return
        ids = [str(i) for i in (item_ids or []) if str(i) in self._cfg.get("installed", {})]
        if not ids:
            return
        threading.Thread(target=self._set_many_worker, args=(ids, game_path, enabled), daemon=True).start()

    def _set_many_worker(self, ids, game_path, enabled):
        verb = "Enabling" if enabled else "Disabling"
        changed = 0
        for n, iid in enumerate(ids, start=1):
            entry = self._cfg.get("installed", {}).get(iid)
            if not entry:
                continue
            name = entry["folder_name"]
            self._set_busy(True, f"[{n}/{len(ids)}] {verb} {name}...")
            try:
                update_gameinfo(game_path, name, self.log, remove=not enabled)
                entry["enabled"] = enabled
                changed += 1
            except Exception as e:
                self.log(f"ERROR {verb.lower()[:-3]}ing '{name}': {e} — skipping and continuing.")
        save_config(self._cfg)
        state = "enabled" if enabled else "disabled"
        summary = f"{state.capitalize()} {changed} addon(s)."
        self.log(summary)
        self._set_busy(False, summary)
        self._toast(summary, success=True)
        self._push_state()

    def remove_many(self, item_ids: list):
        game_path = self._validate_game_path()
        if not game_path:
            return
        ids = [str(i) for i in (item_ids or []) if str(i) in self._cfg.get("installed", {})]
        if not ids:
            return
        threading.Thread(target=self._remove_many_worker, args=(ids, game_path), daemon=True).start()

    def _remove_many_worker(self, ids, game_path):
        removed, failed = 0, []
        for n, iid in enumerate(ids, start=1):
            entry = self._cfg.get("installed", {}).get(iid)
            if not entry:
                continue
            name = entry["folder_name"]
            self._set_busy(True, f"[{n}/{len(ids)}] Removing {name}...")
            try:
                uninstall_addon(iid, game_path, name, self.log)
                del self._cfg["installed"][iid]
                for members in self._cfg.get("modlists", {}).values():
                    if iid in members:
                        members.remove(iid)
                # Saved per item rather than once at the end: if something
                # fails partway, config.json still matches what's on disk.
                save_config(self._cfg)
                removed += 1
            except Exception as e:
                failed.append(name)
                self.log(f"ERROR removing '{name}': {e} — skipping and continuing.")
        summary = f"Removed {removed} addon(s)." + (f" {len(failed)} failed: {', '.join(failed)}" if failed else "")
        self.log(summary)
        self._set_busy(False, summary)
        self._toast(summary, success=not failed)
        self._push_state()

    def toggle_addon(self, item_id: str, enabled: bool):
        entry = self._cfg.get("installed", {}).get(item_id)
        if not entry:
            return
        game_path = self._validate_game_path()
        if not game_path:
            self._push_state()
            return
        threading.Thread(
            target=self._toggle_worker, args=(item_id, entry["folder_name"], game_path, enabled), daemon=True
        ).start()

    def _toggle_worker(self, item_id, folder_name, game_path, enabled):
        verb = "Enabling" if enabled else "Disabling"
        self._set_busy(True, f"{verb} {folder_name}...")
        try:
            update_gameinfo(game_path, folder_name, self.log, remove=not enabled)
            self._cfg["installed"][item_id]["enabled"] = enabled
            save_config(self._cfg)
            state = "enabled" if enabled else "disabled"
            self.log(f"{folder_name} is now {state}.")
            self._set_busy(False, f"{folder_name} {state}.")
            self._push_state()
        except Exception as e:
            self.log(f"ERROR toggling addon: {e}")
            self._set_busy(False, "Failed — see log.")
            self._toast(str(e), success=False)
            self._push_state()

    def set_all_enabled(self, enabled: bool):
        if not self._cfg.get("installed"):
            return
        game_path = self._validate_game_path()
        if not game_path:
            return
        threading.Thread(target=self._set_all_worker, args=(game_path, enabled), daemon=True).start()

    def _set_all_worker(self, game_path, enabled):
        verb = "Enabling" if enabled else "Disabling"
        self._set_busy(True, f"{verb} all addons...")
        try:
            for item_id, entry in list(self._cfg.get("installed", {}).items()):
                update_gameinfo(game_path, entry["folder_name"], self.log, remove=not enabled)
                entry["enabled"] = enabled
            save_config(self._cfg)
            state = "enabled" if enabled else "disabled"
            self.log(f"All addons {state}.")
            self._set_busy(False, f"All addons {state}.")
            self._push_state()
        except Exception as e:
            self.log(f"ERROR: {e}")
            self._set_busy(False, "Failed — see log.")
            self._toast(str(e), success=False)
            self._push_state()

    def open_workshop_page(self, item_id: str):
        webbrowser.open(f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}")

    def open_github(self):
        webbrowser.open(GITHUB_URL)

    def open_url(self, url: str):
        if url:
            webbrowser.open(url)

    def check_for_update(self):
        """Checks GitHub for a newer tagged release than this build.
        Returns None on any failure (offline, rate-limited, no releases
        yet) so the UI just quietly skips the banner rather than showing
        an error for something this minor."""
        try:
            req = urllib.request.Request(
                GITHUB_API_LATEST_RELEASE,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "L4D2AddonManager"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            latest = (data.get("tag_name") or "").strip().lstrip("vV")
            if not latest:
                return None
            return {
                "has_update": _version_tuple(latest) > _version_tuple(APP_VERSION),
                "latest": latest,
                "current": APP_VERSION,
                "url": data.get("html_url") or f"{GITHUB_URL}/releases/latest",
            }
        except Exception:
            return None

    # ---- modlists ----------------------------------------------------------

    def create_modlist(self, name: str):
        name = name.strip()
        if not name:
            return
        modlists = self._cfg.setdefault("modlists", {})
        if name in modlists:
            self._toast(f"A modlist named '{name}' already exists.", success=False)
            return
        modlists[name] = []
        save_config(self._cfg)
        self._push_state()

    def delete_modlist(self, name: str):
        self._cfg.get("modlists", {}).pop(name, None)
        save_config(self._cfg)
        self._push_state()

    def toggle_modlist_member(self, modlist_name: str, item_id: str, checked: bool):
        members = self._cfg.setdefault("modlists", {}).setdefault(modlist_name, [])
        if checked and item_id not in members:
            members.append(item_id)
        elif not checked and item_id in members:
            members.remove(item_id)
        save_config(self._cfg)
        self._push_state()

    def add_to_modlist(self, item_id: str, modlist_name: str):
        self.toggle_modlist_member(modlist_name, item_id, True)

    def add_many_to_modlist(self, modlist_name: str, item_ids: list) -> int:
        """Bulk version for drag-selection: adds every given addon to the
        modlist in one save, creating the modlist if it doesn't exist yet.
        Ignores IDs that aren't installed and ones already in the list.
        Returns how many were newly added."""
        name = str(modlist_name or "").strip()
        if not name:
            return 0
        installed = self._cfg.get("installed", {})
        members = self._cfg.setdefault("modlists", {}).setdefault(name, [])
        existing = set(members)
        added = 0
        for iid in item_ids or []:
            iid = str(iid)
            if iid in installed and iid not in existing:
                members.append(iid)
                existing.add(iid)
                added += 1
        save_config(self._cfg)
        skipped = len(item_ids or []) - added
        msg = f"Added {added} addon(s) to '{name}'."
        if skipped:
            msg += f" {skipped} were already in it."
        self._toast(msg, success=True)
        self._push_state()
        return added

    def set_modlist_all(self, name: str, add: bool):
        if add:
            self._cfg.setdefault("modlists", {})[name] = list(self._cfg.get("installed", {}).keys())
        else:
            self._cfg.setdefault("modlists", {})[name] = []
        save_config(self._cfg)
        self._push_state()

    def apply_modlist(self, name: str):
        game_path = self._validate_game_path()
        if not game_path:
            return
        threading.Thread(target=self._apply_modlist_worker, args=(game_path, name), daemon=True).start()

    def _apply_modlist_worker(self, game_path, name):
        self._set_busy(True, f"Applying modlist '{name}'...")
        try:
            members = set(self._cfg.get("modlists", {}).get(name, []))
            for item_id, entry in list(self._cfg.get("installed", {}).items()):
                want_enabled = item_id in members
                update_gameinfo(game_path, entry["folder_name"], self.log, remove=not want_enabled)
                entry["enabled"] = want_enabled
            save_config(self._cfg)
            self.log(f"Applied modlist '{name}': {len(members)} mod(s) enabled, rest disabled.")
            self._set_busy(False, f"Modlist '{name}' applied.")
            self._toast(f"Modlist '{name}' applied!", success=True)
            self._push_state()
        except Exception as e:
            self.log(f"ERROR applying modlist: {e}")
            self._set_busy(False, "Failed — see log.")
            self._toast(str(e), success=False)

    # ---- settings -----------------------------------------------------------

    def set_theme(self, name: str):
        if name not in THEMES:
            return
        self._cfg["theme"] = name
        save_config(self._cfg)
        self._push_state()

    def set_confirm_remove(self, value: bool):
        self._cfg["confirm_remove"] = bool(value)
        save_config(self._cfg)

    def set_log_hidden(self, value: bool):
        self._cfg["log_hidden"] = bool(value)
        save_config(self._cfg)

    def get_player_count(self):
        count = fetch_l4d2_player_count()
        return count

    def run_rename_migration(self):
        game_path = self._validate_game_path()
        if not game_path:
            return
        threading.Thread(target=self._rename_migration_worker, args=(game_path,), daemon=True).start()

    def _rename_migration_worker(self, game_path):
        self._set_busy(True, "Cleaning up addon names...")
        renamed = skipped = failed = 0
        try:
            installed = self._cfg.get("installed", {})
            for item_id, entry in list(installed.items()):
                old_name = entry["folder_name"]
                base_title = TRAILING_ID_SUFFIX.sub("", old_name).strip()
                if base_title == old_name:
                    skipped += 1
                    continue
                new_name = unique_folder_name(base_title, self._cfg, game_path, exclude_item_id=item_id)
                if new_name == old_name:
                    skipped += 1
                    continue
                try:
                    old_dir = game_path / old_name
                    new_dir = game_path / new_name
                    if old_dir.exists():
                        old_dir.rename(new_dir)
                        self.log(f"Renamed folder: '{old_name}' -> '{new_name}'")
                    else:
                        self.log(f"Note: folder '{old_name}' not found on disk, updating gameinfo.txt only.")
                    was_enabled = entry.get("enabled", True)
                    update_gameinfo(game_path, old_name, self.log, remove=True)
                    if was_enabled:
                        update_gameinfo(game_path, new_name, self.log, remove=False)
                    entry["folder_name"] = new_name
                    renamed += 1
                except Exception as item_err:
                    failed += 1
                    self.log(f"ERROR renaming '{old_name}': {item_err}")
            save_config(self._cfg)
            summary = f"Renamed {renamed} addon(s), {skipped} already clean, {failed} failed."
            self.log(summary)
            self._set_busy(False, summary)
            self._toast(summary, success=(failed == 0))
            self._push_state()
        except Exception as e:
            self.log(f"ERROR during cleanup: {e}")
            self._set_busy(False, "Failed — see log.")
            self._toast(str(e), success=False)

    def rescan_installed_addons(self):
        game_path = self._validate_game_path()
        if not game_path:
            return
        threading.Thread(target=self._rescan_worker, args=(game_path,), daemon=True).start()

    def _rescan_worker(self, game_path):
        self._set_busy(True, "Scanning for addon folders...")
        try:
            found = rescan_addons(self._cfg, game_path, self.log)
            save_config(self._cfg)

            self._set_busy(True, "Checking for missing Workshop images...")
            pics = self._fetch_missing_previews(force=True)

            folder_msg = f"Found {found} untracked addon folder(s)." if found else "No untracked addon folders found."
            pic_msg = f"Updated {pics} image(s)." if pics else "No missing images to update."
            summary = f"{folder_msg} {pic_msg}"
            self.log(summary)
            self._set_busy(False, summary)
            self._toast(summary, success=True)
            self._push_state()
        except Exception as e:
            self.log(f"ERROR during rescan: {e}")
            self._set_busy(False, "Failed — see log.")
            self._toast(str(e), success=False)


# --------------------------------------------------------------------------
# Frontend (HTML/CSS/JS embedded so the whole app stays a single .py file)
# --------------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
:root {
  --bg-app: #1E1E22; --bg-card: #28282E; --accent: #E4E4E7; --accent-hover: #bababd;
  --accent-text: #111111; --text-dim: #9A9AA2; --neutral-btn: #38383F;
  --neutral-btn-hover: #55555b; --danger: #F04747; --danger-hover: #2b0c0c;
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body {
  background: var(--bg-app); color: #fff;
  font-family: Inter, "Segoe UI", Ubuntu, -apple-system, sans-serif;
  display: flex; flex-direction: column; overflow: hidden;
  opacity: 0; transform: scale(0.97) translateY(10px);
  transition: opacity .45s cubic-bezier(.16,1,.3,1), transform .45s cubic-bezier(.16,1,.3,1);
}
body.loaded { opacity: 1; transform: scale(1) translateY(0); }
h1, h2 { margin: 0; }
p { margin: 0; }
.header { padding: 20px 24px 6px; flex-shrink: 0; }
.title-row { display: flex; align-items: center; }
.title-row h1 { font-size: 24px; font-weight: 700; }
.version-box { margin-left: auto; text-align: right; }
.version-box .version { font-size: 24px; font-weight: 700; color: #fff; }
.player-count { font-size: 11px; color: var(--text-dim); margin-top: 4px; }
.update-banner {
  display: none; align-items: center; gap: 10px; margin-top: 8px; padding: 8px 12px;
  border-radius: 8px; background: color-mix(in srgb, var(--accent) 18%, var(--bg-card));
  border: 1px solid color-mix(in srgb, var(--accent) 50%, transparent); font-size: 12px;
}
.update-banner.show { display: flex; }
.update-banner span { flex: 1; font-weight: 600; }
.nav { display: flex; gap: 8px; padding: 6px 24px 10px; flex-shrink: 0; }
.nav-btn {
  padding: 8px 20px; border-radius: 8px; border: none; background: var(--neutral-btn);
  color: #fff; font-weight: 700; font-size: 13px; cursor: pointer;
  font-family: inherit; transition: background-color .15s ease;
}
.nav-btn.active { background: var(--accent); color: var(--accent-text); }
.content { flex: 1; min-height: 0; display: flex; flex-direction: column; }
.tab-panel { display: none; flex-direction: column; flex: 1; min-height: 0; }
.tab-panel.active { display: flex; animation: fadein .18s ease; }
@keyframes fadein { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
.subtitle { font-size: 12px; color: var(--text-dim); padding: 0 24px 8px; flex-shrink: 0; }
.card { background: var(--bg-card); border-radius: 14px; margin: 8px 24px; flex-shrink: 0; }
.card-label {
  font-size: 11px; font-weight: 700; color: var(--text-dim); text-transform: uppercase;
  letter-spacing: .03em; padding: 12px 16px 8px;
}
.row { display: flex; gap: 8px; padding: 0 16px 10px; align-items: center; }
input[type=text] {
  flex: 1; background: #00000030; border: 1px solid #ffffff1a; border-radius: 8px;
  padding: 9px 10px; color: #fff; font-size: 13px; font-family: inherit; min-width: 0;
}
input[type=text]:focus { outline: 1px solid var(--accent); }
input[type=text]::placeholder { color: #ffffff55; }
.btn {
  border: none; border-radius: 8px; padding: 9px 14px; font-weight: 700; cursor: pointer;
  font-size: 13px; font-family: inherit; white-space: nowrap;
  transition: background-color .15s ease, transform .05s ease;
}
.btn:active { transform: scale(0.97); }
.btn:disabled { opacity: .5; cursor: default; }
.btn-accent { background: var(--accent); color: var(--accent-text); }
.btn-accent:hover { background: var(--accent-hover); }
.btn-neutral { background: var(--neutral-btn); color: #fff; }
.btn-neutral:hover { background: var(--neutral-btn-hover); }
.btn-outline { background: transparent; border: 1px solid var(--text-dim); color: var(--text-dim); }
.btn-outline:hover { background: var(--neutral-btn-hover); }
.btn-danger-outline { background: transparent; border: 1px solid var(--danger); color: var(--danger); }
.btn-danger-outline:hover { background: var(--danger-hover); }
.btn-small { padding: 4px 10px; font-size: 11px; border-radius: 6px; }
.progress-track {
  height: 6px; background: #ffffff14; border-radius: 3px; margin: 0 16px 8px;
  overflow: hidden; position: relative; display: none;
}
.progress-track.active { display: block; }
/* Real percentage while downloading (the sliding animation is for steps with no measurable progress). */
.progress-track.determinate .progress-bar { animation: none; left: 0; transition: width .35s ease; }
.progress-bar { position: absolute; top: 0; bottom: 0; width: 40%; background: var(--accent); border-radius: 3px; animation: indeterminate 1.1s infinite ease-in-out; }
@keyframes indeterminate { 0% { left: -40%; } 100% { left: 100%; } }
.status-text { font-size: 11px; color: var(--text-dim); padding: 0 16px 12px; }
.split-container { display: flex; flex-direction: column; flex: 1; margin: 10px 24px 20px; min-height: 0; }
.split-top { display: flex; flex-direction: column; flex: 0 0 68%; min-height: 80px; }
.split-bottom { display: flex; flex-direction: column; flex: 1; min-height: 80px; }
.split-divider { height: 10px; cursor: row-resize; position: relative; flex-shrink: 0; }
.split-divider::after { content: ''; position: absolute; left: 0; right: 0; top: 4px; height: 2px; background: #ffffff22; border-radius: 2px; }
.list-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-shrink: 0; flex-wrap: wrap; }
.list-header .card-label { padding: 0; margin-right: auto; }
.list-header input[type=text] { flex: 1 1 260px; min-width: 220px; height: 30px; padding: 4px 12px; font-size: 13px; }
/* Match the taller search box so the addon toolbar lines up in one even row. */
#splitTop .list-header select { height: 30px; font-size: 12px; padding: 2px 8px; }
#splitTop .list-header .btn-small { height: 30px; padding: 0 12px; font-size: 12px; }
.list-header select {
  height: 24px; font-size: 11px; font-family: inherit; padding: 2px 6px; cursor: pointer;
  background: var(--neutral-btn); color: #fff; border: 1px solid #ffffff1a; border-radius: 6px;
}
.list-header select:focus { outline: 1px solid var(--accent); }
.scroll-list { flex: 1; overflow-y: auto; padding-right: 2px; }
.log-box {
  flex: 1; overflow-y: auto; background: #00000040; border-radius: 12px; padding: 10px 12px;
  font-family: Consolas, monospace; font-size: 12px; white-space: pre-wrap; margin: 0; user-select: text;
}
.empty-msg { color: var(--text-dim); font-size: 12px; padding: 8px 0; }
.addon-row {
  display: flex; align-items: center; background: var(--bg-card); border-radius: 10px;
  padding: 8px 12px; margin-bottom: 8px; gap: 10px;
  border: 1px solid transparent; transition: background-color .08s ease, border-color .08s ease;
}
.addon-row.selected {
  background: color-mix(in srgb, var(--accent) 16%, var(--bg-card));
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
}
#addonList { position: relative; }
.marquee {
  position: absolute; pointer-events: none; z-index: 5; border-radius: 4px;
  border: 1px solid var(--accent);
  background: color-mix(in srgb, var(--accent) 14%, transparent);
}
body.dragging, body.dragging * { user-select: none !important; cursor: default; }
/* Always takes up the same space, selected or not. If it only appeared
   once something was selected, it would push the list down mid-drag and
   the rows would move out from under the cursor. */
.selection-bar {
  display: flex; align-items: center; gap: 8px; flex-shrink: 0; flex-wrap: nowrap;
  height: 34px; margin-bottom: 6px; padding: 0 10px; border-radius: 8px;
  border: 1px dashed #ffffff14; font-size: 12px; overflow: hidden;
  transition: background-color .12s ease, border-color .12s ease;
}
.selection-bar.show {
  background: color-mix(in srgb, var(--accent) 12%, var(--bg-card));
  border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
}
.selection-bar .hint { color: var(--text-dim); font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.selection-bar .count { font-weight: 700; margin-right: auto; white-space: nowrap; }
.selection-bar:not(.show) .count, .selection-bar:not(.show) button { display: none; }
.selection-bar.show .hint { display: none; }
.bulk-row {
  display: flex; align-items: center; gap: 8px; padding: 6px 0;
  border-bottom: 1px solid #ffffff10; font-size: 13px;
}
.bulk-row .bulk-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bulk-row .bulk-meta { color: var(--text-dim); font-size: 11px; white-space: nowrap; }
.addon-info { flex: 1; min-width: 0; }
.addon-thumb {
  width: 44px; height: 44px; border-radius: 8px; flex-shrink: 0; overflow: hidden;
  background: var(--neutral-btn); display: flex; align-items: center; justify-content: center;
}
.addon-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; -webkit-user-drag: none; }
.addon-thumb.placeholder { font-weight: 700; font-size: 18px; color: var(--text-dim); }
.addon-thumb.small { width: 32px; height: 32px; border-radius: 6px; font-size: 14px; }
/* Disabled addons get a dimmed, desaturated image so the list scans at a glance. */
.addon-row.is-disabled .addon-thumb img { filter: grayscale(1); opacity: .45; }
.addon-name { font-weight: 700; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.addon-name.disabled { color: var(--text-dim); }
.addon-sub { font-size: 11px; color: var(--text-dim); }
.switch { position: relative; display: inline-block; width: 36px; height: 20px; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; cursor: pointer; inset: 0; background: #666; border-radius: 20px; transition: .2s; }
.slider::before { content: ''; position: absolute; height: 16px; width: 16px; left: 2px; top: 2px; background: white; border-radius: 50%; transition: .2s; }
.switch input:checked + .slider { background: var(--accent); }
.switch input:checked + .slider::before { transform: translateX(16px); }
.checkbox-row { display: flex; align-items: center; gap: 8px; padding: 8px 0; font-size: 13px; cursor: pointer; }
.checkbox-row input { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
.detail-header { display: flex; align-items: center; gap: 12px; padding: 0 24px 10px; flex-shrink: 0; }
.theme-row { display: flex; flex-wrap: wrap; gap: 8px; padding: 0 16px 16px; }
.theme-btn { min-width: 100px; }
.switch-row { display: flex; align-items: center; gap: 10px; padding: 0 16px 14px; font-size: 13px; cursor: pointer; }
.switch-row input { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
.desc { font-size: 11px; color: var(--text-dim); padding: 0 16px 10px; line-height: 1.5; }
.settings-scroll { flex: 1; overflow-y: auto; padding-bottom: 10px; }
.toast {
  position: fixed; top: 16px; left: 50%; transform: translate(-50%, -20px); background: #22c55e;
  color: white; padding: 10px 18px; border-radius: 8px; font-weight: 700; font-size: 13px;
  opacity: 0; transition: all .25s ease; pointer-events: none; z-index: 999; max-width: 80%;
  text-align: center; white-space: pre-wrap;
}
.toast.show { opacity: 1; transform: translate(-50%, 0); }
.toast.error { background: var(--danger); }
.modal-overlay {
  position: fixed; inset: 0; background: #00000090; display: none; align-items: center;
  justify-content: center; z-index: 1000;
}
.modal-overlay.show { display: flex; }
.modal-card {
  background: var(--bg-app); border: 1px solid #ffffff14; border-radius: 14px; padding: 20px;
  width: 340px; max-height: 80vh; display: flex; flex-direction: column; gap: 10px;
}
.modal-title { font-size: 15px; font-weight: 700; }
.modal-list { max-height: 220px; overflow-y: auto; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: #ffffff22; border-radius: 6px; }
::-webkit-scrollbar-thumb:hover { background: #ffffff3a; }
</style>
</head>
<body>

<div class="toast" id="toast"></div>

<div class="header">
  <div class="title-row">
    <h1>L4D2 Addon Manager</h1>
    <div class="version-box">
      <div class="version" id="versionLabel"></div>
    </div>
  </div>
  <div class="player-count" id="playerCount">Checking player count...</div>
  <div class="update-banner" id="updateBanner">
    <span id="updateBannerText"></span>
    <button class="btn btn-small btn-accent" id="updateBannerGetBtn">Get it</button>
    <button class="btn btn-small btn-neutral" id="updateBannerDismissBtn">Dismiss</button>
  </div>
</div>

<div class="nav">
  <button class="nav-btn" data-tab="mods">Mods</button>
  <button class="nav-btn" data-tab="modlists">Modlists</button>
  <button class="nav-btn" data-tab="settings">Settings</button>
</div>

<div class="content">

  <div class="tab-panel" id="tab-mods">
    <p class="subtitle">Paste a workshop addon or collection link. It handles the download, the vpk rename, and gameinfo.txt.</p>

    <div class="card">
      <div class="card-label">GAME FOLDER</div>
      <div class="row">
        <input type="text" id="gamePathInput" placeholder="C:\\...\\Steam\\steamapps\\common\\Left 4 Dead 2">
        <button class="btn btn-neutral" id="browseBtn">Browse</button>
      </div>
      <div class="row">
        <input type="text" id="launchOptsInput" placeholder="Launch options (e.g. -novid -console -windowed)">
        <button class="btn btn-accent" id="launchBtn">&#9654;&nbsp; Launch L4D2</button>
      </div>
    </div>

    <div class="card">
      <div class="card-label">WORKSHOP ADDON OR COLLECTION</div>
      <div class="row">
        <input type="text" id="urlInput" placeholder="https://steamcommunity.com/sharedfiles/filedetails/?id=...">
        <button class="btn btn-neutral" id="pasteBtn">Paste</button>
        <button class="btn btn-accent" id="installBtn">&#8681;&nbsp; Install</button>
      </div>
      <div class="progress-track" id="progressTrack"><div class="progress-bar"></div></div>
      <div class="status-text" id="statusText">Ready.</div>
    </div>

    <div class="split-container" id="splitContainer">
      <div class="split-top" id="splitTop">
        <div class="list-header">
          <span class="card-label">INSTALLED ADDONS</span>
          <input type="text" id="searchInput" placeholder="Search installed addons...">
          <select id="sortSelect" title="Sort installed addons">
            <option value="date" selected>Date installed (newest)</option>
            <option value="name">Name (A–Z)</option>
            <option value="size">Size (largest first)</option>
            <option value="status">Status (enabled first)</option>
          </select>
          <button class="btn btn-small btn-neutral" id="hideLogBtn">Hide Log</button>
          <button class="btn btn-small btn-neutral" id="disableAllBtn">Disable All</button>
          <button class="btn btn-small btn-neutral" id="enableAllBtn">Enable All</button>
        </div>
        <div class="selection-bar" id="selectionBar">
          <span class="hint">Tip: drag across addons, or Ctrl / Shift-click, to select several at once</span>
          <span class="count" id="selectionCount"></span>
          <button class="btn btn-small btn-accent" id="bulkModlistBtn">+ Add to Modlist</button>
          <button class="btn btn-small btn-neutral" id="bulkEnableBtn">Enable</button>
          <button class="btn btn-small btn-neutral" id="bulkDisableBtn">Disable</button>
        </div>
        <div class="scroll-list" id="addonList"></div>
      </div>
      <div class="split-divider" id="splitDivider"></div>
      <div class="split-bottom" id="splitBottom">
        <div class="list-header">
          <span class="card-label">LOG</span>
          <button class="btn btn-small btn-neutral" id="clearLogBtn">Clear</button>
          <button class="btn btn-small btn-neutral" id="copyLogBtn">Copy log</button>
        </div>
        <pre class="log-box" id="logBox"></pre>
      </div>
    </div>
  </div>

  <div class="tab-panel" id="tab-modlists">
    <div id="modlistListView" style="display:flex; flex-direction:column; flex:1; min-height:0;">
      <p class="subtitle">Group your installed mods into modlists — a competitive loadout, a graphical singleplayer loadout, whatever you want. Applying a modlist enables its mods and disables everything else.</p>
      <div class="card">
        <div class="row" style="padding: 14px 16px;">
          <input type="text" id="newModlistInput" placeholder="New modlist name (e.g. Competitive)">
          <button class="btn btn-accent" id="createModlistBtn">+ Create</button>
        </div>
      </div>
      <div class="scroll-list" id="modlistCards" style="margin: 4px 24px 20px;"></div>
    </div>

    <div id="modlistDetailView" style="display:none; flex-direction:column; flex:1; min-height:0;">
      <div class="detail-header">
        <button class="btn btn-neutral" id="backToModlistsBtn">&#8592; Back</button>
        <h2 id="modlistDetailName"></h2>
        <button class="btn btn-accent" id="applyModlistBtn" style="margin-left:auto;">Apply This Modlist</button>
      </div>
      <p class="subtitle">Applying this modlist enables everything below and disables everything else.</p>
      <div class="row" style="padding: 0 24px 10px;">
        <input type="text" id="modlistSearchInput" placeholder="Search this modlist...">
        <button class="btn btn-small btn-accent" id="modlistAddModsBtn">+ Add Mods</button>
        <button class="btn btn-small btn-neutral" id="modlistClearAllBtn">Clear All</button>
      </div>
      <div class="scroll-list" id="modlistMembers" style="margin: 0 24px 20px;"></div>
    </div>
  </div>

  <div class="tab-panel" id="tab-settings">
    <div class="settings-scroll">
      <div class="card">
        <div class="card-label">THEME</div>
        <div class="theme-row" id="themeRow"></div>
      </div>
      <div class="card">
        <div class="card-label">BEHAVIOR</div>
        <label class="switch-row"><input type="checkbox" id="confirmRemoveToggle">Ask for confirmation before removing an addon</label>
      </div>
      <div class="card">
        <div class="card-label">MAINTENANCE</div>
        <p class="desc">Older versions of this app put the Workshop ID in each addon's name, e.g. "Some Addon (123456789)". Since the ID already shows underneath each addon, this renames existing addon folders and updates gameinfo.txt to drop the ID from the name.</p>
        <div style="padding: 0 16px 16px;"><button class="btn btn-neutral" id="renameMigrationBtn">Clean Up Addon Names</button></div>
        <p class="desc">If your installed-addons list ever comes up empty or missing entries (e.g. after moving to a new folder), or if some addons are missing their preview picture, this scans your L4D2 folder and gameinfo.txt directly and re-checks Workshop images for anything that doesn't have one yet. It never touches or deletes any files.</p>
        <div style="padding: 0 16px 16px;"><button class="btn btn-neutral" id="rescanBtn">Rescan Installed Addons</button></div>
      </div>
      <div class="card">
        <div class="card-label">ABOUT</div>
        <p class="desc">Made by Tanner. Check the GitHub page for updates, to report a bug, or to see what's changed.</p>
        <div style="padding: 0 16px 16px;"><button class="btn btn-neutral" id="githubBtn">Check GitHub for Updates</button></div>
      </div>
    </div>
  </div>

</div>

<div class="modal-overlay" id="modlistModal">
  <div class="modal-card">
    <div class="modal-title" id="modalAddonName"></div>
    <p class="desc" style="padding:0;">Check the modlists this addon should belong to:</p>
    <div class="modal-list" id="modalModlistChecks"></div>
    <div class="row" style="padding:0;">
      <input type="text" id="modalNewModlistInput" placeholder="New modlist name">
      <button class="btn btn-accent" id="modalCreateBtn">+ Create &amp; Add</button>
    </div>
    <button class="btn btn-neutral" id="modalDoneBtn">Done</button>
  </div>
</div>

<div class="modal-overlay" id="bulkModal">
  <div class="modal-card">
    <div class="modal-title" id="bulkModalTitle"></div>
    <p class="desc" style="padding:0;">Pick a modlist to add them to:</p>
    <div class="modal-list" id="bulkModlistRows"></div>
    <div class="row" style="padding:0;">
      <input type="text" id="bulkNewModlistInput" placeholder="New modlist name">
      <button class="btn btn-accent" id="bulkCreateBtn">+ Create &amp; Add</button>
    </div>
    <button class="btn btn-neutral" id="bulkCancelBtn">Cancel</button>
  </div>
</div>

<div class="modal-overlay" id="addModsModal">
  <div class="modal-card">
    <div class="modal-title" id="addModsModalTitle"></div>
    <div class="row" style="padding:0;">
      <input type="text" id="addModsSearchInput" placeholder="Search installed addons...">
      <button class="btn btn-small btn-accent" id="addModsAddAllBtn">Add All</button>
    </div>
    <div class="modal-list" id="addModsRows" style="max-height:320px;"></div>
    <button class="btn btn-neutral" id="addModsDoneBtn">Done</button>
  </div>
</div>

<script>
let state = { installed: {}, modlists: {}, themes: {}, theme_names: [] };
let addonStats = {};
const AUTH_TOKEN = '__AUTH_TOKEN__';

// Talks to Python over the loopback HTTP server instead of pywebview's
// js_api bridge (see the Python-side comment on why that bridge is gone).
async function api(method, ...args) {
  const res = await fetch('/api/' + method, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Auth-Token': AUTH_TOKEN},
    body: JSON.stringify(args),
  });
  const data = await res.json();
  if (data.error) { console.error(method, data.error); return null; }
  return data.result;
}

// Python pushes UI updates onto a queue; this drains it continuously.
async function pollEvents() {
  for (;;) {
    try {
      const res = await fetch('/events', {headers: {'X-Auth-Token': AUTH_TOKEN}});
      const events = await res.json();
      for (const ev of events) {
        if (ev.type === 'log') appendLog(ev.text);
        else if (ev.type === 'busy') setBusy(ev.busy, ev.status);
        else if (ev.type === 'toast') showToast(ev.message, ev.success);
        else if (ev.type === 'state') applyState(ev.state);
        else if (ev.type === 'stats') applyAddonStats(ev.stats);
        else if (ev.type === 'progress') applyProgress(ev);
      }
    } catch (e) {
      await new Promise(r => setTimeout(r, 1000));  // server hiccup; back off
    }
  }
}
let currentModlistDetail = null;
let modalItemId = null;
let toastTimer = null;

function el(tag, props, children) {
  props = props || {};
  const e = document.createElement(tag);
  for (const k in props) {
    const v = props[k];
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.indexOf('on') === 0 && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  (children ? (Array.isArray(children) ? children : [children]) : []).forEach(c => {
    if (c == null) return;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  });
  return e;
}

function applyTheme(palette) {
  const root = document.documentElement;
  root.style.setProperty('--bg-app', palette.bg_app);
  root.style.setProperty('--bg-card', palette.bg_card);
  root.style.setProperty('--accent', palette.accent);
  root.style.setProperty('--accent-hover', palette.accent_hover);
  root.style.setProperty('--accent-text', palette.accent_text);
  root.style.setProperty('--text-dim', palette.text_dim);
  root.style.setProperty('--neutral-btn', palette.neutral_btn);
  root.style.setProperty('--neutral-btn-hover', palette.neutral_btn_hover);
  root.style.setProperty('--danger', palette.danger);
  root.style.setProperty('--danger-hover', palette.danger_hover);
}

function applyState(newState) {
  state = newState;
  document.getElementById('versionLabel').textContent = 'v' + state.version;
  document.getElementById('gamePathInput').value = state.game_path || '';
  document.getElementById('launchOptsInput').value = state.launch_options || '';
  document.getElementById('confirmRemoveToggle').checked = !!state.confirm_remove;
  applyTheme(state.themes[state.theme]);
  renderThemeRow();
  renderAddonList();
  if (currentModlistDetail && state.modlists[currentModlistDetail] !== undefined) {
    renderModlistDetail();
  } else {
    currentModlistDetail = null;
    document.getElementById('modlistListView').style.display = 'flex';
    document.getElementById('modlistDetailView').style.display = 'none';
    renderModlistCards();
  }
  if (modalItemId) renderModalModlists();
  if (document.getElementById('bulkModal').classList.contains('show')) renderBulkModal();
  if (document.getElementById('addModsModal').classList.contains('show')) renderAddModsModal();
  applyLogVisibility();
  // Sizes/dates can change after installs and removals, so refresh them
  // whenever the backend pushes new state. (Runs in a background thread
  // on the Python side; the list re-renders when results arrive.)
  api('load_addon_stats');
}

function setBusy(busy, status) {
  document.getElementById('installBtn').disabled = busy;
  document.getElementById('progressTrack').classList.toggle('active', busy);
  if (!busy) exitDeterminate();
  if (status) document.getElementById('statusText').textContent = status;
}

function exitDeterminate() {
  const track = document.getElementById('progressTrack');
  track.classList.remove('determinate');
  track.querySelector('.progress-bar').style.width = '';
}

function formatMB(bytes) { return (bytes / (1024 * 1024)).toFixed(1); }

function applyProgress(ev) {
  if (ev.done == null) { exitDeterminate(); return; }
  const track = document.getElementById('progressTrack');
  track.classList.add('active');
  let text = `${ev.prefix || ''}Downloading ${ev.name} — ${formatMB(ev.done)}`;
  if (ev.total > 0) {
    const pct = Math.min(100, (ev.done / ev.total) * 100);
    track.classList.add('determinate');
    track.querySelector('.progress-bar').style.width = pct.toFixed(1) + '%';
    text += ` / ${formatMB(ev.total)} MB (${Math.floor(pct)}%)`;
  } else {
    text += ' MB';   // Steam didn't report a size: show what's arrived, bar stays animated
  }
  if (ev.speed > 0) text += ` · ${formatMB(ev.speed)} MB/s`;
  document.getElementById('statusText').textContent = text;
}

function showToast(message, success) {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.classList.toggle('error', !success);
  toast.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove('show'), 3500);
}

function appendLog(text) {
  const box = document.getElementById('logBox');
  box.textContent += text + '\\n';
  box.scrollTop = box.scrollHeight;
}

/* ---------- Mods tab ---------- */

function formatSize(bytes) {
  if (bytes == null) return null;
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

function formatDate(ts) {
  if (!ts) return null;
  const d = new Date(ts * 1000);
  if (isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, {year: 'numeric', month: 'short', day: 'numeric'});
}

// Steam's image CDN can resize on the fly; asking for a small version
// means a big library loads a few KB per addon instead of full-size previews.
function thumbUrl(url) {
  if (!url) return '';
  try {
    const u = new URL(url);
    if (/steamuserimages|steamusercontent/.test(u.hostname)) {
      u.searchParams.set('imw', '128'); u.searchParams.set('imh', '128');
      u.searchParams.set('ima', 'fit'); u.searchParams.set('impolicy', 'Letterbox');
      u.searchParams.set('letterbox', 'false');
    }
    return u.toString();
  } catch (e) { return url; }
}

function buildThumb(entry, small) {
  const box = el('div', {class: 'addon-thumb' + (small ? ' small' : '')});
  const showLetter = () => {
    box.innerHTML = '';
    box.classList.add('placeholder');
    box.textContent = (entry.folder_name || '?').trim().charAt(0).toUpperCase() || '?';
  };
  if (!entry.preview_url) { showLetter(); return box; }
  const img = el('img', {alt: '', loading: 'lazy', draggable: 'false'});
  let triedOriginal = false;
  img.addEventListener('error', () => {
    // If the resized URL is refused, fall back to the original once, then
    // to a letter tile (offline, removed item, etc.).
    if (!triedOriginal) { triedOriginal = true; img.src = entry.preview_url; }
    else showLetter();
  });
  img.src = thumbUrl(entry.preview_url);
  box.appendChild(img);
  return box;
}

function buildAddonRow(id, entry) {
  const row = el('div', {class: 'addon-row' + (selected.has(id) ? ' selected' : '') + (entry.enabled ? '' : ' is-disabled')});
  row.dataset.id = id;
  row.appendChild(buildThumb(entry, false));
  const info = el('div', {class: 'addon-info'});
  info.appendChild(el('div', {class: 'addon-name' + (entry.enabled ? '' : ' disabled'), text: entry.folder_name}));

  const parts = ['Workshop ID ' + id, entry.enabled ? 'Enabled' : 'Disabled'];
  const st = addonStats[id];
  if (st) {
    if (st.missing) parts.push('folder missing');
    else {
      const size = formatSize(st.size);
      if (size) parts.push(size);
      const date = formatDate(st.installed_at);
      if (date) parts.push(date);
    }
  }
  info.appendChild(el('div', {class: 'addon-sub', text: parts.join(' \u00b7 ')}));
  row.appendChild(info);

  const switchLabel = el('label', {class: 'switch'});
  const checkbox = el('input', {type: 'checkbox'});
  checkbox.checked = entry.enabled;
  checkbox.addEventListener('change', () => api('toggle_addon', id, checkbox.checked));
  switchLabel.appendChild(checkbox);
  switchLabel.appendChild(el('span', {class: 'slider'}));
  row.appendChild(switchLabel);

  row.appendChild(el('button', {class: 'btn btn-small btn-outline', text: 'Workshop page', onclick: () => api('open_workshop_page', id)}));
  row.appendChild(el('button', {class: 'btn btn-small btn-danger-outline', text: 'Remove', onclick: () => confirmRemove(id)}));
  return row;
}

function sortEntries(entries) {
  const mode = document.getElementById('sortSelect').value;
  const copy = entries.slice();
  copy.sort((a, b) => {
    const [idA, eA] = a, [idB, eB] = b;
    const sA = addonStats[idA] || {}, sB = addonStats[idB] || {};
    if (mode === 'size') return (sB.size || 0) - (sA.size || 0);
    if (mode === 'date') {
      // Prefer the timestamp saved at install time (available instantly from
      // config) over the disk-scanned one, so the list is in the right order
      // on first paint instead of reshuffling once stats finish loading.
      const tA = eA.installed_at || sA.installed_at || 0;
      const tB = eB.installed_at || sB.installed_at || 0;
      if (tA !== tB) return tB - tA;
      return eA.folder_name.localeCompare(eB.folder_name);
    }
    if (mode === 'status') {
      if (eA.enabled !== eB.enabled) return eA.enabled ? -1 : 1;
      return eA.folder_name.localeCompare(eB.folder_name);
    }
    return eA.folder_name.localeCompare(eB.folder_name);
  });
  return copy;
}

function applyAddonStats(stats) {
  addonStats = stats || {};
  renderAddonList();
}

function renderAddonList() {
  const container = document.getElementById('addonList');
  container.innerHTML = '';
  const q = document.getElementById('searchInput').value.trim().toLowerCase();
  const entries = Object.entries(state.installed || {});
  const filtered = q ? entries.filter(([id, e]) => e.folder_name.toLowerCase().includes(q) || id.toLowerCase().includes(q)) : entries;
  if (entries.length === 0) container.appendChild(el('div', {class: 'empty-msg', text: 'No addons installed yet.'}));
  else if (filtered.length === 0) container.appendChild(el('div', {class: 'empty-msg', text: 'No addons match your search.'}));
  else sortEntries(filtered).forEach(([id, entry]) => container.appendChild(buildAddonRow(id, entry)));
  refreshSelectionUI();
}

/* ---------- Multi-select: drag a box, or click / Ctrl-click / Shift-click ---------- */

const selected = new Set();
let lastClickedId = null;   // anchor for Shift-click ranges

function visibleRowIds() {
  return [...document.querySelectorAll('#addonList .addon-row')].map(r => r.dataset.id);
}

function refreshSelectionUI() {
  // Forget anything that's been uninstalled since it was selected.
  for (const id of [...selected]) if (!(state.installed || {})[id]) selected.delete(id);
  document.querySelectorAll('#addonList .addon-row').forEach(r =>
    r.classList.toggle('selected', selected.has(r.dataset.id)));
  document.getElementById('selectionBar').classList.toggle('show', selected.size > 0);
  document.getElementById('selectionCount').textContent =
    selected.size + ' addon' + (selected.size === 1 ? '' : 's') + ' selected';
}

function clearSelection() { selected.clear(); lastClickedId = null; refreshSelectionUI(); }

function selectAllVisible() { visibleRowIds().forEach(id => selected.add(id)); refreshSelectionUI(); }

// Presses on real controls (toggle switch, buttons) must keep working
// normally rather than starting a selection.
function isInteractive(target) {
  return !!target.closest('button, input, select, label, a, textarea');
}

// Click anywhere blank outside the addon list — the list handles its own
// blank-space clicks already — to drop the current selection, as long as
// it's not an actual control (buttons, inputs, etc. elsewhere in the app)
// or something inside an open modal, which still needs the selection alive.
document.addEventListener('mousedown', e => {
  if (e.button !== 0 || !selected.size) return;
  if (e.target.closest('#addonList, .modal-overlay')) return;
  if (isInteractive(e.target)) return;
  clearSelection();
});

function handleRowClick({row, ctrl, shift}) {
  if (!row) { clearSelection(); return; }   // click on empty space deselects
  const id = row.dataset.id;
  if (shift && lastClickedId) {
    const ids = visibleRowIds();
    const a = ids.indexOf(lastClickedId), b = ids.indexOf(id);
    if (a !== -1 && b !== -1) {
      if (!ctrl) selected.clear();
      const [lo, hi] = a < b ? [a, b] : [b, a];
      for (let i = lo; i <= hi; i++) selected.add(ids[i]);
      refreshSelectionUI();
      return;
    }
  }
  if (ctrl) {
    if (selected.has(id)) selected.delete(id); else selected.add(id);
  } else {
    // Plain click selects just this row; clicking it again deselects it.
    const onlyThis = selected.size === 1 && selected.has(id);
    selected.clear();
    if (!onlyThis) selected.add(id);
  }
  lastClickedId = id;
  refreshSelectionUI();
}

(function setupDragSelect() {
  const list = document.getElementById('addonList');
  const THRESHOLD = 4;      // px of movement before a press counts as a drag
  // Auto-scroll only starts right at the list's edge (the list pane can be
  // short, so a wide trigger zone would scroll while you're still aiming at
  // a row). Speed ramps up the further past the edge you drag.
  const EDGE = 12;          // px inside the edge where scrolling begins
  const RAMP = 60;          // px past that point to reach full speed
  const SPEED = 18;         // max px per frame

  list.addEventListener('mousedown', e => {
    if (e.button !== 0 || isInteractive(e.target)) return;
    e.preventDefault();     // stop the browser starting a text selection

    const additive = e.ctrlKey || e.metaKey || e.shiftKey;
    const base = additive ? new Set(selected) : new Set();
    const pressed = {row: e.target.closest('.addon-row'), ctrl: e.ctrlKey || e.metaKey, shift: e.shiftKey};
    // Measured before the marquee exists: an absolutely-positioned box inside
    // a scroll container grows scrollHeight, which would otherwise let
    // auto-scroll run off into empty space forever.
    const contentH = Math.max(list.scrollHeight, list.clientHeight);
    const contentW = list.clientWidth;

    const toContent = (cx, cy) => {
      const r = list.getBoundingClientRect();
      return {
        x: Math.min(contentW, Math.max(0, cx - r.left + list.scrollLeft)),
        y: Math.min(contentH, Math.max(0, cy - r.top + list.scrollTop)),
      };
    };
    const start = toContent(e.clientX, e.clientY);
    let pointer = {x: e.clientX, y: e.clientY};
    let dragging = false, box = null, raf = null;

    function update() {
      const cur = toContent(pointer.x, pointer.y);
      const x1 = Math.min(start.x, cur.x), x2 = Math.max(start.x, cur.x);
      const y1 = Math.min(start.y, cur.y), y2 = Math.max(start.y, cur.y);
      Object.assign(box.style, {left: x1 + 'px', top: y1 + 'px', width: (x2 - x1) + 'px', height: (y2 - y1) + 'px'});
      selected.clear();
      base.forEach(id => selected.add(id));
      list.querySelectorAll('.addon-row').forEach(row => {
        const top = row.offsetTop, left = row.offsetLeft;
        if (top + row.offsetHeight >= y1 && top <= y2 && left + row.offsetWidth >= x1 && left <= x2) {
          selected.add(row.dataset.id);
        }
      });
      refreshSelectionUI();
    }

    function autoScroll() {
      if (!dragging) return;
      const r = list.getBoundingClientRect();
      let dy = 0;
      if (pointer.y < r.top + EDGE) dy = -SPEED * Math.min(1, (r.top + EDGE - pointer.y) / RAMP);
      else if (pointer.y > r.bottom - EDGE) dy = SPEED * Math.min(1, (pointer.y - (r.bottom - EDGE)) / RAMP);
      if (dy) {
        const before = list.scrollTop;
        list.scrollTop = Math.min(contentH - list.clientHeight, Math.max(0, before + dy));
        if (list.scrollTop !== before) update();
      }
      raf = requestAnimationFrame(autoScroll);
    }

    function onMove(ev) {
      pointer = {x: ev.clientX, y: ev.clientY};
      if (!dragging) {
        if (Math.abs(ev.clientX - e.clientX) < THRESHOLD && Math.abs(ev.clientY - e.clientY) < THRESHOLD) return;
        dragging = true;
        document.body.classList.add('dragging');
        box = el('div', {class: 'marquee'});
        list.appendChild(box);
        raf = requestAnimationFrame(autoScroll);
      }
      update();
    }

    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      if (dragging) {
        dragging = false;
        cancelAnimationFrame(raf);
        document.body.classList.remove('dragging');
        box.remove();
        if (pressed.row) lastClickedId = pressed.row.dataset.id;
      } else {
        handleRowClick(pressed);
      }
    }

    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  });
})();

/* ---------- Bulk add-to-modlist modal ---------- */

function openBulkModal() {
  if (!selected.size) return;
  const n = selected.size;
  document.getElementById('bulkModalTitle').textContent = `Add ${n} addon${n === 1 ? '' : 's'} to a modlist`;
  document.getElementById('bulkNewModlistInput').value = '';
  renderBulkModal();
  document.getElementById('bulkModal').classList.add('show');
}

function closeBulkModal() { document.getElementById('bulkModal').classList.remove('show'); }

function renderBulkModal() {
  const container = document.getElementById('bulkModlistRows');
  container.innerHTML = '';
  const modlists = state.modlists || {};
  const names = Object.keys(modlists);
  if (!names.length) {
    container.appendChild(el('div', {class: 'empty-msg', text: 'No modlists yet \\u2014 create one below.'}));
    return;
  }
  const ids = [...selected];
  names.forEach(name => {
    const members = new Set(modlists[name]);
    const already = ids.filter(i => members.has(i)).length;
    const allIn = already === ids.length;
    const row = el('div', {class: 'bulk-row'});
    row.appendChild(el('span', {class: 'bulk-name', text: name}));
    row.appendChild(el('span', {class: 'bulk-meta',
      text: already ? `${already} of ${ids.length} already in` : `${modlists[name].length} mods`}));
    const btn = el('button', {class: 'btn btn-small ' + (allIn ? 'btn-neutral' : 'btn-accent'),
      text: allIn ? 'All in' : 'Add', onclick: () => bulkAdd(name)});
    if (allIn) btn.disabled = true;
    row.appendChild(btn);
    container.appendChild(row);
  });
}

async function bulkAdd(name) {
  if (!name || !selected.size) return;
  closeBulkModal();
  // Selection is kept afterwards on purpose, so the same set can go into a
  // second modlist (e.g. both "Competitive" and "Graphical") without redoing the drag.
  await api('add_many_to_modlist', name, [...selected]);
}

function confirmBulkRemove() {
  const ids = [...selected];
  if (!ids.length) return;
  if (state.confirm_remove) {
    const names = ids.map(i => (state.installed[i] || {}).folder_name || i);
    const shown = names.slice(0, 8).map(n => '  \\u2022 ' + n).join('\\n');
    const more = names.length > 8 ? `\\n  ...and ${names.length - 8} more` : '';
    const msg = `Remove ${ids.length} addon${ids.length === 1 ? '' : 's'}? This deletes their files.\\n\\n${shown}${more}`;
    if (!confirm(msg)) return;
  }
  api('remove_many', ids);
}

function confirmRemove(id) {
  if (state.confirm_remove) {
    if (!confirm('Remove addon ' + id + '? This deletes its files.')) return;
  }
  api('remove_addon', id);
}

function applyLogVisibility() {
  const visible = !state.log_hidden;
  document.getElementById('splitBottom').style.display = visible ? 'flex' : 'none';
  document.getElementById('splitDivider').style.display = visible ? 'block' : 'none';
  document.getElementById('hideLogBtn').textContent = visible ? 'Hide Log' : 'Show Log';
  document.getElementById('splitTop').style.flex = visible ? '0 0 68%' : '1';
}

(function setupSplitDrag() {
  const divider = document.getElementById('splitDivider');
  const top = document.getElementById('splitTop');
  const container = document.getElementById('splitContainer');
  let dragging = false;
  divider.addEventListener('mousedown', () => { dragging = true; document.body.style.cursor = 'row-resize'; });
  window.addEventListener('mouseup', () => { dragging = false; document.body.style.cursor = ''; });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const rect = container.getBoundingClientRect();
    const pct = Math.min(85, Math.max(15, ((e.clientY - rect.top) / rect.height) * 100));
    top.style.flex = '0 0 ' + pct + '%';
  });
})();

/* ---------- Modlists tab ---------- */

function buildModlistCard(name, members) {
  const row = el('div', {class: 'addon-row'});
  const info = el('div', {class: 'addon-info'});
  info.appendChild(el('div', {class: 'addon-name', text: name}));
  info.appendChild(el('div', {class: 'addon-sub', text: members.length + ' mod' + (members.length !== 1 ? 's' : '')}));
  row.appendChild(info);
  row.appendChild(el('button', {class: 'btn btn-small btn-accent', text: 'Open', onclick: () => openModlistDetail(name)}));
  row.appendChild(el('button', {class: 'btn btn-small btn-danger-outline', text: 'Delete', onclick: () => deleteModlist(name)}));
  return row;
}

function renderModlistCards() {
  const container = document.getElementById('modlistCards');
  container.innerHTML = '';
  const modlists = state.modlists || {};
  const names = Object.keys(modlists);
  if (names.length === 0) { container.appendChild(el('div', {class: 'empty-msg', text: 'No modlists yet \u2014 create one above.'})); return; }
  names.forEach(name => container.appendChild(buildModlistCard(name, modlists[name])));
}

function deleteModlist(name) {
  if (!confirm("Delete modlist '" + name + "'? This doesn't remove any mods.")) return;
  api('delete_modlist', name);
}

function openModlistDetail(name) {
  currentModlistDetail = name;
  closeAddModsModal();
  document.getElementById('modlistListView').style.display = 'none';
  document.getElementById('modlistDetailView').style.display = 'flex';
  document.getElementById('modlistDetailName').textContent = name;
  document.getElementById('modlistSearchInput').value = '';
  renderModlistDetail();
}

function backToModlistList() {
  currentModlistDetail = null;
  closeAddModsModal();
  document.getElementById('modlistDetailView').style.display = 'none';
  document.getElementById('modlistListView').style.display = 'flex';
  renderModlistCards();
}

// Only members are shown here -- this used to list every installed addon
// with a checkbox, which buried what was actually in the modlist under a
// pile of unrelated unchecked mods and made it look incomplete even when
// it wasn't. Adding new mods now happens through the separate "+ Add Mods"
// picker below instead.
function buildModlistDetailRow(id, entry) {
  const row = el('div', {class: 'addon-row' + (entry.enabled ? '' : ' is-disabled')});
  row.appendChild(buildThumb(entry, false));
  const info = el('div', {class: 'addon-info'});
  info.appendChild(el('div', {class: 'addon-name' + (entry.enabled ? '' : ' disabled'), text: entry.folder_name}));
  info.appendChild(el('div', {class: 'addon-sub', text: entry.enabled ? 'Enabled' : 'Disabled'}));
  row.appendChild(info);
  row.appendChild(el('button', {class: 'btn btn-small btn-danger-outline', text: 'Remove',
    onclick: () => api('toggle_modlist_member', currentModlistDetail, id, false)}));
  return row;
}

function renderModlistDetail() {
  if (!currentModlistDetail) return;
  const container = document.getElementById('modlistMembers');
  container.innerHTML = '';
  const installed = state.installed || {};
  const memberIds = (state.modlists[currentModlistDetail] || []).filter(id => installed[id]);
  const q = document.getElementById('modlistSearchInput').value.trim().toLowerCase();
  const rows = memberIds.map(id => [id, installed[id]])
    .filter(([id, e]) => !q || e.folder_name.toLowerCase().includes(q) || id.toLowerCase().includes(q));
  if (memberIds.length === 0) {
    container.appendChild(el('div', {class: 'empty-msg', text: 'This modlist is empty \\u2014 click + Add Mods to add some.'}));
  } else if (rows.length === 0) {
    container.appendChild(el('div', {class: 'empty-msg', text: 'No mods in this modlist match your search.'}));
  } else {
    rows.forEach(([id, entry]) => container.appendChild(buildModlistDetailRow(id, entry)));
  }
}

/* ---------- Add Mods picker (separate from the members-only detail view) ---------- */

function openAddModsModal() {
  if (!currentModlistDetail) return;
  document.getElementById('addModsModalTitle').textContent = "Add mods to '" + currentModlistDetail + "'";
  document.getElementById('addModsSearchInput').value = '';
  renderAddModsModal();
  document.getElementById('addModsModal').classList.add('show');
}

function closeAddModsModal() { document.getElementById('addModsModal').classList.remove('show'); }

function renderAddModsModal() {
  const container = document.getElementById('addModsRows');
  container.innerHTML = '';
  if (!currentModlistDetail) return;
  const members = new Set(state.modlists[currentModlistDetail] || []);
  const q = document.getElementById('addModsSearchInput').value.trim().toLowerCase();
  const entries = Object.entries(state.installed || {}).filter(([id]) => !members.has(id));
  const filtered = q ? entries.filter(([id, e]) => e.folder_name.toLowerCase().includes(q) || id.toLowerCase().includes(q)) : entries;
  if (entries.length === 0) {
    container.appendChild(el('div', {class: 'empty-msg', text: 'Every installed addon is already in this modlist.'}));
  } else if (filtered.length === 0) {
    container.appendChild(el('div', {class: 'empty-msg', text: 'No addons match your search.'}));
  } else {
    filtered.forEach(([id, entry]) => {
      const row = el('div', {class: 'bulk-row'});
      row.appendChild(el('span', {class: 'bulk-name', text: entry.folder_name}));
      row.appendChild(el('button', {class: 'btn btn-small btn-accent', text: 'Add',
        onclick: () => api('toggle_modlist_member', currentModlistDetail, id, true)}));
      container.appendChild(row);
    });
  }
}

/* ---------- Add-to-modlist modal ---------- */

function openAddToModlistModal(itemId, folderName) {
  modalItemId = itemId;
  document.getElementById('modalAddonName').textContent = folderName;
  renderModalModlists();
  document.getElementById('modlistModal').classList.add('show');
}

function closeModal() {
  document.getElementById('modlistModal').classList.remove('show');
  modalItemId = null;
}

function renderModalModlists() {
  const container = document.getElementById('modalModlistChecks');
  container.innerHTML = '';
  const modlists = state.modlists || {};
  const names = Object.keys(modlists);
  if (names.length === 0) { container.appendChild(el('div', {class: 'empty-msg', text: 'No modlists yet \u2014 create one below.'})); return; }
  names.forEach(name => {
    const row = el('label', {class: 'checkbox-row'});
    const cb = el('input', {type: 'checkbox'});
    cb.checked = modlists[name].includes(modalItemId);
    cb.addEventListener('change', () => api('toggle_modlist_member', name, modalItemId, cb.checked));
    row.appendChild(cb);
    row.appendChild(el('span', {text: name}));
    container.appendChild(row);
  });
}

/* ---------- Settings tab ---------- */

function renderThemeRow() {
  const container = document.getElementById('themeRow');
  container.innerHTML = '';
  (state.theme_names || []).forEach(name => {
    const palette = state.themes[name];
    const isActive = name === state.theme;
    const btn = el('button', {class: 'btn theme-btn', text: name, onclick: () => api('set_theme', name)});
    if (isActive) { btn.style.background = palette.accent; btn.style.color = palette.accent_text; btn.style.fontWeight = '700'; }
    else { btn.style.background = 'var(--neutral-btn)'; btn.style.color = '#fff'; }
    container.appendChild(btn);
  });
}

/* ---------- Tabs ---------- */

function switchTab(tabId) {
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + tabId));
}

/* ---------- Player count ---------- */

async function refreshPlayerCount() {
  try {
    const count = await api('get_player_count');
    document.getElementById('playerCount').textContent = count != null
      ? count.toLocaleString() + ' people playing L4D2 right now'
      : 'Player count unavailable';
  } catch (e) { /* ignore */ }
}

async function checkForUpdate() {
  try {
    const info = await api('check_for_update');
    if (!info || !info.has_update) return;
    document.getElementById('updateBannerText').textContent =
      `A new version is available \\u2014 v${info.latest} (you're on v${info.current})`;
    document.getElementById('updateBannerGetBtn').onclick = () => api('open_url', info.url);
    document.getElementById('updateBanner').classList.add('show');
  } catch (e) { /* offline / GitHub unreachable -- just skip the banner */ }
}

/* ---------- Wire up events ---------- */

document.querySelectorAll('.nav-btn').forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

document.getElementById('browseBtn').addEventListener('click', async () => {
  const path = await api('browse_game_path');
  if (path) document.getElementById('gamePathInput').value = path;
});
document.getElementById('gamePathInput').addEventListener('change', e => api('save_game_path', e.target.value));
document.getElementById('launchOptsInput').addEventListener('change', e => api('save_launch_options', e.target.value));
document.getElementById('launchBtn').addEventListener('click', () => api('launch_game'));

document.getElementById('pasteBtn').addEventListener('click', async () => {
  const text = await api('get_clipboard');
  if (text) { document.getElementById('urlInput').value = text.trim(); document.getElementById('statusText').textContent = 'Pasted from clipboard.'; }
});
document.getElementById('installBtn').addEventListener('click', () => {
  const url = document.getElementById('urlInput').value.trim();
  if (!url) { alert('Paste a workshop URL or ID first.'); return; }
  api('install_addon', url);
  document.getElementById('urlInput').value = '';
});

document.getElementById('searchInput').addEventListener('input', renderAddonList);
document.getElementById('sortSelect').addEventListener('change', renderAddonList);

document.getElementById('bulkModlistBtn').addEventListener('click', openBulkModal);
document.getElementById('bulkEnableBtn').addEventListener('click', () => api('set_many_enabled', [...selected], true));
document.getElementById('bulkDisableBtn').addEventListener('click', () => api('set_many_enabled', [...selected], false));
document.getElementById('bulkCancelBtn').addEventListener('click', closeBulkModal);
document.getElementById('bulkModal').addEventListener('click', e => { if (e.target.id === 'bulkModal') closeBulkModal(); });
document.getElementById('bulkCreateBtn').addEventListener('click', () => {
  bulkAdd(document.getElementById('bulkNewModlistInput').value.trim());
});
document.getElementById('bulkNewModlistInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('bulkCreateBtn').click();
});

document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
  const onModsTab = document.getElementById('tab-mods').classList.contains('active');
  if (e.key === 'Delete' && selected.size && onModsTab && !typing) {
    e.preventDefault();
    confirmBulkRemove();
  }
  if (e.key === 'Escape') {
    if (document.getElementById('bulkModal').classList.contains('show')) closeBulkModal();
    else if (selected.size && onModsTab) clearSelection();
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a' && onModsTab && !typing) {
    e.preventDefault();   // select addons instead of every bit of text on the page
    selectAllVisible();
  }
});
document.getElementById('hideLogBtn').addEventListener('click', () => {
  state.log_hidden = !state.log_hidden;
  api('set_log_hidden', state.log_hidden);
  applyLogVisibility();
});
document.getElementById('enableAllBtn').addEventListener('click', () => api('set_all_enabled', true));
document.getElementById('disableAllBtn').addEventListener('click', () => api('set_all_enabled', false));
document.getElementById('clearLogBtn').addEventListener('click', () => { document.getElementById('logBox').textContent = ''; });
document.getElementById('copyLogBtn').addEventListener('click', () => {
  api('copy_log', document.getElementById('logBox').textContent);
  document.getElementById('statusText').textContent = 'Log copied to clipboard.';
});

document.getElementById('createModlistBtn').addEventListener('click', () => {
  const input = document.getElementById('newModlistInput');
  const name = input.value.trim();
  if (!name) return;
  api('create_modlist', name);
  input.value = '';
});
document.getElementById('newModlistInput').addEventListener('keydown', e => { if (e.key === 'Enter') document.getElementById('createModlistBtn').click(); });
document.getElementById('backToModlistsBtn').addEventListener('click', backToModlistList);
document.getElementById('applyModlistBtn').addEventListener('click', () => api('apply_modlist', currentModlistDetail));
document.getElementById('modlistSearchInput').addEventListener('input', renderModlistDetail);
document.getElementById('modlistAddModsBtn').addEventListener('click', openAddModsModal);
document.getElementById('modlistClearAllBtn').addEventListener('click', () => api('set_modlist_all', currentModlistDetail, false));
document.getElementById('addModsDoneBtn').addEventListener('click', closeAddModsModal);
document.getElementById('addModsModal').addEventListener('click', e => { if (e.target.id === 'addModsModal') closeAddModsModal(); });
document.getElementById('addModsSearchInput').addEventListener('input', renderAddModsModal);
document.getElementById('addModsAddAllBtn').addEventListener('click', () => api('set_modlist_all', currentModlistDetail, true));

document.getElementById('modalDoneBtn').addEventListener('click', closeModal);
document.getElementById('modlistModal').addEventListener('click', e => { if (e.target.id === 'modlistModal') closeModal(); });
document.getElementById('modalCreateBtn').addEventListener('click', async () => {
  const input = document.getElementById('modalNewModlistInput');
  const name = input.value.trim();
  if (!name) return;
  await api('create_modlist', name);
  await api('add_to_modlist', modalItemId, name);
  input.value = '';
});

document.getElementById('confirmRemoveToggle').addEventListener('change', e => api('set_confirm_remove', e.target.checked));
document.getElementById('renameMigrationBtn').addEventListener('click', () => {
  if (!confirm('This will rename addon folders on disk and update gameinfo.txt to remove Workshop IDs from their names. Continue?')) return;
  api('run_rename_migration');
});
document.getElementById('rescanBtn').addEventListener('click', () => api('rescan_installed_addons'));
document.getElementById('githubBtn').addEventListener('click', () => api('open_github'));
document.getElementById('updateBannerDismissBtn').addEventListener('click', () => {
  document.getElementById('updateBanner').classList.remove('show');
});

switchTab('mods');

(async () => {
  pollEvents();
  const bootstrap = await api('get_bootstrap');
  applyState(bootstrap);
  document.body.classList.add('loaded');
  refreshPlayerCount();
  checkForUpdate();
  setInterval(refreshPlayerCount, 5 * 60 * 1000);
  api('load_addon_stats');
  api('load_previews');
})();
</script>
</body>
</html>
"""


def main():
    global API, WINDOW
    API = Api()
    port = start_ui_server()
    # url= (not html=) and NO js_api: pywebview is now only a browser frame.
    WINDOW = webview.create_window(
        "L4D2 Workshop Addon Manager",
        url=f"http://127.0.0.1:{port}/",
        width=900,
        height=780,
        min_size=(700, 600),
        background_color="#1E1E22",
    )
    webview.start(gui="edgechromium", debug=False)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("This tool automates SteamCMD + gameinfo.txt editing and is built for Windows,")
        print("which is where L4D2 and its addons folder normally live. It may not work as-is")
        print("on other platforms.")
    main()

