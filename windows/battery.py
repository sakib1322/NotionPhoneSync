"""Battery helpers: charge levels of Bluetooth devices, and Dell's battery charging modes
(Standard, ExpressCharge, Primarily AC, Adaptive, Custom start/stop) — the same settings the
Dell Power Manager / Dell Optimizer app changes, set in the laptop's BIOS."""
import json
import os
import subprocess
import tempfile

NO_WINDOW = 0x08000000

MODES = [
    ("Standard", "Standard",
     "Recommended for users who switch between battery power and an external power source.\n\n"
     "Fully charges your battery at a standard rate (not as fast as ExpressCharge)."),
    ("Express", "ExpressCharge™",
     "Charges your battery faster. Best when you often need a quick charge.\n\n"
     "Fast charging can wear the battery a little faster over the years."),
    ("PrimAcUse", "Primarily AC Use",
     "Recommended if you mostly use the laptop plugged in.\n\n"
     "Keeps the battery a little below full so it lasts longer."),
    ("Adaptive", "Adaptive",
     "Windows-style smart charging: the laptop learns how you use it and picks the best charge level."),
    ("Custom", "Custom",
     "For when you want to choose exactly when the battery starts and stops charging.\n\n"
     "Example: start at 50%, stop at 80% — good for a laptop that's plugged in most of the day."),
]
CCTK = [r"C:\Program Files (x86)\Dell\Command Configure\X86_64\cctk.exe",
        r"C:\Program Files\Dell\Command Configure\X86_64\cctk.exe"]


def _ps(script, timeout=60):
    """Runs a PowerShell script (no window) and returns its text output."""
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    return (r.stdout or "").strip()


# ---- Bluetooth devices ----------------------------------------------------------------------------

BT_SCRIPT = r"""
$k = '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2'      # battery level
$c = '{83DA6326-97A6-4088-9453-A1923F573B29} 15'     # connected right now?
$devs = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
  Where-Object { $_.InstanceId -match '^(BTHENUM|BTHLE|BTHLEDEVICE|BTHHFENUM)\\' })
$out = @()
if ($devs.Count) {
  $props = @(Get-PnpDeviceProperty -InstanceId ($devs.InstanceId) -KeyName $k, $c -ErrorAction SilentlyContinue)
  $byId = @{}
  foreach ($p in $props) { if (-not $byId[$p.InstanceId]) { $byId[$p.InstanceId] = @{} }; $byId[$p.InstanceId][$p.KeyName] = $p.Data }
  foreach ($d in $devs) {
    $v = $byId[$d.InstanceId]
    if ($v -and $v[$k] -ne $null) {
      $con = $v[$c]
      if ($con -eq $false) { continue }
      $out += [pscustomobject]@{ n = $d.FriendlyName; b = [int]$v[$k] }
    }
  }
}
ConvertTo-Json -Compress -InputObject @($out)
"""

STRIP = (" Hands-Free AG", " Hands-Free", " Avrcp Transport", " Stereo", " Audio")


def bt_levels():
    """[(device name, battery %)] for connected Bluetooth devices that report their battery."""
    if os.name != "nt":
        return []
    raw = _ps(BT_SCRIPT, timeout=40)
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return []
    if isinstance(items, dict):
        items = [items]
    out, seen = [], set()
    for it in items:
        name = str(it.get("n") or "Bluetooth device")
        for s in STRIP:
            if name.endswith(s):
                name = name[: -len(s)]
        b = it.get("b")
        if name.lower() in seen or not isinstance(b, int) or not 0 <= b <= 100:
            continue
        seen.add(name.lower())
        out.append((name.strip(), b))
    return out


# ---- this laptop's battery ---------------------------------------------------------------------------

def laptop_battery():
    """(percent, plugged in) or None."""
    try:
        import psutil
        b = psutil.sensors_battery()
        return (round(b.percent), bool(b.power_plugged)) if b else None
    except Exception:
        return None


def maker():
    if os.name != "nt":
        return ""
    try:
        return _ps("(Get-CimInstance Win32_ComputerSystem).Manufacturer", timeout=20)
    except Exception:
        return ""


def cctk_path():
    return next((p for p in CCTK if os.path.isfile(p)), None)


READ_SCRIPT = r"""
$ns = 'root\dcim\sysman\biosattributes'
try {
  $m = (Get-CimInstance -Namespace $ns -ClassName EnumerationAttribute -Filter "AttributeName='PrimaryBattChargeCfg'" -ErrorAction Stop).CurrentValue
  $a = (Get-CimInstance -Namespace $ns -ClassName IntegerAttribute -Filter "AttributeName='CustomChargeStart'" -ErrorAction SilentlyContinue).CurrentValue
  $b = (Get-CimInstance -Namespace $ns -ClassName IntegerAttribute -Filter "AttributeName='CustomChargeStop'" -ErrorAction SilentlyContinue).CurrentValue
  ConvertTo-Json -Compress -InputObject @{ mode = "$m"; start = $a; stop = $b }
} catch { ConvertTo-Json -Compress -InputObject @{ error = $_.Exception.Message } }
"""


def dell_read(elevated=False):
    """{'mode': 'Custom', 'start': 50, 'stop': 80} — or {'error': ...} if it can't be read.
    elevated=True asks Windows for permission first (reading the BIOS can need it)."""
    if os.name != "nt":
        return {"error": "Windows only"}
    try:
        if elevated:
            wrapped = ("$msgFile = Join-Path $env:ProgramData 'NotionLaptopSync\\battery-result.txt'\n"
                       "New-Item -ItemType Directory -Force -Path (Split-Path $msgFile) | Out-Null\n"
                       "$r = & {\n" + READ_SCRIPT + "\n}\n$r | Out-File -Encoding utf8 -Force $msgFile\nexit 0\n")
            code, text = _elevated(wrapped, timeout=120)
            if code is None:
                return {"error": "cancelled"}
            d = json.loads(text or "{}")
        else:
            d = json.loads(_ps(READ_SCRIPT, timeout=30) or "{}")
    except Exception as e:
        return {"error": str(e)}
    if d.get("mode"):
        for k in ("start", "stop"):
            try:
                d[k] = int(d[k]) if d.get(k) is not None else None
            except (TypeError, ValueError):
                d[k] = None
    return d


SET_SCRIPT = r"""
$msgFile = Join-Path $env:ProgramData 'NotionLaptopSync\battery-result.txt'
New-Item -ItemType Directory -Force -Path (Split-Path $msgFile) | Out-Null
function Done($code, $text) { try { $text | Out-File -Encoding utf8 -Force $msgFile } catch {}; exit $code }
$mode = '{mode}'; $start = {start}; $stop = {stop}
$pw = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{pwb64}'))
$pwBytes = [Text.Encoding]::UTF8.GetBytes($pw)
$wmiError = ''
try {
  $bai = Get-WmiObject -Namespace root\dcim\sysman\biosattributes -Class BIOSAttributeInterface -ErrorAction Stop
  if (-not $bai) { throw 'no BIOS interface' }
  function S($n, $v) {
    if ($pw) { $r = $bai.SetAttribute(1, $pwBytes.Length, $pwBytes, $n, "$v") }
    else { $r = $bai.SetAttribute(0, 0, 0, $n, "$v") }
    if ($r.Status -ne 0) { throw "status:$($r.Status):$n" }
  }
  if ($mode -eq 'Custom') {
    try { S 'CustomChargeStart' $start; S 'CustomChargeStop' $stop }
    catch { S 'CustomChargeStop' $stop; S 'CustomChargeStart' $start }
  }
  S 'PrimaryBattChargeCfg' $mode
  Done 0 'ok'
} catch { $wmiError = $_.Exception.Message }
$cctkPaths = @('C:\Program Files (x86)\Dell\Command Configure\X86_64\cctk.exe', 'C:\Program Files\Dell\Command Configure\X86_64\cctk.exe')
if (-not ($cctkPaths | Where-Object { Test-Path $_ })) {
  # Plan B: install Dell's own free tool (Dell Command | Configure) quietly with Windows' app installer.
  $wg = Get-Command winget -ErrorAction SilentlyContinue
  if (-not $wg) { $wg = Get-ChildItem "$env:ProgramFiles\WindowsApps\Microsoft.DesktopAppInstaller_*_x64__8wekyb3d8bbwe\winget.exe" -ErrorAction SilentlyContinue | Select-Object -Last 1 }
  if ($wg) {
    $exe = if ($wg.Source) { $wg.Source } else { $wg.FullName }
    & $exe install --id Dell.CommandConfigure -e --silent --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Out-Null
  }
}
foreach ($c in $cctkPaths) {
  if (Test-Path $c) {
    $arg = if ($mode -eq 'Custom') { "--PrimaryBattChargeCfg=Custom:$start-$stop" } else { "--PrimaryBattChargeCfg=$mode" }
    $args2 = @($arg)
    if ($pw) { $args2 += "--ValSetupPwd=$pw" }
    $o = & $c @args2 2>&1 | Out-String
    if ($o -match 'Setup Password is required') { Done 20 'password needed' }
    if ($pw -and $o -match '(?i)password.*(incorrect|invalid|wrong)|(incorrect|invalid).*password') { Done 21 'wrong password' }
    if ($LASTEXITCODE -eq 0) { Done 0 'ok' }
    Done 13 ("Dell Command Configure said: " + $o.Trim())
  }
}
if ($wmiError -like 'status:*') { Done 11 $wmiError }
if ($wmiError -match 'namespace|not found|class') { Done 10 $wmiError }
Done 12 $wmiError
"""


def _elevated(script, timeout=180):
    """Runs a PowerShell script as administrator (Windows asks first) and returns (exit code, message).
    The script is passed encoded, so PowerShell's script-blocking settings don't get in the way."""
    import base64
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    msg_file = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "NotionLaptopSync", "battery-result.txt")
    try:
        os.remove(msg_file)
    except OSError:
        pass
    launcher = ("try { $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden "
                f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{enc}' -ErrorAction Stop; "
                "'code:' + $p.ExitCode } catch { 'cancelled' }")
    out = _ps(launcher, timeout=timeout)
    if "cancelled" in out:
        return None, ""
    try:
        code = int(out.rsplit("code:", 1)[1].strip())
    except (IndexError, ValueError):
        code = -1
    try:
        with open(msg_file, encoding="utf-8-sig") as f:
            text = f.read().strip()
    except OSError:
        text = out
    return code, text


def dell_set(mode, start=None, stop=None, password=None):
    """Changes the charging mode in the BIOS. Windows asks for permission (it's a system setting).
    Returns None when done, or a message saying what went wrong."""
    if mode not in [m[0] for m in MODES]:
        return "Unknown mode."
    start, stop = int(start or 50), int(stop or 100)
    if mode == "Custom" and not (50 <= start <= 95 and 55 <= stop <= 100 and stop - start >= 5):
        return "Start must be 50–95%, stop 55–100%, and stop at least 5% above start."
    import base64
    pwb64 = base64.b64encode((password or "").encode("utf-8")).decode()
    script = (SET_SCRIPT.replace("{mode}", mode).replace("{start}", str(start)).replace("{stop}", str(stop))
              .replace("{pwb64}", pwb64))
    try:
        code, text = _elevated(script, timeout=900)
    except Exception as e:
        return f"It didn't run ({e})."
    dell_set.last = (code, text)
    if code is None:
        return "You said No to Windows' permission prompt, so nothing was changed."
    if code == 0:
        return None
    if code == 10:
        return ("This laptop's BIOS doesn't let apps change it directly, and Dell's free helper "
                "“Dell Command | Configure” couldn't be installed automatically (no internet?). Install it from "
                "dell.com/support (search “Dell Command Configure”) and press Apply again — the app uses it by itself.")
    if code == 20:
        return "PASSWORD"
    if code == 21:
        return "That BIOS setup password isn't right. Check it and press Apply again."
    if code == 11:
        if ":2:" in text or ":6:" in text:
            return "PASSWORD" if not password else "That BIOS setup password isn't right. Check it and press Apply again."
        return f"The BIOS refused the change ({text})."
    return f"It didn't work: {text or 'no details'} (code {code})."


# ---- remembering the BIOS password (encrypted by Windows for this user only) -----------------------------

def protect(text):
    """Encrypts with Windows (DPAPI): only this Windows user on this laptop can read it back."""
    import base64
    import ctypes
    from ctypes import wintypes as wt

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    data = text.encode("utf-8")
    src = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("couldn't encrypt")
    try:
        return base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def unprotect(b64):
    import base64
    import ctypes
    from ctypes import wintypes as wt

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    try:
        data = base64.b64decode(b64)
        src = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
        out = BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
            return ""
        try:
            return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        return ""


def open_dell_app():
    """Opens Dell Power Manager (or Dell Optimizer) if it's installed. Returns True if it was found."""
    out = _ps("$a = Get-StartApps | Where-Object { $_.Name -match 'Power Manager|Dell Optimizer' } | Select-Object -First 1; "
              "if ($a) { Start-Process ('shell:AppsFolder\\' + $a.AppID); 'ok' }", timeout=30)
    return "ok" in out


WAKE_SCRIPT = r"""
$out = Join-Path $env:ProgramData 'NotionLaptopSync\battery-result.txt'
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null
$done = @()
foreach ($d in (powercfg /devicequery wake_armed)) {
  $d = "$d".Trim()
  if ($d -and $d -ne 'NONE') { powercfg /devicedisablewake "$d" | Out-Null; $done += $d }
}
($done -join "`n") | Out-File -Encoding utf8 -Force $out
exit 0
"""


def stop_wake_devices():
    """Stops mice, keyboards, USB dongles and network cards from waking the laptop up (and turns
    nothing else). Asks Windows for permission. Returns (list of devices changed, error or None)."""
    try:
        code, text = _elevated(WAKE_SCRIPT, timeout=120)
    except Exception as e:
        return [], str(e)
    if code is None:
        return [], "You said No to Windows' permission prompt."
    if code != 0:
        return [], text or f"code {code}"
    return [l.strip() for l in (text or "").splitlines() if l.strip()], None


def awake_reasons():
    """What's keeping the laptop awake right now (Windows' own list; needs admin, so may be empty)."""
    try:
        return _ps("powercfg /requests", timeout=20)
    except Exception:
        return ""


BLOCKERS_SCRIPT = r"""
$out = Join-Path $env:ProgramData 'NotionLaptopSync\battery-result.txt'
New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null
$done = @()
foreach ($line in (powercfg /requests)) {
  $l = "$line".Trim()
  if ($l -match '^\[PROCESS\]\s+(.+)$') {
    $name = Split-Path $matches[1] -Leaf
    powercfg /requestsoverride PROCESS "$name" DISPLAY SYSTEM AWAYMODE EXECUTION | Out-Null; $done += $name
  } elseif ($l -match '^\[SERVICE\]\s+.*\((.+)\)\s*$') {
    $name = $matches[1]
    powercfg /requestsoverride SERVICE "$name" DISPLAY SYSTEM AWAYMODE EXECUTION | Out-Null; $done += "service $name"
  } elseif ($l -match '^\[DRIVER\]\s+(.+)$') {
    $name = $matches[1]
    powercfg /requestsoverride DRIVER "$name" DISPLAY SYSTEM AWAYMODE EXECUTION | Out-Null; $done += $name
  }
}
(($done | Select-Object -Unique) -join "`n") | Out-File -Encoding utf8 -Force $out
exit 0
"""


def stop_awake_blockers():
    """Tells Windows to ignore the apps/drivers that are keeping the laptop awake right now (browsers,
    Claude, audio dongles…), so it sleeps on its own again. Asks permission. Returns (names, error)."""
    try:
        code, text = _elevated(BLOCKERS_SCRIPT, timeout=120)
    except Exception as e:
        return [], str(e)
    if code is None:
        return [], "You said No to Windows' permission prompt."
    if code != 0:
        return [], text or f"code {code}"
    return [l.strip() for l in (text or "").splitlines() if l.strip()], None


# ---- hibernate without keeping the big hibernate file ---------------------------------------------------
# Windows' hibernate file (hiberfil.sys) takes about 40% of your RAM on drive C: all the time. Here it only
# exists while the laptop is hibernating: it's switched on just before, and off again (file deleted) as soon
# as the laptop wakes. Two small scheduled tasks do the admin-only parts, so Windows asks permission once.

HIB_TASK, HIB_CLEAN = "NotionSync Hibernate", "NotionSync Hibernate Cleanup"

HIB_SETUP = r"""
$ErrorActionPreference = 'Stop'
try {
  $u = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
  $p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Highest
  $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
  $a1 = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -WindowStyle Hidden -Command "powercfg /hibernate on; Start-Sleep -Seconds 2; shutdown.exe /h"'
  Register-ScheduledTask -TaskName 'NotionSync Hibernate' -Action $a1 -Principal $p -Settings $s -Force | Out-Null
  $cls = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
  $t = New-CimInstance -CimClass $cls -ClientOnly
  $t.Enabled = $true
  $t.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name=''Microsoft-Windows-Power-Troubleshooter''] and EventID=1]]</Select></Query></QueryList>'
  $t2 = New-ScheduledTaskTrigger -AtLogOn
  $a2 = New-ScheduledTaskAction -Execute 'powercfg.exe' -Argument '/hibernate off'
  Register-ScheduledTask -TaskName 'NotionSync Hibernate Cleanup' -Action $a2 -Trigger @($t, $t2) -Principal $p -Settings $s -Force | Out-Null
  powercfg /hibernate off
  exit 0
} catch {
  $out = Join-Path $env:ProgramData 'NotionLaptopSync\battery-result.txt'
  New-Item -ItemType Directory -Force -Path (Split-Path $out) | Out-Null
  $_.Exception.Message | Out-File -Encoding utf8 -Force $out
  exit 12
}
"""


def _task_exists(name):
    r = subprocess.run(["schtasks", "/query", "/tn", name], capture_output=True, text=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def hib_space_needed():
    """(bytes needed for the hibernate file, bytes free on C:)"""
    import shutil
    try:
        import psutil
        ram = psutil.virtual_memory().total
    except Exception:
        ram = 8 * 1024 ** 3
    free = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\").free
    return int(ram * 0.42), free


def hibernate_now():
    """Hibernates now, creating the hibernate file just for this time. Returns None or an error message."""
    need, free = hib_space_needed()
    hiber = os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "hiberfil.sys")
    have_file = os.path.exists(hiber)
    if not have_file and free < need + 512 * 1024 ** 2:
        gb = lambda b: f"{b / 1024 ** 3:.1f} GB"
        return (f"Hibernating needs about {gb(need)} free on drive C: for a moment (it's deleted again when the "
                f"laptop wakes), but only {gb(free)} is free.")
    if not (_task_exists(HIB_TASK) and _task_exists(HIB_CLEAN)):
        code, text = _elevated(HIB_SETUP, timeout=120)
        if code is None:
            return "You said No to Windows' permission prompt (it's only asked once)."
        if code != 0:
            return f"Couldn't set it up: {text or code}"
    r = subprocess.run(["schtasks", "/run", "/tn", HIB_TASK], capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        return f"Couldn't start hibernating: {(r.stderr or r.stdout).strip()}"
    return None


def hibernate_file_off():
    """Deletes a hibernate file left from before (frees its space). Asks permission."""
    code, text = _elevated("powercfg /hibernate off\nexit $LASTEXITCODE\n", timeout=60)
    return None if code == 0 else ("You said No to Windows' prompt." if code is None else f"Couldn't: {text or code}")
