# Validation — 2026-09-07

Tested on Linux x86-64 with **Wine 11.16 (Staging)**, Python 3.14, Tk 8.6,
and a Wayland desktop through XWayland. The helper uses the system Wine build.
This records observed results on this machine, not a compatibility guarantee
for every distribution or future client release.

## Actual installation

`./msw install` completed with a fresh default Wine prefix at
`~/.local/share/msw-helper-wine/prefix`. The official WebView2 offline download
was cached beforehand; there was no preinstalled Wine runtime, NGM, or game in
that prefix. The helper automatically initialized Wine, installed WebView2,
downloaded Nexon's launcher components, installed Worlds, verified the real
client, and registered the desktop application and URL handler.

A separate installation in the repository's ignored `runtime/` directory also
completed. Publisher payloads remain unmodified and are excluded from Git.

The resulting game directory is approximately **1.9 GiB**:

```text
prefix/drive_c/Nexon/MapleStory Worlds/
  msw.exe
  UnityPlayer.dll
  GameAssembly.dll
  msw_Data/
  obd-manifest-106603
```

Observed core binaries, all Windows PE x86-64:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `msw.exe` | 688096 | `b1b896d8e032a274c285948a7d8e742d760e3cf4cdf8e7137e5626731c07ef94` |
| `UnityPlayer.dll` | 31006120 | `43fb0aea1b80c74963dae88ec7fc1c3c1993d893ce94d12972000fcc9b653ad1` |
| `GameAssembly.dll` | 191046112 | `932584681170cdf0520d96464996820811f8baa58556877f5350c8c10f79a849` |

The client logged `MOD.Version: win.26.7.1.722` and Unity `2022.3.62f2`.
These hashes are observations, not pinned release requirements; updates may
legitimately replace them.

## Repair and updating

1. Closed the client started by the test installation.
2. Backed up and removed `msw_Data/boot.config`.
3. Confirmed that the helper reported the client as incomplete.
4. Ran the helper's real update operation against Nexon's servers.
5. Confirmed that NGM restored the file **byte for byte**, updated its manifest,
   and opened `msw.exe`. The helper returned success and recorded the verified
   manifest hash in `state.json`.

Restored `boot.config` SHA-256:
`4e65165bd2841c99e40610af0b54ba8e925620eeb44bb3d01050b2f05d9e92ce`.

The helper uses Nexon's current metadata and patcher. Downloading a future game
release cannot be demonstrated before Nexon publishes it; the live repair path
and component revision/cache invalidation were tested.

## Desktop and GUI

- `xdg-mime query default x-scheme-handler/ngm` returned
  `msw-helper-wine-ngm.desktop`.
- Dispatched a current-format, unauthenticated `ngm://launch/` URL with `gio open`.
  The registered desktop handler invoked NGM and started the installed `msw.exe`.
- Exercised GLib's actual desktop-file parsing with spaces, percent signs,
  quotes, dollar signs, backticks, and backslashes in paths. The recorder
  received the complete original URL as exactly one argument, including its
  spaces, plus signs, and percent encoding.
- Ran the real Tk GUI: verified button callbacks, worker-to-GUI progress,
  completion, enabled repair for incomplete files, and a visible cancel button.
  [GUI screenshot](helper.png) shows the installed state.
- Observed the actual Worlds window render its logo, navigation, avatar, and
  main interface under Wine.

The automated test launches did not carry a Nexon account ticket. The user later
reported successfully entering a world and playing during the initial testing,
followed by a world-entry regression and a successful retest described below.
The latest confirmation comes from the user's actual world entry; automated
file, GUI, and protocol checks alone do not establish in-world gameplay.

## Vuplex crash correction

The initial WineD3D configuration rendered the game's main window but crashed
the separate `Vuplex WebView.vuplex` browser. The user's report led to a separate
reproduction and a native crash dump. Vuplex's `log-chromium.txt` recorded:

```text
ID3D11Device::OpenSharedResource() failed with HRESULT error code: -2147467263
```

That is `E_NOTIMPL` (`0x80004001`). The browser then dereferenced the missing
texture at `Vuplex WebView.vuplex + 0x70cc5`. Unity also logged a
`NullReferenceException` from `Vuplex.WebView.Internal.BaseWebView.HandleInitFinished`.

DXVK 2.7.1 still failed, reporting a missing shared-resource handle. **DXVK 3.1**
resolved the reproduced failure on Wine 11.16 with the NVIDIA 610.57.04 driver:
the browser and its child processes remained running, with no new shared-texture
errors or Unity WebView exception. The Friends/Chat panel rendered successfully.
The fix uses the shared-resource
support introduced for upstream Wine in [DXVK 3.0](https://github.com/doitsujin/dxvk/releases/tag/v3.0).
The helper now installs the verified 3.1 DLLs before every NGM launch when needed.
Normal Wine crash reporting remains enabled in the shipped configuration.

A separate late crash was captured in Chromium 100's `cef_shutdown` call after
the original graphics failure was fixed. Vuplex was then scoped to Windows 7
compatibility mode, leaving Worlds on Windows 10. The follow-up test rendered
successfully and the browser exited without a new shutdown dump. This is a
tested compatibility setting, not a guarantee against every Chromium failure.

The DXVK 3.1 archive digest was checked against its official GitHub release asset:
`30f9cc326874be344285582275446968cfa4c069db31ce56df312d6644179154`.
The current active user session must be closed and launched again to migrate
from the initial WineD3D installation; the helper refuses to change its graphics
mid-session during normal launch handling.

## World-entry regression: security error 201

The user reported that a world had been playable during testing, but subsequent
entry failed with a security-module reset error, code 201. The cause has not yet
been isolated; this is not evidence that Wine can never run the game, nor proof
that DXVK caused the regression.

The audit found background Wine services left in both development prefixes and
an unnecessarily broad graphics override. The helper now enables DXVK only for
`msw.exe` and `Vuplex WebView.vuplex`; other processes use Wine's builtin graphics.
All helper-created test-prefix services were stopped, and the idle main prefix
was prepared for a clean launch. The helper's primary Play Worlds button and
`./msw play` now launch the desktop client through NGM.

Nexon's official NGS installer was also run using the game metadata's arguments
(`install -download -gid 2037 -name MapleStoryWorlds`). The installed `NGService.exe`
was unchanged. A direct service-start diagnostic observed exception `0xc0000409`
and service exit 1067 in the main prefix, and startup error 1053 in the test prefix.
Those standalone probes do not reproduce a complete authenticated world-entry
handshake, so they do not establish the cause of error 201. NGS's own log files
are encoded and were not interpreted as plaintext error reports.

Security service SHA-256 observed before and after the installer:
`1f067171a7e0a4ac864b8f8a63e6f30a5833187bd7fdb839da729b7ae670e3a4`.

On 2026-09-07, after the cleanup and desktop launch changes, the user confirmed
successful world entry again. Their screenshot shows **Old School Maple** at
its world/channel selection screen. Error 201 did not block this attempt.

This establishes a successful retest with the current configuration. Because
the service cleanup, scoped graphics overrides, official NGS installer run, and
fresh launch preceded that retest, it does not isolate which change cleared the
failure or establish reliability across repeated launches.

## Repeatable checks

The session cleanup patch adds **Stop Worlds** / `./msw stop` and a detached
watcher that cleans up the dedicated prefix after the game exits. The current
59 unit tests pass, including background-browser cleanup, active-game protection,
operation locking, ownership checks, graceful stop and stuck-process fallback,
unreadable process environments, retries after Wine errors, and refusing to
follow a replacement Wine server. The desktop smoke test and
real Tk smoke test also pass; the Tk check exercises the Stop Worlds button
with a mocked backend.

The user's follow-up about hanging after quitting the game exposed a live
watcher failure: `session.log` recorded `Cannot inspect Wine processes for
automatic cleanup.` Same-user desktop services, including `systemd` and
`ssh-agent`, prohibit reading their process environments. The watcher now skips
those unrelated processes and still confirms the active apps through Wine
before stopping the dedicated prefix. Transient Wine-check and shutdown errors
are retried. Explicit Stop Worlds also proceeds to prefix shutdown if its
graceful close command fails or times out, and uses native polling while the
game exits instead of repeatedly starting Wine's `tasklist`.

The corrected watcher stopped all 11 processes left in the actual closed
Worlds session, including Wine services, Edge's updater, and its crash handler.
A fresh actual game launch followed by a normal window-close request completed
with zero remaining prefix processes after 10.2 seconds. A second test through
the updated installed helper reached the game's own Quit / Cancel dialog;
the initial close request alone did not exit it. The window subsequently closed
before the automated confirmation click, and the installed watcher again
logged successful cleanup with zero remaining prefix processes. This verifies
desktop client cleanup on this machine; it does not establish shutdown
reliability from every authenticated world.

```sh
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 tests/gui_smoke.py
PYTHONPATH=. python3 tests/desktop_smoke.py
./msw verify
```

The 40 unit tests and both desktop/GUI smoke tests passed locally. The launch
control change was checked with the real Tk smoke test: Play Worlds dispatches
the client launch on its worker, while Website opens the browser separately.
`./msw play` was also run against the main installation and opened the actual
MapleStory Worlds window. That check does not establish successful world entry.
The tests do not need a Nexon account or download game content. `verify` is a
basic core-file/completion check; NGM's restore operation performs content repair.

## Installer, window tracking, and uninstall

The existing per-user installer now records installation ownership and installs
three desktop templates from `msw_helper/resources/`, plus the Worlds PNG icon:
the primary game entry, a separate helper GUI entry, and the hidden protocol
handler. `desktop-state.json` records installed file hashes and the previous
protocol handler. The installed copy includes the resources and can run after
the checkout moves.

The running **MapleStory Worlds-Old School Maple** window was inspected with
`xprop` and reported `WM_CLASS(STRING) = "msw.exe", "msw.exe"`, matching the game
entry's `StartupWMClass`. The helper uses Tk class `Msw-helper-wine`, matching its
separate entry. Actual installed desktop files passed `desktop-file-validate`.
The icon is referenced by absolute path because this machine's existing shared
icon cache failed validation when rebuilt; installing the helper does not
require modifying that cache.

The GUI-only entry is **MapleStory Worlds Helper**. Its desktop icon and the
GUI's `_NET_WM_ICON` use the original 256×256 application artwork extracted
from `msw.exe`, replacing the initial placeholder. Re-registration removes the
old SVG only when it still matches the recorded installation hash.

`tests/desktop_smoke.py` registered a disposable installation with real XDG
tools, dispatched each generated entry with GLib, and verified game/helper
commands and unchanged protocol arguments. It then ran the **installed helper's
own uninstall command**, removed the disposable files and desktop entries, and
confirmed that the previous protocol handler was restored. The real game
installation was retained.

Unit tests cover modified desktop files, a newer prefix taking over the menu
entries, later user changes to the default handler, missing desktop files,
unrelated files, Wine drive symlinks, ownership validation, and refusal to
remove an active client. Wine shutdown is checked for the matching wineserver
and the dedicated `WINEPREFIX`; full real-game removal was not performed.
The Tk smoke test verifies both declining and accepting uninstall with a mocked
backend, disables cancellation during removal, and closes the GUI on success.

The readiness review also checked ownership before installation and desktop
registration. Existing untracked prefix, cache, log, or helper directories now
cause an actionable error instead of silently becoming uninstall targets.
Recorded legacy installs can still migrate. Wine settings takes the operation
lock and records ownership before creating a prefix, including before the game
has been installed. EOF at the CLI uninstall prompt cancels removal.

Further testing can cover repeated clean launches into a world after the
error-201 recovery and a full installation/update/removal cycle on another
machine. The results above describe the configuration tested locally.
