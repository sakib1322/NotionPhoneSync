"""Small Windows-only helpers: standard folders, power status, start with Windows."""
import ctypes
import os
import sys
import uuid

try:
    import winreg
    from ctypes import wintypes
    IS_WINDOWS = True
except ImportError:  # lets the sync logic be tested on other systems
    IS_WINDOWS = False

APP_KEY = "NotionLaptopSync"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

KNOWN_FOLDERS = [
    ("Desktop", "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"),
    ("Documents", "FDD39AD0-238F-46AF-ADB4-6C85480369C7"),
    ("Downloads", "374DE290-123F-4565-9164-39C4925E467B"),
    ("Pictures", "33E28130-4E1E-4676-835A-98395C3BC3BB"),
    ("Videos", "18989B1D-99B5-455B-841C-AB7C74E4DDFC"),
    ("Music", "4BD8D571-6D19-48D3-BE97-422220080E43"),
]


def _known_folder(guid_text):
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    u = uuid.UUID(guid_text)
    g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    p = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) != 0:
        return None
    try:
        return p.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(p)


def default_folders():
    """Your personal folders (works even when OneDrive has moved them)."""
    out = []
    for name, guid in KNOWN_FOLDERS:
        path = None
        if IS_WINDOWS:
            try:
                path = _known_folder(guid)
            except Exception:
                path = None
        path = path or os.path.join(os.path.expanduser("~"), name)
        if os.path.isdir(path) and path not in out:
            out.append(path)
    return out


def on_mains_power():
    """True when plugged in (or a desktop without a battery)."""
    if not IS_WINDOWS:
        return True

    class SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]

    s = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
        return True
    return s.ACLineStatus != 0 or s.BatteryFlag == 128


def frozen():
    return getattr(sys, "frozen", False)


def set_startup(enabled):
    """Adds or removes the app from 'start with Windows' (only for the installed app)."""
    if not IS_WINDOWS or not frozen():
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_KEY, 0, winreg.REG_SZ, f'"{sys.executable}" --minimized')
        else:
            try:
                winreg.DeleteValue(k, APP_KEY)
            except FileNotFoundError:
                pass


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def keep_in_memory():
    """Windows moves a program out of memory (to the disk) when its window is minimised. Coming back,
    it has to read itself back from the disk before it can draw — that's the black boxes you see for a
    moment on a slow laptop. This asks Windows to keep the app's working part in memory (up to a
    sensible amount, smaller on laptops with little memory). Returns the amount kept, in MB, or 0."""
    if not IS_WINDOWS:
        return 0
    try:
        import psutil
        rss = psutil.Process().memory_info().rss
        total = psutil.virtual_memory().total
        floor = int(min(rss, 160 * 2 ** 20, total * 0.06))
        if floor < 32 * 2 ** 20:
            return 0
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.SetProcessWorkingSetSizeEx.argtypes = [wintypes.HANDLE, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        QUOTA_LIMITS_HARDWS_MIN_ENABLE, QUOTA_LIMITS_HARDWS_MAX_DISABLE = 0x1, 0x8
        ok = k32.SetProcessWorkingSetSizeEx(k32.GetCurrentProcess(), floor, max(floor * 4, 512 * 2 ** 20),
                                            QUOTA_LIMITS_HARDWS_MIN_ENABLE | QUOTA_LIMITS_HARDWS_MAX_DISABLE)
        return floor // 2 ** 20 if ok else 0
    except Exception:
        return 0
