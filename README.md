# MapleStory Worlds Wine Helper

Install the **actual MapleStory Worlds Windows client**, update/repair it with
Nexon's patcher, and open website `ngm://` links in the same Wine prefix. Includes
a Python/Tk desktop GUI and a command line interface.

The installer downloads Nexon's unmodified game files. A completed installation
contains `msw.exe`, `UnityPlayer.dll`, `GameAssembly.dll`, `msw_Data/`, and the Nexon
patch manifest. Installing only Nexon Game Manager never counts as success.

![The installed helper GUI](docs/helper.png)

## Quick start

Requirements: Linux x86-64, Python **3.11+**, Python Tk, Wine with 64-bit Windows
and 32-bit installer support, a graphical desktop, and `xdg-utils`. Allow at least
6 GiB free for installation. Graphics need a current Vulkan driver compatible
with [DXVK 3.x](https://github.com/doitsujin/dxvk/wiki/Driver-support) (normally
Vulkan 1.4). A current Wine Staging build is a suitable starting
point; see [validation](docs/validation.md) for the version actually tested.

```sh
# Open the helper GUI; Play Worlds launches the desktop client
./msw

# Launch the Worlds desktop client directly
./msw play

# Or install everything, including desktop integration
./install.sh

# Check installed files
./msw verify

# Check for updates and repair files (Nexon opens the client afterward)
./msw update

# Optional: open the official website in your normal Linux browser
./msw website

# Preview removal, or uninstall with confirmation
./msw uninstall --dry-run
./uninstall.sh
```

Tk is packaged as `tk` on Arch Linux and `python3-tk` on Debian/Ubuntu. Wine and
desktop dependencies should be installed using your distribution's package
manager. The helper uses your installed Wine and does not change system packages.

The GUI's primary **Play Worlds** button and `./msw play` launch the installed
desktop client through Nexon Game Manager. Use **Website** or `./msw website`
when you want to browse the website instead.

## Website launch

Installation adds **MapleStory Worlds (Wine)** for direct game launch and
**MapleStory Worlds Helper** for installation management to your application
menu. It also makes the helper the handler for `ngm://` links. Sign in on the
[official Worlds website](https://maplestoryworlds.nexon.com/en/play), choose a
world, and allow the browser to open the external application.

The complete URL is passed as **one argument**, without decoding/re-encoding it,
splitting it at spaces, or sending it through a shell. Nexon handles the session
ticket, patching, and world selection. Login stays in your browser. Expired login
links must be generated again by clicking Play on the website.

```sh
# Re-register after moving/updating the helper, or changing Wine/data directories
./msw register-desktop

# Inspect the desktop association
xdg-mime query default x-scheme-handler/ngm
# Expected: msw-helper-wine-ngm.desktop
```

The `ngm` scheme is shared by Nexon games. This helper claims that desktop
association and accepts Worlds game code `106603`; it rejects other Nexon games.
If your browser continues to show Nexon's installer prompt after opening the
client, check its external-application and local-network permission settings.

## Paths and options

Default data directory: `$XDG_DATA_HOME/msw-helper-wine`, normally
`~/.local/share/msw-helper-wine`. `MSW_HOME` overrides it. It contains:

| Path | Contents |
| --- | --- |
| `prefix/` | Dedicated Wine prefix |
| `prefix/drive_c/Nexon/MapleStory Worlds/` | Actual Worlds binaries and game data |
| `cache/` | Publisher downloads, reusable after an interrupted install |
| `logs/helper.log` | Wine/NGM output with launch arguments redacted |
| `logs/webview-bootstrap.log` | Microsoft runtime bootstrap diagnostics |
| `state.json` | Last verified operation and installed patch-manifest hash |
| `dxvk.json` | Installed graphics version and DLL integrity hashes |
| `installation.json` | Ownership record for safe removal of the dedicated installation |
| `desktop-state.json` | Installed desktop/icon paths and hashes, plus the previous `ngm://` handler |
| `helper/` | Copy used by desktop entries, independent of the checkout location |

```sh
# Use another Wine build and/or data directory consistently for every command
./msw --wine /path/to/wine --data-dir "$HOME/Games/Worlds" install
./msw --wine /path/to/wine --data-dir "$HOME/Games/Worlds" gui

# Repository-local installation, useful for development
./msw --data-dir "$PWD/runtime" install --no-desktop

./msw doctor
./msw winecfg
./msw --help
```

`MSW_WINE` selects a Wine executable. The helper deliberately ignores a foreign
`WINEPREFIX` and `WINEARCH`; it consistently uses its own prefix. It preserves
your display and audio environment. On desktops with `DISPLAY`, Wine uses X11
(or XWayland). The helper installs **DXVK 3.1** for Direct3D 11 and DXGI in this
prefix, enabled specifically for `msw.exe` and `Vuplex WebView.vuplex`. Nexon's
launcher and security processes retain Wine's builtin graphics configuration.
The archive is pinned to the SHA-256 published with the upstream release;
installed DLLs are also checked before launching. DXVK logs go in `logs/`.
The bundled Vuplex browser uses Wine's Windows 7 compatibility mode; the game
continues to use Windows 10.

Choose an empty data directory for a new installation. The helper refuses to
claim pre-existing prefix/cache/log/helper directories without its ownership
record; this prevents a later uninstall from deleting unrelated data. Recorded
installations from earlier helper versions are migrated automatically.

## Installation and updates

1. Initialize a Wine prefix, suppressing optional Mono/Gecko first-run prompts.
2. Install Microsoft's official x64 WebView2 runtime. When the Edge updater
   stalls, stage its official inner installer, validate it against the bundled
   Microsoft SHA-256 manifest, and run it directly. Set the WebView2 process to
   Wine's Windows 7 compatibility mode while the game uses Windows 10.
3. Fetch Nexon's current NGM component metadata and bootstrap the 64-bit launcher.
   Legacy HTTP URLs are upgraded to HTTPS. Nexon's `crc` values identify component
   revisions; cached files are additionally checked with local SHA-256 hashes.
4. Ask NGM to install Worlds (`106603@1585`). NGM owns content download, patching,
   and its required game dependencies. Before invoking NGM, the helper installs
   DXVK 3.1 so Worlds and its Vuplex browser can share Direct3D textures.
5. Check the real client files and patch manifest, then repair the executable
   registration under `HKCU\Software\Nexon\MOD` and install desktop integration.

`update` uses NGM's **restore** flow, which checks/repairs game content and opens
the client afterward. Play Worlds and website launches also use NGM so Nexon can
patch before launching. `verify` checks the core binaries/data and completion
manifest; it is not a replacement for Nexon's full content verification.

Close Worlds and Nexon's launcher before installing or updating. A per-prefix
lock prevents concurrent helper operations. Downloads use temporary files and
atomic replacement. Failed operations do not write a success state. Cancellation
stops the helper's current process; Nexon may already have spawned a separate
window. Close that window before retrying. The helper never kills an entire Wine
server or a newly launched game during normal operation.

## Desktop integration and removal

`./install.sh` is a per-user installer: it installs the actual game and a stable
copy of the helper, then creates the menu entries, icon, and website handler.
It requires no root access. Existing installations get the new entries and
uninstaller with `./msw register-desktop`.

The desktop templates and original Worlds application icon live in
`msw_helper/resources/`. The GUI-only launcher is **MapleStory Worlds Helper**;
its icon and the helper window icon use the artwork from Nexon's `msw.exe`.
Generated entries are installed under `$XDG_DATA_HOME/applications`, normally
`~/.local/share/applications`:

| Desktop file | Action | Window class |
| --- | --- | --- |
| `msw-helper-wine.desktop` | Launch Worlds; includes a Manage installation action | `msw.exe` |
| `msw-helper-wine-manager.desktop` | Open the helper GUI | `Msw-helper-wine` |
| `msw-helper-wine-ngm.desktop` | Handle website links; hidden from menus | — |

The game and helper entries set
[`StartupWMClass`](https://specifications.freedesktop.org/desktop-entry/latest/recognized-keys.html)
so the desktop can associate each running window with its launcher. The icon
uses its installed absolute path and does not depend on a shared icon cache.

Use **Uninstall…** in the helper GUI, `./uninstall.sh`, or `./msw uninstall`.
The CLI shows the paths and asks for confirmation; `--dry-run` only lists them,
and `--yes` supports unattended removal. Uninstall removes the dedicated Wine
prefix, including game files and local settings, cached downloads, logs, and the
installed helper copy. Close Worlds and Nexon's launcher first; the uninstaller
then stops background services only in this prefix.

Desktop files and the icon are removed only when they still match the recorded
installation. Modified entries and another prefix's newer registration are
preserved. The helper removes its own MIME association tokens and restores the
previous `ngm://` handler when available, unless you have since selected another
default. Unrelated files in the data directory and its small operation lock
remain. The source checkout and system Wine installation are retained.

## Troubleshooting

- **Security-module reset error 201 when entering a world:** close every Worlds
  instance, including development/test copies, and launch again with Play Worlds.
  This is a separate world-entry check; passing `verify` does not validate the
  security session. World entry into Old School Maple succeeded in the user's
  retest on 2026-09-07; the specific cause remains unisolated. The changes and
  successful retest are recorded in
  [validation](docs/validation.md#world-entry-regression-security-error-201).
- **Repeated “Vuplex WebView.vuplex” / Wine Debugger windows:** close Worlds and
  launch it again with Play Worlds using the updated helper, or run Update /
  repair. The handler automatically installs DXVK 3.1 before launching. WineD3D
  fails Vuplex's `OpenSharedResource` call, and older DXVK 2.x builds do not support
  the current upstream Wine sharing path. This fixes the reproduced graphics
  failure; the helper does not disable Wine's crash reporting.
- **Missing game files:** rerun Install, or use Update / repair for an existing
  game directory. Do not use NGM's presence as proof the game is installed.
- **White/blank Nexon window:** check `./msw doctor` for WebView2, retry Install,
  and use a current Wine build. The helper supplies WebView2's Wine compatibility
  setting and disables GPU rendering for that embedded browser only.
- **Website asks to install NGM:** rerun `register-desktop`, check the browser's
  permission to open external apps, and click Play again after setup completes.
- **Expired session/empty client:** sign in on the website and launch a world
  there. A client started by repair has no authenticated website ticket.
- **Failure diagnostics:** `./msw doctor` prints paths without storing website
  tickets. The helper log redacts launch arguments; Nexon's own logs inside the
  prefix can contain them, so remove them before sharing those logs.

The project targets the current global Worlds Windows launch flow. Wine support
and authenticated gameplay depend on Nexon's current client/service. The tested
and untested parts are recorded in [docs/validation.md](docs/validation.md).

## Development

No pip dependencies are required.

```sh
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 tests/gui_smoke.py
PYTHONPATH=. python3 tests/desktop_smoke.py  # requires gio + desktop-file-validate
# In a headless CI environment:
PYTHONPATH=. xvfb-run -a python3 tests/gui_smoke.py
```

The tests cover incomplete installs, repair, failed update detection, download
truncation and checksums, graphics migration, locking, protocol preservation,
token redaction, real Tk controls, desktop registration, and uninstall ownership
and association cleanup. The desktop smoke test installs and removes a disposable
helper copy using actual XDG tools. Tests do not download a game or require a
Nexon account.

Inspired by the approach in the author's `pso_wine` project. Publisher endpoints
and launch parameters were checked against the
[Worlds website](https://maplestoryworlds.nexon.com/en/play),
[Nexon NGM protocol implementation](https://platform.nexon.com/ngm/js/npf_ngm.js),
[Worlds game metadata](https://ngmapi.nexon.com/game-info/106603), and
[NGM component metadata](https://platform.nexon.com/NGM/Json/NGMUpdateInfo.json).
Microsoft documents the [WebView2 distribution flow](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution).

Helper source is licensed under GPL-3.0-or-later. Nexon game files, Microsoft
runtimes, and other downloaded components remain under their respective licenses
and are excluded from this Git repository.
