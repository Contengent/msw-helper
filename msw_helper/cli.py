import argparse
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import URLError
import webbrowser

from . import __version__
from .core import Helper, HelperError, WEBSITE


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS,
                        help="data directory containing prefix, cache and logs (or MSW_HOME)")
    common.add_argument("--wine", default=argparse.SUPPRESS,
                        help="Wine executable (or MSW_WINE; default: wine)")
    p = argparse.ArgumentParser(description="Install, update and launch MapleStory Worlds with Wine.", parents=[common])
    p.add_argument("--version", action="version", version=__version__)
    commands = p.add_subparsers(dest="command")
    for name, help_text in (("gui", "open the graphical helper (default)"),
                            ("install", "install the actual Worlds client and desktop entries"),
                            ("update", "check/repair game files using Nexon, then open the client"),
                            ("verify", "check actual client files; exit nonzero if incomplete"),
                            ("doctor", "show paths, dependencies and installation diagnostics"),
                            ("play", "open the official website for login and world selection"),
                            ("register-desktop", "install menu entry and ngm:// handler"),
                            ("winecfg", "open Wine settings for this prefix")):
        sub = commands.add_parser(name, help=help_text, parents=[common])
        if name == "install":
            sub.add_argument("--no-desktop", action="store_true", help="skip desktop integration")
    sub = commands.add_parser("open", help="handle one ngm:// website URL", parents=[common])
    sub.add_argument("uri", help="the complete browser URL, quoted as one argument")
    return p


def show_error(message):
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("MapleStory Worlds", message, parent=root)
        root.destroy()
    except Exception:
        pass  # The CLI error remains available without Tk or a display.


def main(argv=None):
    args = parser().parse_args(argv)
    helper = Helper(getattr(args, "data_dir", None), getattr(args, "wine", None))
    command = args.command or "gui"
    try:
        if command == "gui":
            from .gui import run_gui
            run_gui(helper)
        elif command == "install":
            helper.install()
            if not args.no_desktop:
                from .desktop import register
                register(helper)
        elif command == "update":
            helper.update()
        elif command == "verify":
            report = helper.verify()
            print(json.dumps(report, indent=2))
            return 0 if report["installed"] else 1
        elif command == "doctor":
            print(json.dumps(helper.doctor(), indent=2))
        elif command == "play":
            if not webbrowser.open(WEBSITE):
                raise HelperError(f"Could not open your browser. Visit {WEBSITE}")
        elif command == "register-desktop":
            from .desktop import register
            register(helper)
        elif command == "open":
            helper.open_uri(args.uri)
        elif command == "winecfg":
            helper.require_wine()
            helper.run(["winecfg"], timeout=None, label="Wine settings")
    except KeyboardInterrupt:
        print("Interrupted. Run the command again to resume.", file=sys.stderr)
        return 130
    except (HelperError, OSError, URLError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        message = str(exc)
        print(f"Error: {message}", file=sys.stderr)
        if command in {"open", "gui"}:
            show_error(message)
        return 1
    return 0
