# Desktop resources

`msw-helper-wine-manager.desktop` is the menu entry, **MapleStory Worlds**: it
opens the account launcher (`gui`) and matches the launcher's window (application
id `io.github.MswHelperWine`). Keep this one visible — it is the entry users have
pinned since the first release, and GNOME drops hidden entries from the dash.

`msw-helper-wine.desktop` is hidden. It only matches the game's `msw.exe`
windows so running clients get the Worlds name and icon.
`msw-helper-wine-ngm.desktop` handles `ngm://` links opened from your own browser
and sends them to the first account.

`msw-helper-wine.png` is the 256×256 application icon extracted from Nexon's
installed `msw.exe` (Worlds client `win.26.7.1.722`, PE icon resource 1) and decoded
to PNG without resizing. The same artwork is used for the desktop entries and
the launcher window icon.

The MapleStory Worlds artwork and trademarks belong to their respective owners;
they are not covered by the helper source's GPL license.
