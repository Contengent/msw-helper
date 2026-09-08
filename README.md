# MapleStory Worlds Wine Helper

A small helper for installing and playing MapleStory Worlds on Linux through
Wine. It downloads the official Windows client, sets up the components it needs,
and handles updates. You can launch the game from the helper, your application
menu, or the Worlds website.

![MapleStory Worlds Helper](docs/helper.png)

You'll need 64-bit Linux, Wine, Python 3.11 or newer with Tk, and `xdg-utils`.
Your graphics driver needs Vulkan 1.4 support for DXVK. Allow at least 6 GiB of
free space. Tk is called `tk` on Arch and `python3-tk` on Debian/Ubuntu.

Open a terminal in this folder and run:

```sh
./msw
```

Click **Install Worlds** the first time. When installation finishes, click
**Play Worlds** to open the desktop app.

Installation also adds two entries to your application menu:

- **MapleStory Worlds (Wine)** opens the game.
- **MapleStory Worlds Helper** opens the helper GUI.

Both use the original Worlds icon and match their running windows, so you can
pin them to your taskbar.

To update the game or fix missing files, close Worlds and click
**Update / repair**. The client opens again when the check finishes.

After a game launched through the helper closes, the helper automatically stops
its remaining Wine background processes. If Worlds gets stuck or the helper
still reports it running, click **Stop Worlds** to close it and clean up its
dedicated Wine prefix.

If you prefer the terminal:

```sh
./install.sh       # Install Worlds and its menu entries
./msw play         # Launch the desktop app
./msw stop         # Close Worlds and stop its background processes
./msw update       # Update or repair the game
./uninstall.sh     # Uninstall, with confirmation
```

The **Website** button opens the
[Worlds website](https://maplestoryworlds.nexon.com/en/play). You can sign in and
choose a world there too; allow the browser to open the game when it asks.

To remove the installation, use **Uninstall…** in the helper or `./uninstall.sh`.
This removes the game, its local settings, cached downloads, and the installed
launchers. You can preview what will be removed with `./msw uninstall --dry-run`.

Files are stored in `~/.local/share/msw-helper-wine` by default. No `sudo` is
needed to run the helper. After pulling an update to this repo, run
`./msw register-desktop` to update the installed helper and menu entries.

If something goes wrong, **Open logs** shows the logs and `./msw doctor` reports
the installation's status. More options are listed in `./msw --help`; the
[testing notes](docs/validation.md) cover the Wine setup and issues we've seen.

Helper source: [GPL-3.0-or-later](LICENSE). MapleStory Worlds and its artwork
belong to their respective owners.
