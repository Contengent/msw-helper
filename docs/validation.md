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
  (That Tk GUI has since been replaced by the GTK launcher; see
  [Account launcher](#account-launcher-2026-11-23).)
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
resolved the reproduced failure on Wine 11.16 with an NVIDIA driver:
the browser and its child processes remained running, with no new shared-texture
errors or Unity WebView exception. The Friends/Chat panel rendered successfully.
The fix uses the shared-resource
support introduced for upstream Wine in [DXVK 3.0](https://github.com/doitsujin/dxvk/releases/tag/v3.0).
The helper now installs the verified 3.1 DLLs before every NGM launch when needed.
Normal Wine crash reporting remains enabled in the shipped configuration.

A separate late crash was captured in Chromium 100's `cef_shutdown` call after
the original graphics failure was fixed. Vuplex was then scoped to Windows 7
compatibility mode, leaving Worlds on Windows 10. The follow-up test rendered
successfully and the browser exited without a new shutdown dump. The user has
since reported the Vuplex error again on quit with that setting active. The
shutdown crash remains unresolved; the earlier clean exit did not establish a
reliable workaround.

### Recurrent quit crash: symbolized evidence

The saved `.work/dxvk31-late-crash.mdmp` was analyzed using the official release
symbols for CEF `100.0.24+g0783cf8+chromium-100.0.4896.127`, which is the version
bundled with Worlds. The installed DLL and downloaded PDB have the same GUID
`93CAF2FC-6732-F421-4C4C-44205044422E`, age 1. The symbols archive's SHA-1 matches
the official build index: `41c8c62d6e799a87ca3f15eb27b9242929b3e0fe`.

The significant frames are:

```text
libcef + 0x1530cec  content::RenderProcessHostImpl::Cleanup
libcef + 0x152d260  content::RenderProcessHostImpl::DecrementKeepAliveRefCount
libcef + 0x14700c0  content::KeepAliveHandleImpl::~KeepAliveHandleImpl
libcef + 0x2fa814f  base::sequence_manager::internal::TaskQueueImpl::UnregisterTaskQueue
libcef + 0x156611d  content::BrowserTaskQueues::~BrowserTaskQueues
libcef + 0x2ea4e80  content::ContentMainRunnerImpl::Shutdown
libcef + 0x3bd57cf  CefMainRunner::FinalizeShutdown
libcef + 0x2ed7a07  CefContext::Shutdown
libcef + 0x2ed78fe  CefShutdown
```

At the fault, `NotificationServiceImpl::current()` returned null and cleanup
dereferenced it. This matches the shutdown-order failure described in
[CEF issue 3276](https://github.com/chromiumembedded/cef/issues/3276).
Its upstream correction drains pending UI/IO tasks before shutdown, but that
correction is already present in the source for the bundled CEF version.
Applying that patch again or changing the reported Windows version therefore
is not an established fix for this recurrence.

This is analysis of the saved earlier crash, not a fresh dump of the latest
reported error. A diagnostic launch exited during loading without producing a
dump; it did not test the fully initialized browser's quit path. Further live
testing was deferred because the user started a session and asked to keep it
open. Normal Wine crash reporting was restored before that session. No new
browser binary or crash workaround has been deployed.

The next useful test is a fully loaded desktop client, confirmation of its
own Quit dialog, and capture of the resulting exception before background
cleanup. A passing result must include absence of a new Vuplex exception,
along with absence of remaining processes. Process cleanup alone does not
establish a crash-free quit.

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
71 tests pass, including background-browser cleanup, active-game protection,
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

The follow-up shutdown change watches new complete Unity log lines for
`Input System module state changed to: ShutdownInProgress.` Both recent real
Player logs contain that message after quitting. This allows cleanup to start
even if `msw.exe` never exits: allow five seconds for normal game teardown,
then stop the dedicated Wine server. Server commands have five-second deadlines,
with a native fallback for surviving Wine, browser and crash-handler processes.
Cleanup only reports success after checking for remaining prefix processes.
It also handles native children that outlive the observed Wine server.

Old log contents are ignored; log rotation and truncation are handled. The
watcher rechecks game and server identities under the helper's operation lock,
and preserves a replacement session or an active launcher/settings operation.
Native fallback uses pidfds and rechecks exact prefix ownership before signalling.
The added tests cover these cases and use actual Linux child processes to check
that terminating a browser in the owned prefix preserves both the game and a
browser in a different prefix. This bounds a stuck shutdown; it does not repair
the bundled CEF shutdown bug described above.

The disposable Wine smoke test freezes a real Windows test process with
`SIGSTOP`, leaves a native browser fixture outside Wine's server, and appends
the confirmed-quit log message. The detached watcher cleared both and all
prefix services in **6.0 seconds**, while a browser fixture in another prefix
stayed running. This exercises the actual server shutdown and native fallback.
The test requires clang, lld-link and Wine's development import libraries.

A subsequent real Worlds test reached its Quit dialog during loading. After
the game accepted Quit, the new watcher logged `Worlds confirmed Quit` and
`Worlds session fully stopped`; native inspection found zero remaining prefix
processes. The test did not establish a crash-free quit of a fully loaded world.

```sh
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 tests/session_smoke.py
PYTHONPATH=. python3 tests/app_smoke.py      # was tests/gui_smoke.py (Tk, since removed)
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

## Login and per-account sessions

The in-client **Toy SDK** login (NGM's embedded WebView2 form) was observed
failing under Wine with `Network Error — Request / Log in failure (Toy SDK)`,
Toy error `803020007`. The dependable path is the website: it hands NGM an
`ngm://launch/` link that already carries the account's token, and NGM opens the
client authenticated. The launcher never persists that link
(`core.Helper.open_uri`; the session watcher receives paths only).

The game also keeps per-account state in the prefix — Unity PlayerPrefs under
`HKCU\Software\Nexon\MapleStory Worlds` include `@access_token_*`, `@user_id_*`
and `bt-MOD.UserName_*`, and NGM's WebView2 profile lives at
`drive_c/ProgramData/Nexon/NGM/NGM64.exe.WebView2`. That state is **not** enough
to skip sign-in: a tokenless launch (`make_uri("launch")`) of an installed prefix
that had previously entered a world still showed the game's LOG IN screen and
the Toy SDK error above (the user's screenshot). Every Play therefore needs a
fresh website link. Only value names were inspected; no token contents were read.

### How the website builds the launch link

Fetched from the live site on 2026-11-23 (`platform.nexon.com/ngm/js/npf_ngm.js`
and the site's Nuxt bundle). The world page's **Play** (`button.btn_play`) calls
`$evt.startMODGame("play", game)`, which — when signed in, on desktop — calls
`NGM.LaunchGame2("106603@1585", "/npsn:<GUID> /runmode:play /gameid:<world> /publishid:… /gamename:… /publishname:…")`.
`GxLaunchGame` reads the `NPP` cookie (passed as `-token:`), `A2SK` (`-a2sk:`)
and an insign token from `_ifwt`, all via `document.cookie`, and
`OpenUriWithHiddenFrameWithoutBlur` assigns the finished URI to a hidden iframe.
Signed out, the same Play button navigates to `signin.nexon.com` /
`www.nexon.com/account/en/login`.

`ExecuteNGM` only proceeds when `CustomScheme.IsSupportedBrowser()` is true,
i.e. Firefox (`InstallTrigger`), Chrome (`window.chrome`), Opera, or Safari on
macOS. WebKitGTK on Linux matches none, so its Play button would do nothing. The
launcher injects `window.chrome = {runtime: {}}` at document start, on
`https://maplestoryworlds.nexon.com/*` only. Nexon's sign-in pages and the
Cloudflare check are not modified. The site sits behind Cloudflare: plain HTTP
clients get a 403 challenge page, while WebKitGTK passes it (the first world-page
load took up to ~35 s; later loads reuse the clearance cookie).

ChronoStory is group `6AK9I`, world page
`https://maplestoryworlds.nexon.com/en/play/6697fa1ed9cd4de7bea6daffed99a637`.

## Account launcher (2026-11-23)

The Tk helper and the interim Tk account manager were replaced by a GTK 4 /
libadwaita launcher (`msw_helper/app.py`) and a WebKitGTK 6.0 browser layer
(`msw_helper/web.py`). Tested with GTK 4.24.1, libadwaita 1.10.0, WebKitGTK
2.54.2 and PyGObject 3.58.1 on Wayland.

**Model.** Each account is an independent data directory: its own Wine prefix
(so clients can run at the same time with separate Wine servers, settings, and
session watchers) and its own persistent WebKit profile in `web/` (data, cache,
`cookies.sqlite`). `accounts.Accounts` stores the list and each account's world in
`~/.config/msw-helper-wine/accounts.json`, and adopts a pre-existing classic
install at `~/.local/share/msw-helper-wine` as the **Default** account. Added
accounts live under `.../msw-helper-wine/instances/<id>` and are claimed with an
ownership marker before their web profile or prefix exists, so uninstall may
remove them; `web` and `prefix.partial` were added to the uninstall set.

**Play.** `LaunchWindow` loads the world page in the account's profile, polls
for the Play button plus `window.NGM` and the Nuxt store, and presses Play. A
`decide-policy` handler catches any `ngm:` navigation (including the hidden
iframe), validates it with `core.validate_uri`, ignores it (so it never reaches
the desktop), and hands it to that account's `Helper.open_uri`. All other
non-web schemes are blocked. The window stays visible so the user can sign in,
pass a site check, or press Play by hand; it closes itself once the link is
caught. Popups (social sign-in) open as related views in the same profile.

**Staying signed in.** Nexon's page offers "Login automatically in the future";
the launcher asks the user to tick it. WebKit does not persist session-only
cookies, so the launcher also saves Nexon's session cookies to
`web/session-cookies.json` (mode 0600 in a 0700 directory) after a signed-in page
and restores them once per run. Whether a given Nexon session survives a long
gap was not measured; an expired one simply leads back to the sign-in page.

**Live checks, signed out** (fresh throwaway profile, real site). The window
passed Cloudflare, found `button.btn_play`, `window.NGM` and the store, reported
the site as a supported browser with the shim, pressed Play once, and reached
`https://www.nexon.com/account/en/login` showing Nexon's real form. No link was
captured and nothing launched. In a separate prototype, calling the site's own
`NGM.LaunchGame2('106603@1585', …)` produced an `ngm://launch/` link that the
policy handler caught; it carried `-mode -game -passarg -insign_token -position
-architectureplatform -timestamp` (no `-token`, as expected when signed out).

A first live run pressed Play three times, because after a press the site
reloads the world page several times before it navigates. That would have used
up the automatic presses before the user returned from sign-in. Presses now have
a cooldown (10 s signed in, 30 s signed out; reaching sign-in was observed to
take ~12–50 s) and separate signed-in and signed-out budgets, and the cooldown
resets on the sign-in page. Polls only act on Worlds pages, so a sign-in cookie
appearing mid-sign-in (for example during 2-step verification) cannot pull the
window away. The rerun pressed once.

**Not tested here:** a signed-in launch, because it needs the user's own Nexon
credentials. That step relies on the site's documented-above code path, which
the prototype exercised up to the token.

**Copying an install.** A new account copies an installed, idle account's prefix
instead of downloading. Measured on the real Default prefix (5.2 GB), the largest
parts are the game (1.9 GB), its `resource_cache` (1.7 GB) and Edge/WebView2
(0.9 GB). The copy skips crash reports (`backtrace`, 211 MB), the in-game browser
profile (`Vuplex.WebView`), NGM's WebView2 profile, `Player*.log`, and temp
folders, then deletes the game's PlayerPrefs key so the new account doesn't
inherit the old one's saved token. With real Wine 11.16, `reg delete` of that
key returned 0 and removed it from `user.reg`, and returned 1 when it was already
absent; both are accepted. Drive links (`dosdevices/z:` → `/`) are copied as
links, and an interrupted copy leaves no prefix. The copy then runs
`Helper.finish("clone")`, which verifies the client and registers it.

Copying refuses while Wine or game processes use the source prefix. The live
process list for the Default prefix also contained the user's native Firefox
(`firefox`, `web content`, …), which inherited `WINEPREFIX` when the game opened a
link; an early version would therefore have refused to copy even after the game
was closed. The check now counts only Windows images and Wine processes. A full
real copy was not run because the user's game was open at the time.

**Desktop.** The visible menu entry is `msw-helper-wine-manager.desktop`
("MapleStory Worlds"), which opens the launcher and matches its application id
`io.github.MswHelperWine`. `msw-helper-wine.desktop` is hidden and keeps
`StartupWMClass=msw.exe`, so game windows get the Worlds name and icon. Neither
pins `--data-dir`. The `ngm://` handler sends links opened from the user's own
browser to the first account. Error pop-ups from desktop launches now use
libadwaita instead of Tk.

The first version of the launcher hid the manager entry instead, and the user's
pinned launcher disappeared: their GNOME dash favourite was
`msw-helper-wine-manager.desktop`, and GNOME drops `NoDisplay` entries from the
dash. The entry ids now keep that file visible. Re-registration also deletes
the retired `gui.py`/`manager.py` from the installed copy, because the copy step
only adds and overwrites files.

**Cleanup watcher and native browsers.** Re-registering then failed with
"Another install, update, or launch is in progress". `/proc/locks` showed the
session watcher holding the operation lock. Its log repeated "Worlds shutdown is
incomplete. Still running: crashhelper, firefox, forkserver, … web content"
for over nine minutes: the user's Firefox, opened by the game through
winebrowser, inherited `WINEPREFIX`. `shutdown_prefix` correctly never kills
Firefox, but it counted Firefox as remaining, failed, and retried forever, taking
the lock every few seconds. That also intermittently blocked Play and updates
for that account. `session.prefix_processes` now counts only Windows images and
Wine's own processes. `shutdown_prefix`, the watcher and the account copy check
use it, so a native app that merely inherited the variable no longer keeps a
session alive. The stuck watcher (old code) was stopped by hand; no Wine
processes remained in that prefix.

**Automated coverage.** `tests/test_accounts.py` covers the registry, world
parsing and persistence, adoption, routing, and copying (excludes, symlinks,
registry deletion, refusal cases, and cleanup after a failed copy).
`tests/test_web.py` covers sign-in page detection and link capture/blocking
through the real policy handler. `tests/app_smoke.py` runs the real GTK window
with a mocked backend and browser window: install, one-click Play routed to the
right prefix, copying for the second account, Stop while running, and
world/rename updates. Rendered screenshots were checked in light and dark mode
(`docs/launcher.png`).

```sh
python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 tests/app_smoke.py
PYTHONPATH=. python3 tests/desktop_smoke.py
```
