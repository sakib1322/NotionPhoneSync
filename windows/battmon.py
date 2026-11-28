"""Battery usage per app (laptop): how much of your battery each app uses — watts, % per minute,
% per hour and how much it has used since the app started — plus battery health.

How it's worked out: Windows tells us how fast the battery is draining (watts). That drain is shared
out between the apps by how much processor (CPU) and graphics (GPU) work each one is doing; a part is
kept for the screen, Wi-Fi and Windows itself, which run whatever apps are open. Windows' own Settings
page uses the same idea.
"""
import json
import os
import re
import subprocess
import threading
import time

NO_WINDOW = 0x08000000

FRIENDLY = {
    "msedge": "Microsoft Edge", "chrome": "Google Chrome", "firefox": "Firefox", "brave": "Brave", "opera": "Opera",
    "whatsapp": "WhatsApp", "whatsapp.root": "WhatsApp", "telegram": "Telegram", "slack": "Slack", "discord": "Discord",
    "teams": "Microsoft Teams", "ms-teams": "Microsoft Teams", "zoom": "Zoom", "outlook": "Outlook",
    "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint", "onenote": "OneNote", "code": "VS Code",
    "explorer": "File Explorer", "notionlaptopsync": "Notion Laptop Sync (this app)", "notion": "Notion",
    "claude": "Claude", "spotify": "Spotify", "vlc": "VLC", "acrord32": "Adobe Acrobat", "acrobat": "Adobe Acrobat",
    "searchhost": "Windows Search", "searchindexer": "Windows Search indexing", "msmpeng": "Windows Security (antivirus)",
    "dwm": "Windows screen drawing (DWM)", "csrss": "Windows", "system": "Windows (system)", "svchost": "Windows services",
    "audiodg": "Windows audio", "phoneexperiencehost": "Phone Link", "onedrive": "OneDrive", "steam": "Steam",
    "python": "Python", "pythonw": "Python", "powershell": "PowerShell", "windowsterminal": "Terminal",
    "applicationframehost": "Windows apps", "textinputhost": "Windows keyboard", "shellexperiencehost": "Windows Start/taskbar",
    "startmenuexperiencehost": "Windows Start menu", "widgets": "Windows Widgets", "runtimebroker": "Windows apps (broker)",
}
HIDE = {"idle", "system idle process", "memory compression", "registry", "secure system"}


def friendly(name):
    base = name.lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return FRIENDLY.get(base, name[:-4] if name.lower().endswith(".exe") else name)


def _ps(script, timeout=30):
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
    return (r.stdout or "").strip()


def health():
    """Battery health from Windows' battery report: {design_wh, full_wh, health_pct, cycles, name, maker}."""
    import tempfile
    out = os.path.join(tempfile.gettempdir(), "nls-battery-report.xml")
    try:
        subprocess.run(["powercfg", "/batteryreport", "/xml", "/output", out], capture_output=True, timeout=40,
                       creationflags=NO_WINDOW)
        x = open(out, encoding="utf-8", errors="replace").read()
    except Exception:
        return {}

    def tag(t):
        m = re.search(rf"<{t}>([^<]*)</{t}>", x)
        return m.group(1).strip() if m else ""
    try:
        design = int(tag("DesignCapacity") or 0) / 1000
        full = int(tag("FullChargeCapacity") or 0) / 1000
    except ValueError:
        design = full = 0
    cyc = tag("CycleCount")
    return {"design_wh": design, "full_wh": full,
            "health_pct": round(full / design * 100, 1) if design else None,
            "cycles": int(cyc) if cyc.isdigit() else None,
            "name": tag("Id") or tag("DeviceName"), "maker": tag("Manufacturer"), "chem": tag("Chemistry")}


# Windows facts we can't get from Python directly: the battery drain and graphics (GPU) use per app.
# One small PowerShell runs while you look at the page and prints a line every few seconds.
LIVE = r"""
$ErrorActionPreference = 'SilentlyContinue'
while ($true) {
  $b = Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus | Select-Object -First 1
  $gpu = @{}
  $c = Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue
  if ($c) { foreach ($s in $c.CounterSamples) {
    if ($s.InstanceName -match 'pid_(\d+)_' -and $s.CookedValue -gt 0) { $p = $matches[1]; $gpu[$p] = [double]$gpu[$p] + $s.CookedValue } } }
  $o = @{ t = [DateTimeOffset]::Now.ToUnixTimeMilliseconds();
          dis = $b.DischargeRate; chg = $b.ChargeRate; rem = $b.RemainingCapacity; on = $b.PowerOnline; volt = $b.Voltage;
          gpu = $gpu }
  [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress -Depth 3))
  [Console]::Out.Flush()
  Start-Sleep -Seconds 3
}
"""


class Monitor:
    """Samples every few seconds; keeps per-app totals since the app started."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []            # latest per-app list (dicts)
        self.summary = {}
        self.used_wh = {}         # app -> energy used since start (Wh)
        self.used_total = 0.0
        self.started = time.time()
        self.prev = {}            # pid -> (cpu seconds, name)
        self.prev_t = None
        self.live = {}            # last line from the PowerShell helper
        self.helper = None
        self.visible = False
        self.health = {}
        self.base_w = 3.0         # screen, Wi-Fi and Windows itself (refined as we learn the laptop)
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=lambda: setattr(self, "health", health()), daemon=True).start()

    # the PowerShell helper runs only while the page is open (it costs a little battery itself)

    def set_visible(self, on):
        self.visible = on
        if on and self.helper is None and os.name == "nt":
            try:
                self.helper = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", LIVE],
                                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                               creationflags=NO_WINDOW)
                threading.Thread(target=self._read_helper, args=(self.helper,), daemon=True).start()
            except Exception:
                self.helper = None
        elif not on and self.helper is not None:
            try:
                self.helper.kill()
            except Exception:
                pass
            self.helper = None

    def _read_helper(self, proc):
        for line in proc.stdout:
            try:
                self.live = json.loads(line)
            except ValueError:
                pass

    def stop(self):
        self.set_visible(False)

    def _loop(self):
        import psutil
        ncpu = psutil.cpu_count() or 1
        while True:
            try:
                self._sample(psutil, ncpu)
            except Exception:
                pass
            time.sleep(3 if self.visible else 30)    # less often while its page is closed

    def _sample(self, psutil, ncpu):
        now = time.time()
        cur = {}
        for p in psutil.process_iter(["pid", "name", "cpu_times"]):
            try:
                ct = p.info["cpu_times"]
                if ct is None:
                    continue
                cur[p.info["pid"]] = (ct.user + ct.system, p.info["name"] or "?")
            except Exception:
                continue
        dt = (now - self.prev_t) if self.prev_t else None
        per_cpu = {}
        if dt and dt > 0.5:
            for pid, (cs, name) in cur.items():
                old = self.prev.get(pid)
                if old and old[1] == name:
                    d = max(0.0, cs - old[0])
                    if d > 0 and name.lower() not in HIDE:
                        per_cpu[name] = per_cpu.get(name, 0.0) + d / (dt * ncpu) * 100
        pid_name = {pid: n for pid, (_, n) in cur.items()}
        self.prev, self.prev_t = cur, now
        if not per_cpu and dt is None:
            return
        # graphics use per app (only while the page is open)
        live = self.live if self.live and now * 1000 - (self.live.get("t") or 0) < 15000 else {}
        per_gpu = {}
        for pid, v in (live.get("gpu") or {}).items():
            n = pid_name.get(int(pid))
            if n and n.lower() not in HIDE:
                per_gpu[n] = per_gpu.get(n, 0.0) + float(v)
        # battery
        bat = None
        try:
            b = psutil.sensors_battery()
            bat = (b.percent, b.power_plugged, b.secsleft) if b else None
        except Exception:
            pass
        full_wh = (self.health.get("full_wh") or 0) or 45.0
        dis_mw = live.get("dis") or 0
        on_battery = bool(bat) and not bat[1]
        cpu_total = sum(per_cpu.values())
        gpu_total = min(100.0, sum(per_gpu.values()))
        measured = on_battery and dis_mw and dis_mw > 300
        if measured:
            total_w = dis_mw / 1000
            # learn this laptop's base power: the lowest drain seen while nearly idle
            if cpu_total < 4 and gpu_total < 5:
                self.base_w = max(1.0, min(self.base_w * 0.8 + total_w * 0.2, total_w * 0.9))
        else:
            total_w = self.base_w + cpu_total / 100 * 18 + gpu_total / 100 * 8     # a typical thin laptop
        base = min(self.base_w, total_w * 0.85)
        active = max(0.0, total_w - base)
        weights = {}
        for n in set(per_cpu) | set(per_gpu):
            c = per_cpu.get(n, 0) / cpu_total if cpu_total > 0 else 0
            g = per_gpu.get(n, 0) / gpu_total if gpu_total > 0 else 0
            weights[n] = (0.75 * c + 0.25 * g) if gpu_total > 0 else c
        groups = {}
        for n, w in weights.items():
            f = friendly(n)
            g = groups.setdefault(f, {"app": f, "cpu": 0.0, "gpu": 0.0, "w": 0.0, "procs": 0})
            g["cpu"] += per_cpu.get(n, 0)
            g["gpu"] += per_gpu.get(n, 0)
            g["w"] += active * w
            g["procs"] += 1
        groups["Screen, Wi-Fi & Windows (base)"] = {"app": "Screen, Wi-Fi & Windows (base)", "cpu": 0.0, "gpu": 0.0,
                                                   "w": base, "procs": 0}
        step = dt or 0
        with self.lock:
            for g in groups.values():
                wh = g["w"] * step / 3600
                self.used_wh[g["app"]] = self.used_wh.get(g["app"], 0.0) + wh
            self.used_total += total_w * step / 3600
            rows = []
            for g in groups.values():
                pct_min = g["w"] / full_wh / 60 * 100
                rows.append(dict(g, pct_min=pct_min, pct_hour=pct_min * 60,
                                 used_pct=self.used_wh.get(g["app"], 0) / full_wh * 100))
            rows.sort(key=lambda r: -r["w"])
            self.rows = rows
            self.summary = {
                "pct": bat[0] if bat else None, "plugged": bat[1] if bat else None,
                "secsleft": bat[2] if bat else None, "watts": total_w, "measured": bool(measured),
                "pct_min": total_w / full_wh / 60 * 100, "cpu": cpu_total, "gpu": gpu_total,
                "full_wh": full_wh, "rem_wh": (live.get("rem") or 0) / 1000 or None,
                "charge_w": (live.get("chg") or 0) / 1000 or None, "since": self.started,
                "used_pct": self.used_total / full_wh * 100,
            }

    def snapshot(self):
        with self.lock:
            return list(self.rows), dict(self.summary), dict(self.health)
