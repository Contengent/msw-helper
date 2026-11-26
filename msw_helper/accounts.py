"""Accounts: one Wine prefix plus one private Nexon web session per account.

Each account is an independent data directory. Its Wine prefix runs that
account's client, so several accounts can play at once without sharing a Wine
server, settings or game session. Its ``web`` directory is a private browser
profile: the user signs in to Nexon once there, and the launcher later asks the
Worlds website (inside that profile) to start the selected world. The site
builds the ``ngm://`` launch link from that profile's login cookie, and the
launcher hands it straight to the account's prefix. The short-lived link itself
is never stored.
"""
import json
import os
from pathlib import Path
import re
import shutil

from .core import Helper, HelperError, atomic_json

REGISTRY_VERSION = 1
DEFAULT_ID = "default"
WORLD_HOST = "https://maplestoryworlds.nexon.com"
DEFAULT_WORLD = {"name": "ChronoStory", "id": "6697fa1ed9cd4de7bea6daffed99a637"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_WORLD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_WORLD_URL_RE = re.compile(r"^https://maplestoryworlds\.nexon\.com/(?:[a-z]{2}/)?play/([0-9a-f]{32})/?(?:[?#].*)?$")

# Account-specific data that must not be copied from one account to another.
# Paths are relative to the prefix; "*" matches the Wine user directory.
CLONE_EXCLUDES = (
    "drive_c/users/*/AppData/LocalLow/nexon/MapleStory Worlds/backtrace",
    "drive_c/users/*/AppData/LocalLow/nexon/MapleStory Worlds/Vuplex.WebView",
    "drive_c/users/*/AppData/LocalLow/nexon/MapleStory Worlds/Player.log",
    "drive_c/users/*/AppData/LocalLow/nexon/MapleStory Worlds/Player-prev.log",
    "drive_c/users/*/AppData/Local/Temp",
    "drive_c/ProgramData/Nexon/NGM/NGM64.exe.WebView2",
    "drive_c/windows/temp",
)
# Unity PlayerPrefs: the game's per-account access token, user id and name.
GAME_PREFS_KEY = r"HKCU\Software\Nexon\MapleStory Worlds"


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser().resolve()
    return base / "msw-helper-wine"


def registry_path() -> Path:
    return config_dir() / "accounts.json"


def default_home() -> Path:
    """The classic single-account location, honoring MSW_HOME and XDG_DATA_HOME."""
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).expanduser().resolve()
    return Path(os.environ.get("MSW_HOME", data_home / "msw-helper-wine")).expanduser().resolve()


def instances_root() -> Path:
    return default_home() / "instances"


def world_url(world_id: str) -> str:
    return f"{WORLD_HOST}/en/play/{world_id}"


def parse_world(text: str) -> str:
    """Accept a world page URL (or its 32-character id) and return the id."""
    text = text.strip()
    if _WORLD_ID_RE.match(text):
        return text
    match = _WORLD_URL_RE.match(text)
    if not match:
        raise HelperError("Paste a world page link such as "
                          "https://maplestoryworlds.nexon.com/en/play/<world id>.")
    return match.group(1)


def _valid_id(value) -> bool:
    return isinstance(value, str) and bool(_SLUG_RE.match(value))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32].strip("-")


class Account:
    def __init__(self, id: str, name: str, data_dir):
        self.id = id
        self.name = name
        self.data_dir = Path(data_dir)

    @property
    def web_dir(self) -> Path:
        return self.data_dir / "web"

    def helper(self, *, wine=None, emit=None) -> Helper:
        return Helper(self.data_dir, wine=wine, emit=emit)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "data_dir": str(self.data_dir)}


class Accounts:
    """The ordered account list and the selected world, saved in one file."""

    def __init__(self):
        self.path = registry_path()
        self.accounts: list[Account] = []
        self.world = dict(DEFAULT_WORLD)
        self._load()
        self._adopt_default()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != REGISTRY_VERSION:
            return
        world = data.get("world")
        if (isinstance(world, dict) and isinstance(world.get("name"), str) and world["name"].strip()
                and isinstance(world.get("id"), str) and _WORLD_ID_RE.match(world["id"])):
            self.world = {"name": world["name"].strip(), "id": world["id"]}
        rows = data.get("accounts")
        seen = set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            id, name, path = row.get("id"), row.get("name"), row.get("data_dir")
            if (_valid_id(id) and id not in seen and isinstance(name, str) and name.strip()
                    and isinstance(path, str) and Path(path).is_absolute()):
                seen.add(id)
                self.accounts.append(Account(id, name.strip(), Path(path)))

    def _adopt_default(self):
        """Register an existing single-account install so upgraders keep it."""
        home = default_home()
        if any(a.data_dir == home for a in self.accounts):
            return
        if (home / "prefix/system.reg").exists() or (home / "installation.json").exists():
            self.accounts.insert(0, Account(self._unique_id(DEFAULT_ID), "Default", home))
            self._save()

    def _unique_id(self, base: str) -> str:
        base = base or "account"
        id, n = base, 2
        while any(a.id == id for a in self.accounts):
            id, n = f"{base}-{n}", n + 1
        return id

    def _save(self):
        atomic_json(self.path, {"version": REGISTRY_VERSION, "world": self.world,
                                "accounts": [a.to_dict() for a in self.accounts]})

    def get(self, id: str) -> Account:
        for account in self.accounts:
            if account.id == id:
                return account
        raise HelperError(f"No account with id {id!r}.")

    def resolve(self, key: str) -> Account:
        for account in self.accounts:
            if account.id == key or account.name == key:
                return account
        raise HelperError(f"No account matching {key!r}. Open the launcher to see your accounts.")

    def default(self) -> Account:
        if self.accounts:
            return self.accounts[0]
        return Account(DEFAULT_ID, "Default", default_home())

    def add(self, name: str) -> Account:
        name = name.strip()
        if not name:
            raise HelperError("Enter a name for the account.")
        if any(a.name.casefold() == name.casefold() for a in self.accounts):
            raise HelperError(f"An account named “{name}” already exists.")
        id = self._unique_id(_slug(name))
        account = Account(id, name, instances_root() / id)
        if account.data_dir == default_home() or any(a.data_dir == account.data_dir for a in self.accounts):
            raise HelperError(f"An account directory already exists at {account.data_dir}.")
        if account.data_dir.exists() and any(account.data_dir.iterdir()):
            raise HelperError(f"{account.data_dir} already contains files. Remove it or choose another name.")
        # Claim the directory before anything (the web profile, the prefix) is
        # created in it, so a later uninstall is allowed to remove those files.
        from .maintenance import mark_installation
        mark_installation(account.helper())
        self.accounts.append(account)
        self._save()
        return account

    def rename(self, id: str, name: str):
        name = name.strip()
        if not name:
            raise HelperError("Enter a name for the account.")
        if any(a.name.casefold() == name.casefold() and a.id != id for a in self.accounts):
            raise HelperError(f"An account named “{name}” already exists.")
        self.get(id).name = name
        self._save()

    def remove(self, id: str):
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if a.id != id]
        if len(self.accounts) != before:
            self._save()

    def set_world(self, name: str, world_id: str):
        name = name.strip()
        if not name:
            raise HelperError("Enter a name for the world.")
        if not _WORLD_ID_RE.match(world_id):
            raise HelperError("That is not a valid world id.")
        self.world = {"name": name, "id": world_id}
        self._save()

    def installed(self, *, wine=None) -> list[Account]:
        return [a for a in self.accounts if a.helper(wine=wine).verify()["installed"]]


def route(uri: str, *, wine=None, emit=None) -> Helper:
    """A website link opened in the user's own browser goes to the first account.

    Launches started from the launcher never reach the desktop handler: the
    launcher captures the link in the account's own web profile instead.
    """
    return Accounts().default().helper(wine=wine, emit=emit)


def _excluded(relative: str) -> bool:
    from fnmatch import fnmatchcase
    return any(fnmatchcase(relative, pattern) for pattern in CLONE_EXCLUDES)


def clone_install(source: Helper, target: Helper):
    """Copy an installed, idle account's prefix to a new account.

    Saves downloading Worlds again. Account-specific state — the game's saved
    access token and user id, the launcher's and in-game browser profiles,
    logs and crash reports — is left out, so the new account starts signed out.
    """
    from .maintenance import mark_installation, validate_ownership
    from .session import prefix_processes, shutdown_prefix
    with target.locked(), source.locked():
        target.require_wine()
        validate_ownership(source)
        mark_installation(target)
        if not source.verify()["installed"]:
            raise HelperError("The account to copy from does not have a complete Worlds install.")
        # A native browser the game opened inherits WINEPREFIX but doesn't use the prefix.
        in_use = sorted(set(prefix_processes(source.prefix).values()))
        if in_use:
            raise HelperError("Close the game on the account being copied, then try again. "
                              "Still running: " + ", ".join(in_use[:6]))
        if target.prefix.exists():
            raise HelperError("This account already has a Wine prefix.")
        staging = target.home / "prefix.partial"
        if staging.exists():
            shutil.rmtree(staging)
        target.emit("Copying the game from an existing account (no download needed)…")
        copied = [0]

        def ignore(directory, names):
            rel_dir = os.path.relpath(directory, source.prefix)
            skipped = []
            for name in names:
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                if _excluded(rel):
                    skipped.append(name)
            target.check_cancel()
            copied[0] += len(names) - len(skipped)
            if copied[0] >= 2000:
                copied[0] = 0
                target.emit("Copying game files…")
            return skipped
        try:
            # Wine's drive links (dosdevices/z: -> /) are copied as links, never followed.
            shutil.copytree(source.prefix, staging, symlinks=True, ignore=ignore)
            staging.rename(target.prefix)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        for name in ("dxvk.json",):
            if (source.home / name).is_file():
                shutil.copy2(source.home / name, target.home / name)
        target.emit("Clearing the copied account's saved game login…")
        target.run(["reg", "delete", GAME_PREFS_KEY, "/f"], timeout=60,
                   label="Clearing copied game settings", allowed=(0, 1))
        target.finish("clone")
        shutdown_prefix(target)
