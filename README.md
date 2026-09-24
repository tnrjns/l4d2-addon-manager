# L4D2 Addon Manager

Install Left 4 Dead 2 Workshop addons and **collections** locally by pasting
a link. No subscribing, no sketchy downloader sites, no manually renaming
`.vpk` files or hand-editing `gameinfo.txt`.

Paste a Workshop URL → click Install → done.

*Made by Tanner.*

---

## Why?

Normally, installing a Workshop addon as a local mod means:

1. Find a third-party Workshop downloader site
2. Download the file
3. Rename the `.vpk` to `pak01_dir.vpk`
4. Make a folder for it in your L4D2 directory
5. Open `gameinfo.txt` and add a `SearchPaths` entry by hand
6. Repeat forever, for every addon, and hope you remember which folder was which

This app does all of that for you — including whole **collections** — and
names each folder after the addon's real Workshop title, with its preview
image, so you can actually tell them apart later.

## Features

- **Paste & install** — give it any Workshop URL or ID
- **Whole collections in one paste** — it detects a collection link
  automatically, installs everything inside it (skipping what you already
  have), and drops the result into a modlist named after the collection
- **Live download progress** — real megabyte and percentage readout while
  an addon downloads, not just a spinner
- **Auto-names folders and shows preview images** using each addon's real
  Workshop title and picture
- **Enable / disable** any addon — or several at once — without deleting
  files, so switching mods on and off is instant
- **Modlists** — group addons into named loadouts (e.g. "Competitive",
  "Graphical Singleplayer") and apply one to enable exactly those and
  disable everything else
- **Mass-select** — drag across addons, or Ctrl/Shift-click, to enable,
  disable, remove, or add several to a modlist at once
- **Search and sort** your installed addons (by name, size, date installed,
  or enabled status)
- **Launches L4D2 through Steam**, keeping the session VAC-secure, with
  custom launch options remembered between runs
- Seven built-in themes, and an in-app notice when a newer version is
  available on GitHub
- Automatic one-time backup of your original `gameinfo.txt`
- Handles single `.vpk`, multi-part `.vpk`, and loose-file addons
- A **Rescan** tool that recovers your addon list (including preview
  images) directly from disk and `gameinfo.txt`, in case `config.json`
  ever goes missing
- Downloads SteamCMD for you on first run

## Install (for most people)

1. Go to the [**Releases**](../../releases) page
2. Download `L4D2AddonManager.exe` from the latest release
3. Run it — that's it. No Python, no dependencies, nothing to set up.

> **Windows SmartScreen warning:** because the exe isn't code-signed
> (signing certificates cost money), Windows may show a "Windows protected
> your PC" screen the first time. Click **More info → Run anyway**. The
> full source is in this repo if you'd rather build it yourself.
>
> **WebView2:** the app's interface renders through Microsoft Edge
> WebView2, which is pre-installed on virtually all up-to-date Windows 10/11
> machines. If it's somehow missing, Windows will prompt you to install the
> small WebView2 Runtime automatically the first time you run the app.

On first launch it'll try to auto-detect your L4D2 folder. If it guesses
wrong, click **Browse** and point it at the folder containing
`left4dead2.exe` — usually:

```
C:\Program Files (x86)\Steam\steamapps\common\Left 4 Dead 2
```

The app will let you know right in its own window if a newer version is
available on GitHub, with a one-click link straight to it.

## Install on Linux

There's no compiled Linux binary — GTK/WebKit2 doesn't bundle reliably
across distros, so it ships as source instead, via one of two ways:

**Option A — download the release bundle**

1. Go to the [**Releases**](../../releases) page
2. Download `L4D2AddonManager-linux.tar.gz` from the latest release
3. Extract it, then:

```bash
cd L4D2AddonManager-linux
chmod +x run_linux.sh
./run_linux.sh
```

**Option B — clone the repo** (if you want to track updates via git)

```bash
git clone https://github.com/tnrjns/l4d2-addon-manager.git
cd l4d2-addon-manager
chmod +x run_linux.sh
./run_linux.sh
```

`run_linux.sh` installs `pywebview` and whichever GTK/WebKit2 packages your
distro needs (detects `apt` or `pacman` automatically), then launches the
app. If you'd rather do it by hand, or you're not on apt/pacman:

```bash
pip install pywebview

# Ubuntu / Debian / Mint / Pop!_OS:
sudo apt install gir1.2-gtk-3.0 gir1.2-webkit2-4.1

# Arch / CachyOS / Manjaro / EndeavourOS:
sudo pacman -S python-gobject webkit2gtk-4.1

python3 l4d2_addon_manager.py
```

If GTK doesn't work for your setup, there's a Qt-based fallback:

```bash
pip install PyQt5 PyQtWebEngine qtpy
```

**Wayland note:** on compositors like Hyprland or Sway, WebKitGTK
occasionally renders oddly or refuses to open. If that happens, try forcing
XWayland instead:

```bash
GDK_BACKEND=x11 python3 l4d2_addon_manager.py
```

Everything else — installing addons, modlists, themes, launching the
game — works the same as on Windows. Linux support is newer than Windows
support and less battle-tested; if SteamCMD or the game launch does
something unexpected, the in-app log shows exactly what command ran and
what came back, which is the most useful thing to include when reporting
an issue.

## Usage

1. Find an addon (or collection) on the Steam Workshop in your browser
2. Copy the page URL (e.g. `https://steamcommunity.com/sharedfiles/filedetails/?id=123456789`)
3. Paste it into the app (there's a **Paste** button) and hit **Install**

The first install also downloads SteamCMD (~20MB, one time, automatic).

### Where files go

Each addon becomes its own folder next to `left4dead2.exe`:

```
Left 4 Dead 2\
├── left4dead2.exe
├── Some Cool Addon\
│   └── pak01_dir.vpk
└── left4dead2\
    └── gameinfo.txt        ← a SearchPaths line is added here
```

The matching `gameinfo.txt` entry looks like:

```
SearchPaths
{
    Game            "Some Cool Addon"
    Game            update
    ...
}
```

Your original `gameinfo.txt` is backed up to `gameinfo.txt.bak` the first
time the app edits it.

## Running from source

If you'd rather not use the prebuilt exe:

```bash
git clone https://github.com/tnrjns/l4d2-addon-manager.git
cd l4d2-addon-manager
pip install -r requirements.txt
python l4d2_addon_manager.py
```

Requires Python 3.8+ on Windows.

## Building the exe yourself

Double-click `build_exe.bat`, or run:

```bash
pip install -r requirements.txt pyinstaller
python -m PyInstaller --onefile --windowed --name "L4D2AddonManager" --collect-all webview --collect-all clr_loader --collect-all pythonnet l4d2_addon_manager.py
```

The result lands in `dist\L4D2AddonManager.exe`.

## Publishing a new release

The repo includes a GitHub Actions workflow that, on every version tag,
builds the Windows exe and packages a separate Linux bundle
(`L4D2AddonManager-linux.tar.gz` — the script, `run_linux.sh`, and a short
install note), then attaches **both** to the same release as distinct
downloads. The app also checks this same GitHub Releases feed on launch
and shows an in-app banner if a newer version exists, so tagging a release
is what actually notifies users.

To cut a release:

```bash
git tag v2.2.1
git push origin v2.2.1
```

Actions will build it and publish a release with both the Windows exe and
the Linux bundle attached. You can also trigger a build manually from the
**Actions** tab without tagging.

Or just run `release.bat`, which commits, pushes, tags, and pushes the tag
for you — it'll ask for the version number.

## Notes & limitations

- **Windows is the primary platform.** Linux is supported (see [Install
  on Linux](#install-on-linux) above) but newer and less tested — no
  prebuilt binary yet, and Windows-specific things like WebView2 don't
  apply there.
- Uses SteamCMD's **anonymous login**, which works for public Workshop
  items. Private or restricted items may fail — the in-app log will show
  SteamCMD's actual error.
- Some older Workshop uploads are stored by Steam without a `.vpk`
  extension. The app detects this and installs them correctly anyway.
- Close L4D2 before installing, removing, or toggling addons.
- This installs addons as **local mods** via `gameinfo.txt`, which is
  separate from Steam's own Workshop subscription system. That's the point —
  it's what lets these work offline and persist without subscribing.

## Contributing

Issues and PRs welcome. If you're reporting a bug, the **Copy log** button
in the app grabs the full log, which usually shows exactly what went wrong.

## License

MIT — see [LICENSE](LICENSE).
