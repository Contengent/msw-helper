"""Exercise real Tk widgets and worker delivery (run under Xvfb in CI)."""
from pathlib import Path
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

from msw_helper.core import Helper
from msw_helper.gui import Application
from msw_helper.desktop import GUI_CLASS


with tempfile.TemporaryDirectory() as data:
    root = tk.Tk(className=GUI_CLASS)
    app = Application(root, Helper(Path(data)))
    assert root.winfo_class() == GUI_CLASS
    assert (app.icon.width(), app.icon.height()) == (256, 256)
    with patch('msw_helper.gui.subprocess.Popen') as opener:
        app.buttons['logs'].invoke()
        opener.assert_not_called()
    assert not app.helper.logs.exists()
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
    with patch.object(app.helper, "play", side_effect=lambda: app.helper.emit("Client launch requested")) as launch, \
            patch("msw_helper.gui.webbrowser.open", return_value=True) as browser:
        assert app.buttons["play"]["text"] == "Play Worlds"
        app.buttons["play"].invoke()
        deadline = time.monotonic() + 5
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        assert not app.busy, "Play Worlds did not complete"
        launch.assert_called_once_with()
        browser.assert_not_called()
        assert "Client launch requested" in app.output.get("1.0", "end")
        app.buttons["website"].invoke()
        browser.assert_called_once()
    app.helper.game_dir.mkdir(parents=True)
    app.refresh()
    assert "repair" in app.status.get()
    assert str(app.buttons["update"]["state"]) == "normal"
    root.update()
    assert app.cancel_button.winfo_viewable()
    assert app.cancel_button.winfo_rooty() + app.cancel_button.winfo_height() <= root.winfo_rooty() + root.winfo_height()
    assert app.buttons["uninstall"].winfo_viewable()
    with patch("tkinter.messagebox.askyesno", return_value=False), \
            patch.object(app.helper, "uninstall") as remove:
        app.buttons["uninstall"].invoke()
        remove.assert_not_called()
        assert not app.busy
    with patch("tkinter.messagebox.askyesno", return_value=True), \
            patch("tkinter.messagebox.showinfo"), \
            patch.object(app.helper, "uninstall") as remove:
        app.buttons["uninstall"].invoke()
        assert str(app.cancel_button["state"]) == "disabled"
        deadline = time.monotonic() + 5
        while app.busy and time.monotonic() < deadline:
            root.update()
            time.sleep(0.02)
        remove.assert_called_once_with()
        assert not app.busy
        try:
            assert not root.winfo_exists(), "GUI should close after removing its installed copy"
        except tk.TclError:
            pass  # Tcl application was destroyed by successful uninstall.
print("GUI smoke test passed")
