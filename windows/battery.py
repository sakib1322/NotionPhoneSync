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
$k = '{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2'
$out = @()
Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
  Where-Object { $_.InstanceId -match '^(BTHENUM|BTHLE|BTHLEDEVICE)\\' -and $_.Status -eq 'OK' } |
  ForEach-Object {
    $p = Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName $k -ErrorAction SilentlyContinue
    if ($p -and $p.Data -ne $null) { $out += [pscustomobject]@{ n = $_.FriendlyName; b = [int]$p.Data } }
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
$wmiError = ''
try {
  $bai = Get-WmiObject -Namespace root\dcim\sysman\biosattributes -Class BIOSAttributeInterface -ErrorAction Stop
  if (-not $bai) { throw 'no BIOS interface' }
  function S($n, $v) {
    $r = $bai.SetAttribute(0, 0, 0, $n, "$v")
    if ($r.Status -ne 0) { throw "status:$($r.Status):$n" }
  }
  if ($mode -eq 'Custom') {
    try { S 'CustomChargeStart' $start; S 'CustomChargeStop' $stop }
    catch { S 'CustomChargeStop' $stop; S 'CustomChargeStart' $start }
  }
  S 'PrimaryBattChargeCfg' $mode
  Done 0 'ok'
} catch { $wmiError = $_.Exception.Message }
foreach ($c in @('C:\Program Files (x86)\Dell\Command Configure\X86_64\cctk.exe', 'C:\Program Files\Dell\Command Configure\X86_64\cctk.exe')) {
  if (Test-Path $c) {
    $arg = if ($mode -eq 'Custom') { "--PrimaryBattChargeCfg=Custom:$start-$stop" } else { "--PrimaryBattChargeCfg=$mode" }
    $o = & $c $arg 2>&1 | Out-String
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


def dell_set(mode, start=None, stop=None):
    """Changes the charging mode in the BIOS. Windows asks for permission (it's a system setting).
    Returns None when done, or a message saying what went wrong."""
    if mode not in [m[0] for m in MODES]:
        return "Unknown mode."
    start, stop = int(start or 50), int(stop or 100)
    if mode == "Custom" and not (50 <= start <= 95 and 55 <= stop <= 100 and stop - start >= 5):
        return "Start must be 50–95%, stop 55–100%, and stop at least 5% above start."
    script = SET_SCRIPT.replace("{mode}", mode).replace("{start}", str(start)).replace("{stop}", str(stop))
    try:
        code, text = _elevated(script)
    except Exception as e:
        return f"It didn't run ({e})."
    dell_set.last = (code, text)
    if code is None:
        return "You said No to Windows' permission prompt, so nothing was changed."
    if code == 0:
        return None
    if code == 10:
        return ("This laptop's BIOS doesn't let apps change the charging mode directly. Install Dell's free "
                "“Dell Command | Configure” (from dell.com/support) and press Apply again — the app uses it automatically.")
    if code == 11:
        if ":2:" in text or ":6:" in text:
            return "The BIOS has an admin (setup) password, so it refused the change."
        return f"The BIOS refused the change ({text})."
    return f"It didn't work: {text or 'no details'} (code {code})."
