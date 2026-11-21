"""Live host metrics for the dashboard: CPU, temperatures, memory, storage. Linux /proc and /sys only."""
import os
import shutil
import threading
import time
from pathlib import Path

from .util import uptime_seconds

# hwmon driver name -> (display name, preferred sensor labels, default warn °C, default crit °C)
SENSORS = {
    "k10temp": ("CPU", ("Tctl", "Tdie"), 85, 95),
    "zenpower": ("CPU", ("Tdie", "Tctl"), 85, 95),
    "coretemp": ("CPU", ("Package id 0",), 85, 95),
    "cpu_thermal": ("CPU", (), 80, 90),
    "amdgpu": ("GPU", ("edge",), 85, 95),
    "radeon": ("GPU", (), 85, 95),
    "nouveau": ("GPU", (), 85, 95),
    "nvme": ("NVMe SSD", ("Composite",), 70, 80),
    "drivetemp": ("Drive", (), 50, 60),
}


def _read(p, default=None):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return default


class Metrics:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cpu_pct = None
        self.history = []  # (ts, cpu%) for the last ~10 minutes
        self.model = next((l.split(":", 1)[1].strip() for l in (_read("/proc/cpuinfo", "") or "").splitlines()
                           if l.startswith("model name")), None)
        threading.Thread(target=self._sampler, daemon=True).start()

    def _cpu_times(self):
        f = (_read("/proc/stat", "") or "").splitlines()[0].split()[1:]
        v = [int(x) for x in f]
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return sum(v[:8]), idle

    def _sampler(self):
        prev = self._cpu_times()
        while True:
            time.sleep(5)
            try:
                cur = self._cpu_times()
                dt, di = cur[0] - prev[0], cur[1] - prev[1]
                prev = cur
                if dt > 0:
                    self.cpu_pct = round(100 * (dt - di) / dt, 1)
                    self.history = (self.history + [(int(time.time()), self.cpu_pct)])[-120:]
            except Exception:
                pass

    def temps(self):
        out = []
        for h in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
            name = _read(h / "name", "")
            if name not in SENSORS:
                continue
            label, prefer, warn, crit = SENSORS[name]
            readings = {}
            for f in h.glob("temp*_input"):
                base = f.name[:-len("_input")]
                v = _read(f)
                if v and v.lstrip("-").isdigit():
                    readings[_read(h / f"{base}_label", base)] = (int(v) / 1000, base)
            if not readings:
                continue
            key = next((k for k in prefer if k in readings), None) or max(readings, key=lambda k: readings[k][0])
            c, base = readings[key]
            hw_crit = _read(h / f"{base}_crit")
            if hw_crit and hw_crit.isdigit() and 40000 < int(hw_crit) < 130000:
                crit = int(hw_crit) / 1000
                warn = min(warn, crit - 10)
            out.append({"name": label, "c": round(c, 1), "warn": warn, "crit": crit})
        # stable order: CPU, GPU, NVMe, drives; number duplicates
        order = {"CPU": 0, "GPU": 1, "NVMe SSD": 2, "Drive": 3}
        out.sort(key=lambda t: order.get(t["name"], 9))
        seen = {}
        for t in out:
            seen[t["name"]] = seen.get(t["name"], 0) + 1
        idx = {}
        for t in out:
            if seen[t["name"]] > 1:
                idx[t["name"]] = idx.get(t["name"], 0) + 1
                t["name"] += f" {idx[t['name']]}"
        return out

    def memory(self):
        mi = {}
        for l in (_read("/proc/meminfo", "") or "").splitlines():
            k, _, v = l.partition(":")
            mi[k] = int(v.split()[0]) * 1024 if v.split() else 0
        total, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
        st, sf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
        return {"total": total, "used": total - avail, "available": avail,
                "pct": round(100 * (total - avail) / total, 1) if total else None,
                "swap_total": st, "swap_used": st - sf}

    def disks(self):
        c = self.cfg["checks"]
        paths = list(dict.fromkeys(c.get("disk_paths", ["/"]) + c.get("mounts", [])))
        devs = {}
        for l in (_read("/proc/mounts", "") or "").splitlines():
            parts = l.split()
            if len(parts) > 2:
                devs[parts[1]] = (parts[0], parts[2])
        out = []
        for p in paths:
            mounted = p == "/" or os.path.ismount(p)
            d = {"path": p, "mounted": mounted, "device": devs.get(p, (None, None))[0],
                 "fs": devs.get(p, (None, None))[1]}
            if mounted:
                try:
                    u = shutil.disk_usage(p)
                    d.update(total=u.total, used=u.used, free=u.free, pct=round(100 * u.used / u.total, 1))
                except OSError:
                    d["mounted"] = False
            out.append(d)
        return {"items": out, "warn": c.get("disk_warn", 85), "crit": c.get("disk_crit", 95)}

    def snapshot(self):
        l1, l5, l15 = os.getloadavg()
        return {
            "cpu": {"pct": self.cpu_pct, "load": [round(l1, 2), round(l5, 2), round(l15, 2)],
                    "cores": os.cpu_count(), "model": self.model, "history": [h[1] for h in self.history]},
            "temps": self.temps(),
            "memory": self.memory(),
            "disks": self.disks(),
            "uptime": int(uptime_seconds()),
        }
