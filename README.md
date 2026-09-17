# L4D2 Addon Manager

Install Left 4 Dead 2 Workshop addons **locally** by pasting a link. No
subscribing, no sketchy downloader sites, no manually renaming `.vpk` files
or hand-editing `gameinfo.txt`.

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
6. Repeat forever, and hope you remember which folder was which

This app does all of that for you, and names each folder after the addon's
real Workshop title so you can actually tell them apart later.

## Features

- **Paste & install** — give it any Workshop URL or ID
- **Auto-names folders** using the addon's real title from Steam
- **Enable / disable** addons with a switch (temporarily removes the
  `gameinfo.txt` line; files stay on disk, so toggling back on is instant)
- **Enable All / Disable All** in one click
- **Search** your installed addons
- **Launch L4D2** directly, with custom launch options that get remembered
- **Workshop page** button on every addon, so you can jump back to its listing
- Automatic one-time backup of your original `gameinfo.txt`
- Handles single `.vpk`, multi-part `.vpk`, and loose-file addons
- Downloads SteamCMD for you on first run

## Install (for most people)

1. Go to the [**Releases**](../../releases) page
2. Download `L4D2AddonManager.exe` from the latest release
3. Run it — that's it. No Python, no dependencies, nothing to set up.

> **Windows SmartScreen warning:** because the exe isn't code-signed
> (signing certificates cost money), Windows may show a "Windows protected
> your PC" screen the first time. Click **More info → Run anyway**. The
> full source is in this repo if you'd rather build it yourself.

On first launch it'll try to auto-detect your L4D2 folder. If it guesses
wrong, click **Browse** and point it at the folder containing
`left4dead2.exe` — usually:

```
C:\Program Files (x86)\Steam\steamapps\common\Left 4 Dead 2
```

## Usage

1. Find an addon on the Steam Workshop in your browser
2. Copy the page URL (e.g. `https://steamcommunity.com/sharedfiles/filedetails/?id=123456789`)
3. Paste it into the app (there's a **Paste** button) and hit **Install**

The first install also downloads SteamCMD (~20MB, one time, automatic).

### Where files go

Each addon becomes its own folder next to `left4dead2.exe`:

```
Left 4 Dead 2\
├── left4dead2.exe
├── Some Cool Addon (123456789)\
│   └── pak01_dir.vpk
└── left4dead2\
    └── gameinfo.txt        ← a SearchPaths line is added here
```

The matching `gameinfo.txt` entry looks like:

```
SearchPaths
{
    Game            "Some Cool Addon (123456789)"
    Game            update
    ...
}
```

Your original `gameinfo.txt` is backed up to `gameinfo.txt.bak` the first
time the app edits it.

## Running from source

If you'd rather not use the prebuilt exe:

```bash
git clone https://github.com/YOUR_USERNAME/l4d2-addon-manager.git
cd l4d2-addon-manager
pip install -r requirements.txt
python l4d2_addon_manager.py
```

Requires Python 3.8+ on Windows.

## Building the exe yourself

Double-click `build_exe.bat`, or run:

```bash
pip install -r requirements.txt pyinstaller
python -m PyInstaller --onefile --windowed --name "L4D2AddonManager" --collect-all customtkinter l4d2_addon_manager.py
```

The result lands in `dist\L4D2AddonManager.exe`.

## Publishing a new release

The repo includes a GitHub Actions workflow that builds the exe on a Windows
runner and attaches it to a release automatically. To cut a release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Actions will build it and publish a release with the exe attached. You can
also trigger a build manually from the **Actions** tab without tagging.

## Notes & limitations

- **Windows only.** The app automates Windows paths and launches
  `left4dead2.exe` directly.
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
