"""GUI services for Caster: screen-reader speech, global hotkeys, the tray
icon, accessible control labelling and the settings dialog.

Split out of caster.py so the main window stays about casting.
"""

from __future__ import annotations

import ctypes
import os
import sys

import wx
import wx.adv

from caster_config import QUALITY_PRESETS


# ---------------------------------------------------------------------------
# Screen-reader speech
# ---------------------------------------------------------------------------

class NvdaSpeaker:
    """Speaks through NVDA directly, when NVDA is running and its controller
    client library is available.

    A status bar update is not reliably announced: NVDA reads a status bar on
    request, not when it changes, so "Playing on Kitchen" or "Capture failed"
    can pass silently. Speaking outright is the difference between feedback
    and no feedback for the events that matter -- a cast starting, a cast
    failing, a device going away.

    Everything degrades quietly. Without NVDA, or without the DLL, this is a
    no-op and the status bar carries on as the fallback.
    """

    #: The DLL is 64-bit or 32-bit to match the host process, not the OS.
    DLL_NAMES = ("nvdaControllerClient64.dll", "nvdaControllerClient32.dll")

    def __init__(self) -> None:
        self._dll = None
        self._load()

    def _search_paths(self) -> list:
        here = os.path.dirname(os.path.abspath(
            sys.executable if getattr(sys, "frozen", False) else __file__))
        return [here, os.path.join(here, "nvda"), ""]

    def _load(self) -> None:
        name = self.DLL_NAMES[0 if sys.maxsize > 2**32 else 1]
        for folder in self._search_paths():
            try:
                self._dll = ctypes.windll.LoadLibrary(
                    os.path.join(folder, name) if folder else name)
                return
            except Exception:
                continue

    @property
    def available(self) -> bool:
        if self._dll is None:
            return False
        try:
            # 0 means NVDA is running and listening.
            return self._dll.nvdaController_testIfRunning() == 0
        except Exception:
            return False

    def speak(self, text: str, interrupt: bool = False) -> None:
        if not text or self._dll is None:
            return
        try:
            if interrupt:
                self._dll.nvdaController_cancelSpeech()
            self._dll.nvdaController_speakText(ctypes.c_wchar_p(text))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Accessible labelling
# ---------------------------------------------------------------------------

class _NamedAccessible(wx.Accessible):
    """Pins a control's accessible name, leaving role, state and value to
    the native control underneath.

    Windows infers a control's name from the nearest static text created
    before it, which a wxSlider built with wxSL_LABELS quietly breaks: it
    inserts its own minimum, maximum and value statics in between, so the
    volume slider announced as "0". Stating the name outright is immune to
    whatever a control decides to build for itself.
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def GetName(self, childId):
        # childId 0 is the control itself; its parts keep their own names.
        if childId:
            return (wx.ACC_NOT_IMPLEMENTED, "")
        return (wx.ACC_OK, self._name)


def labelled(parent, sizer, text: str, build, proportion: int = 0,
             border: int = 8):
    """Build a control behind a real label and add both to `sizer`.

    A wx control with no label announces as bare "slider" or "edit", which
    for two sliders in a row means the volume and the position are
    indistinguishable by ear. `SetHelpText` does not fix that -- it feeds
    context help and tooltips, not the accessible name a screen reader
    reads. A visible wxStaticText immediately before the control does, and
    it also gives sighted users the same information.

    `build` is a callable taking the parent and returning the control,
    rather than a ready-made control, because "immediately before" means
    the creation order and not the sizer. Windows names a control after the
    nearest static text created before it, and wx gives a static's Alt
    mnemonic to whatever follows it. Building the control first put the
    static on the wrong side of both, so every control wore the *previous*
    one's name: the URL box announced "Devices:" and answered to Alt+D.
    Reordering the windows afterwards is not enough either -- a wxSpinCtrl
    is a buddy edit plus an up-down, and only the up-down can be moved, so
    the part that takes focus keeps the wrong label. Creating the static
    first is the only fix that reaches every control.
    """
    label = wx.StaticText(parent, label=text)
    sizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, border)
    control = build(parent)
    name = text.replace("&", "").rstrip(":")
    control.SetName(name)
    # Held on the control as well: the window owns the accessible, and a
    # Python-side reference keeps it from being collected under it.
    control._accessible = _NamedAccessible(name)
    control.SetAccessible(control._accessible)
    sizer.Add(control, proportion,
              wx.LEFT | wx.RIGHT | wx.EXPAND | (wx.TOP if not text else 0),
              border)
    return control


# ---------------------------------------------------------------------------
# Global hotkeys
# ---------------------------------------------------------------------------

#: action -> (modifiers, key, description). Chosen to avoid the Windows and
#: NVDA defaults: NVDA owns Insert and Caps Lock combinations, and Windows
#: owns most Win+key ones.
DEFAULT_HOTKEYS = {
    "cast_audio": (wx.MOD_CONTROL | wx.MOD_ALT, ord("A"),
                   "Cast system audio"),
    "cast_screen": (wx.MOD_CONTROL | wx.MOD_ALT, ord("S"), "Cast screen"),
    "stop": (wx.MOD_CONTROL | wx.MOD_ALT, ord("X"), "Stop casting"),
    "mute": (wx.MOD_CONTROL | wx.MOD_ALT, ord("M"), "Mute or unmute"),
    "volume_up": (wx.MOD_CONTROL | wx.MOD_ALT, wx.WXK_UP, "Volume up"),
    "volume_down": (wx.MOD_CONTROL | wx.MOD_ALT, wx.WXK_DOWN, "Volume down"),
}


class HotkeyManager:
    """System-wide hotkeys, so casting does not require finding the window.

    Registration is best-effort per key: another application may already own
    a combination, and one refusal must not cost the rest.
    """

    #: Ids start high to stay clear of wx's own.
    BASE_ID = 0xB000

    def __init__(self, frame: wx.Frame, on_action) -> None:
        self.frame = frame
        self.on_action = on_action
        self._registered: dict = {}

    def register_all(self) -> list:
        """Register every default hotkey; returns the actions that failed."""
        failed = []
        for index, (action, (mods, key, _)) in enumerate(
                DEFAULT_HOTKEYS.items()):
            hotkey_id = self.BASE_ID + index
            try:
                if self.frame.RegisterHotKey(hotkey_id, mods, key):
                    self._registered[hotkey_id] = action
                    self.frame.Bind(wx.EVT_HOTKEY, self._on_hotkey,
                                    id=hotkey_id)
                else:
                    failed.append(action)
            except Exception:
                failed.append(action)
        return failed

    def unregister_all(self) -> None:
        for hotkey_id in list(self._registered):
            try:
                self.frame.UnregisterHotKey(hotkey_id)
            except Exception:
                pass
        self._registered.clear()

    def _on_hotkey(self, event) -> None:
        action = self._registered.get(event.GetId())
        if action:
            self.on_action(action)

    @staticmethod
    def describe() -> str:
        parts = []
        for mods, key, description in DEFAULT_HOTKEYS.values():
            name = {wx.WXK_UP: "Up arrow", wx.WXK_DOWN: "Down arrow"}.get(
                key, chr(key) if isinstance(key, int) and key < 256 else "?")
            combo = []
            if mods & wx.MOD_CONTROL:
                combo.append("Ctrl")
            if mods & wx.MOD_ALT:
                combo.append("Alt")
            if mods & wx.MOD_SHIFT:
                combo.append("Shift")
            combo.append(name)
            parts.append(f"{'+'.join(combo)}: {description}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------

class TrayIcon(wx.adv.TaskBarIcon):
    """Keeps Caster reachable while its window is hidden.

    The menu duplicates what the hotkeys do, because a hotkey another app has
    already claimed leaves no other way in.
    """

    def __init__(self, frame, actions) -> None:
        super().__init__()
        self.frame = frame
        self.actions = actions      # [(label, callable), ...]
        # wxPython moved TaskBarIcon.SetIcon from wx.Icon to wx.BitmapBundle
        # partway through 4.x, and which one it wants decides whether there
        # is a tray icon at all -- so try both rather than pick.
        bitmap = wx.ArtProvider.GetBitmap(wx.ART_INFORMATION, wx.ART_OTHER,
                                          wx.Size(16, 16))
        for candidate in (bitmap, wx.Icon(bitmap)):
            try:
                self.SetIcon(candidate, "Caster")
                break
            except Exception:
                continue
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, lambda e: self.restore())

    def CreatePopupMenu(self):
        menu = wx.Menu()
        for label, handler in self.actions:
            if label == "-":
                menu.AppendSeparator()
                continue
            item = menu.Append(wx.ID_ANY, label)
            self.Bind(wx.EVT_MENU, lambda e, h=handler: h(), item)
        return menu

    def restore(self) -> None:
        self.frame.Show()
        self.frame.Restore()
        self.frame.Raise()


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(wx.Dialog):
    """Preferences. Every control carries a visible label, and the dialog is
    laid out in one column so tab order matches reading order."""

    def __init__(self, parent, settings, output_devices, input_devices) -> None:
        super().__init__(parent, title="Settings",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.settings = settings
        self._outputs = output_devices or [("Default output", "")]
        self._inputs = input_devices or [("No microphone", "")]

        panel = wx.Panel(self)
        box = wx.BoxSizer(wx.VERTICAL)

        quality_keys = list(QUALITY_PRESETS)
        self._quality_keys = quality_keys
        self.quality = labelled(
            panel, box, "Screen capture &quality:",
            lambda p: wx.Choice(
                p, choices=[QUALITY_PRESETS[k]["label"] for k in quality_keys]))
        current = settings["capture_quality"]
        self.quality.SetSelection(
            quality_keys.index(current) if current in quality_keys else 1)

        self.output = labelled(
            panel, box, "Capture sound &from:",
            lambda p: wx.Choice(p, choices=[d[0] for d in self._outputs]))
        self.output.SetSelection(self._index(self._outputs,
                                             settings["capture_audio_device"]))

        self.mic = labelled(
            panel, box, "Mix in &microphone:",
            lambda p: wx.Choice(p, choices=[d[0] for d in self._inputs]))
        self.mic.SetSelection(self._index(self._inputs,
                                          settings["capture_mic_device"]))

        self.offset = labelled(
            panel, box, "&Audio delay in ms (negative delays picture):",
            lambda p: wx.SpinCtrl(p, min=-2000, max=2000,
                                  initial=int(settings["av_offset_ms"])))

        self.sleep = labelled(
            panel, box, "Sleep &timer in minutes (0 = off):",
            lambda p: wx.SpinCtrl(
                p, min=0, max=600,
                initial=int(settings["sleep_timer_minutes"])))

        self.seeds = labelled(
            panel, box, "&Sonos IPs on other subnets, comma separated:",
            lambda p: wx.TextCtrl(
                p, value=", ".join(settings["sonos_seed_ips"])))

        self.kodi_user = labelled(
            panel, box, "&Kodi username:",
            lambda p: wx.TextCtrl(p, value=settings["kodi_username"]))
        self.kodi_pass = labelled(
            panel, box, "Kodi &password:",
            lambda p: wx.TextCtrl(p, value=settings["kodi_password"],
                                  style=wx.TE_PASSWORD))

        self.checks = {}
        for key, label in (
                ("discover_on_launch", "Scan for &devices at startup"),
                ("reselect_last_device", "Reselect the &last device"),
                ("global_hotkeys", "System-&wide hotkeys"),
                ("minimise_to_tray", "Close to the &notification area"),
                ("auto_reconnect", "&Reconnect if a device drops"),
                ("speak_status", "Speak status through N&VDA")):
            check = wx.CheckBox(panel, label=label)
            check.SetValue(bool(settings[key]))
            box.Add(check, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
            self.checks[key] = check

        panel.SetSizer(box)

        # The buttons belong to the dialog, not the panel, so they go in the
        # dialog's own sizer. Putting dialog-owned buttons inside the panel's
        # sizer leaves them outside the panel's tab traversal, which is a
        # dialog you cannot accept from the keyboard.
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        outer.Add(buttons, 0, wx.ALL | wx.EXPAND, 8)
        self.SetSizer(outer)

        self.SetMinSize((460, 520))
        outer.Fit(self)
        self.CentreOnParent()
        self.quality.SetFocus()

    @staticmethod
    def _index(pairs, value) -> int:
        for i, (_, name) in enumerate(pairs):
            if name == value:
                return i
        return 0

    def apply(self) -> None:
        self.settings.update(
            capture_quality=self._quality_keys[max(self.quality.GetSelection(), 0)],
            capture_audio_device=self._outputs[max(self.output.GetSelection(), 0)][1],
            capture_mic_device=self._inputs[max(self.mic.GetSelection(), 0)][1],
            av_offset_ms=int(self.offset.GetValue()),
            sleep_timer_minutes=int(self.sleep.GetValue()),
            sonos_seed_ips=[p.strip() for p in self.seeds.GetValue().split(",")
                            if p.strip()],
            kodi_username=self.kodi_user.GetValue().strip(),
            kodi_password=self.kodi_pass.GetValue(),
            **{key: bool(check.GetValue())
               for key, check in self.checks.items()})
