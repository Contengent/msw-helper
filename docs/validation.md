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
followed by a world-entry regression described below. Automated file, GUI, and
protocol checks do not establish a working security session or in-world gameplay.

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
was restarted for a clean, single-instance website launch.

Nexon's official NGS installer was also run using the game metadata's arguments
(`install -download -gid 2037 -name MapleStoryWorlds`). The installed `NGService.exe`
was unchanged. A direct service-start diagnostic observed exception `0xc0000409`
and service exit 1067 in the main prefix, and startup error 1053 in the test prefix.
Those standalone probes do not reproduce a complete authenticated world-entry
handshake, so they do not establish the cause of error 201. NGS's own log files
are encoded and were not interpreted as plaintext error reports.

Security service SHA-256 observed before and after the installer:
`1f067171a7e0a4ac864b8f8a63e6f30a5833187bd7fdb839da729b7ae670e3a4`.

The same-world retest after cleanup remains pending. Do not describe this
regression as fixed until a world can be entered again.

## Repeatable checks

```sh
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 tests/gui_smoke.py
PYTHONPATH=. python3 tests/desktop_smoke.py
./msw verify
```

The 20 unit tests and both desktop/GUI smoke tests passed locally. GitHub Actions
is configured to run them with Python 3.11 and Xvfb; remote CI has not been run.
The tests do not need a Nexon account or download game content. `verify` is a
basic core-file/completion check; NGM's restore operation performs content repair.
