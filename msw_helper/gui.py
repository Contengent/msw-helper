"""Tk GUI: workers never access Tk; the event loop owns all widget updates."""
import queue
import subprocess
import threading
import webbrowser

from .core import Cancelled, WEBSITE


class Application:
    def __init__(self, root, helper):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText
        self.root, self.helper = root, helper
        self.events = queue.Queue()
        self.busy = False
        helper.emit = lambda text: self.events.put(("log", text))
        root.title("MapleStory Worlds · Wine Helper")
        from .desktop import ICON_NAME, RESOURCES
        self.icon = tk.PhotoImage(master=root, file=str(RESOURCES / ICON_NAME))
        root.iconphoto(True, self.icon)
        root.geometry("800x590")
        root.minsize(800, 590)
        root.configure(background="#f5f3ee")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f3ee")
        style.configure("TLabel", background="#f5f3ee", foreground="#252a32")
        style.configure("Title.TLabel", font=("sans-serif", 24, "bold"))
        style.configure("TButton", padding=(12, 10), font=("sans-serif", 10))
        style.configure("Play.TButton", background="#ffb144", foreground="#242424")
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="MapleStory Worlds", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Install, update, and launch MapleStory Worlds.",
                  font=("sans-serif", 11)).pack(anchor="w", pady=(6, 18))
        self.status = tk.StringVar()
        ttk.Label(frame, textvariable=self.status, font=("sans-serif", 12, "bold")).pack(anchor="w")
        self.location = ttk.Label(frame, text=str(helper.game_dir), wraplength=740)
        self.location.pack(anchor="w", pady=(5, 16))
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        self.buttons = {}
        for name, label, command in (
                ("play", "Play Worlds", self.play),
                ("install", "Install Worlds", self.install),
                ("update", "Update / repair", lambda: self.start("Updating Worlds…", helper.update)),
                ("website", "Website", self.website)):
            button = ttk.Button(actions, text=label, command=command,
                                style="Play.TButton" if name == "play" else "TButton")
            button.pack(side="left", padx=(0, 8))
            self.buttons[name] = button
        ttk.Label(frame, text="Play Worlds opens the desktop client. Website opens your browser.\n"
                  "Update / repair opens the client when Nexon finishes checking files.",
                  wraplength=730).pack(anchor="w", pady=(12, 16))
        tools = ttk.Frame(frame)
        tools.pack(fill="x")
        for name, label, command in (
                ("verify", "Verify files", self.verify),
                ("register", "Register website links", self.register),
                ("winecfg", "Wine settings", lambda: self.start("Wine settings…", helper.winecfg)),
                ("logs", "Open logs", self.logs)):
            button = ttk.Button(tools, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            self.buttons[name] = button
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(18, 8))
        footer = ttk.Frame(frame)
        footer.pack(side="bottom", fill="x", pady=(10, 0))
        self.buttons["uninstall"] = ttk.Button(footer, text="Uninstall…", command=self.uninstall)
        self.buttons["uninstall"].pack(side="left")
        self.cancel_button = ttk.Button(footer, text="Cancel operation", command=helper.cancel.set, state="disabled")
        self.cancel_button.pack(side="right")
        self.output = ScrolledText(frame, height=10, wrap="word", font=("monospace", 10),
                                   background="#fffefa", foreground="#252a32", relief="flat", padx=12, pady=10)
        self.output.pack(fill="both", expand=True)
        self.output.configure(state="disabled")
        self.refresh()
        self.append("Ready. Downloads and game files are kept in a dedicated Wine prefix.")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.timer = root.after(100, self.poll)

    def append(self, message):
        self.output.configure(state="normal")
        self.output.insert("end", message + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def refresh(self):
        installed = self.helper.verify()["installed"]
        repairable = self.helper.game_dir.is_dir()
        self.status.set("Worlds client installed" if installed else
                        "Worlds files need repair" if repairable else "Worlds is not installed yet")
        self.buttons["update"].configure(state="normal" if repairable else "disabled")

    def start(self, label, operation, *, cancellable=True, completion="done"):
        if self.busy:
            return
        self.busy = True
        self.helper.cancel.clear()
        self.status.set(label)
        self.append(label)
        for button in self.buttons.values():
            button.configure(state="disabled")
        self.cancel_button.configure(state="normal" if cancellable else "disabled")
        self.progress.start(12)
        def worker():
            try:
                operation()
            except Exception as exc:
                self.events.put(("cancelled" if isinstance(exc, Cancelled) else "error", str(exc)))
            else:
                self.events.put((completion, "Operation completed."))
        threading.Thread(target=worker, daemon=True).start()

    def install(self):
        def operation():
            from .desktop import register
            self.helper.install()
            register(self.helper)
        self.start("Installing Worlds…", operation)

    def register(self):
        from .desktop import register
        self.start("Registering website links…", lambda: register(self.helper))

    def uninstall(self):
        from tkinter import messagebox
        if messagebox.askyesno("Uninstall MapleStory Worlds",
                "Remove Worlds, its Wine prefix, local settings, downloads, and desktop entries?\n\n"
                + str(self.helper.home), parent=self.root, default="no"):
            self.start("Uninstalling Worlds…", self.helper.uninstall,
                       cancellable=False, completion="uninstalled")

    def verify(self):
        report = self.helper.verify()
        if report["installed"]:
            self.append("Worlds client files verified.")
        else:
            self.append("Missing or incomplete files:\n" + "\n".join(report["missing"]))
        self.append("Nexon launcher: " + ("ready" if report["ngm"] else "missing") +
                    ". WebView2: " + ("ready." if report["webview2"] else "missing."))
        self.append("Graphics: " + ("ready." if report["dxvk"] else
                    "DXVK will be installed on the next install, repair, or launch."))
        self.refresh()

    def play(self):
        self.start("Starting Worlds…", self.helper.play)

    def website(self):
        if webbrowser.open(WEBSITE):
            self.append("Sign in and click Play on the Worlds website.")
        else:
            self.append(f"Open {WEBSITE} in your browser.")

    def logs(self):
        if not self.helper.logs.is_dir():
            self.append("No logs yet. Install or launch Worlds to create them.")
            return
        try:
            subprocess.Popen(["xdg-open", str(self.helper.logs)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.append(str(exc))

    def poll(self):
        from tkinter import messagebox
        try:
            while True:
                kind, message = self.events.get_nowait()
                self.append(message)
                if kind != "log":
                    self.busy = False
                    self.progress.stop()
                    self.cancel_button.configure(state="disabled")
                    for button in self.buttons.values():
                        button.configure(state="normal")
                    self.refresh()
                    if kind == "error":
                        self.status.set("Operation failed — see details below")
                        messagebox.showerror("MapleStory Worlds", message, parent=self.root)
                    elif kind == "uninstalled":
                        messagebox.showinfo("MapleStory Worlds", "Worlds has been uninstalled.", parent=self.root)
                        self.close()
                        return
        except queue.Empty:
            pass
        self.timer = self.root.after(100, self.poll)

    def close(self):
        from tkinter import messagebox
        if self.busy:
            messagebox.showinfo("Operation in progress", "Cancel the operation and wait for it to stop before closing.", parent=self.root)
            return
        self.root.after_cancel(self.timer)
        self.root.destroy()


def run_gui(helper):
    try:
        import tkinter as tk
    except ImportError as exc:
        from .core import HelperError
        raise HelperError("The GUI needs Python Tk: install python3-tk (Debian/Ubuntu) or tk (Arch).") from exc
    from .desktop import GUI_CLASS
    root = tk.Tk(className=GUI_CLASS)
    Application(root, helper)
    root.mainloop()
