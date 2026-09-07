"""Exercise real Tk widgets and worker delivery (run under Xvfb in CI)."""
from pathlib import Path
import tempfile
import time
import tkinter as tk

from msw_helper.core import Helper
from msw_helper.gui import Application


with tempfile.TemporaryDirectory() as data:
    root = tk.Tk()
    app = Application(root, Helper(Path(data)))
    app.buttons["verify"].invoke()
    assert "not installed" in app.status.get()
    app.start("Test worker", lambda: app.helper.emit("Worker reached the GUI"))
    deadline = time.monotonic() + 5
    while app.busy and time.monotonic() < deadline:
        root.update()
        time.sleep(0.02)
    assert not app.busy, "GUI worker completion was not delivered"
    assert "Worker reached the GUI" in app.output.get("1.0", "end")
    assert str(app.buttons["install"]["state"]) == "normal"
    assert str(app.buttons["update"]["state"]) == "disabled"
    app.helper.game_dir.mkdir(parents=True)
    app.refresh()
    assert "repair" in app.status.get()
    assert str(app.buttons["update"]["state"]) == "normal"
    root.update()
    assert app.cancel_button.winfo_viewable()
    assert app.cancel_button.winfo_rooty() + app.cancel_button.winfo_height() <= root.winfo_rooty() + root.winfo_height()
    app.close()
print("GUI smoke test passed")
