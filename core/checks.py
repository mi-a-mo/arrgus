"""Deterministic health checks. Each returns result dicts:
{id, group, name, status: ok|warn|fail, detail, fix: {kind,...}|None, container}
"""
import json
import os
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse

from .util import sh

CURL_ERR = {6: "DNS lookup failed", 7: "connection refused", 28: "timed out",
            35: "TLS handshake failed", 52: "empty reply", 56: "connection reset",
            60: "TLS certificate invalid"}


def result(id, group, name, status, detail="", fix=None, container=None):
    return {"id": id, "group": group, "name": name, "status": status,
            "detail": detail, "fix": fix, "container": container}


def docker_inventory():
    rc, out, err = sh(["docker", "ps", "-aq"], timeout=30)
    if rc:
        return None, err or "docker ps failed"
    ids = out.split()
    if not ids:
        return {}, None
    rc, out, err = sh(["docker", "inspect", *ids], timeout=60)
    if rc:
        return None, err
    inv = {}
    for c in json.loads(out):
        st = c["State"]
        labels = c["Config"].get("Labels") or {}
        name = c["Name"].lstrip("/")
        inv[name] = {
            "name": name,
            "service": labels.get("com.docker.compose.service"),
            "status": st.get("Status"),
            "running": bool(st.get("Running")) and not st.get("Restarting"),
            "health": (st.get("Health") or {}).get("Status"),
            "restart_count": c.get("RestartCount", 0),
            "started_at": st.get("StartedAt"),
            "exit_code": st.get("ExitCode"),
            "oom": st.get("OOMKilled"),
            "error": st.get("Error"),
            "image": c["Config"].get("Image"),
            "image_id": c.get("Image"),
            "binds": [(m["Source"], m["Destination"]) for m in c.get("Mounts", []) if m.get("Type") == "bind"],
            "id": c.get("Id"),
            "network_mode": (c.get("HostConfig") or {}).get("NetworkMode"),
            # {"8989/tcp": [("0.0.0.0", "8989"), ...]} — only published ports
            "ports": {k: [(b.get("HostIp", ""), b.get("HostPort", "")) for b in v]
                      for k, v in ((c.get("NetworkSettings") or {}).get("Ports") or {}).items() if v},
        }
    return inv, None


def http_probe(url, via=None, resolve=None, timeout=10):
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", str(timeout)]
    if resolve:
        u = urlparse(url)
        cmd += ["--resolve", f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}:{resolve}"]
    cmd.append(url)
    if via:
        cmd = ["docker", "exec", via] + cmd
    rc, out, err = sh(cmd, timeout=timeout + 10)
    code = int(out) if out.isdigit() else 0
    return code, rc, err


class Checker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.box = cfg["box"]
        self.c = cfg["checks"]
        self.prev_restarts = {}

    # ---- compose ---------------------------------------------------------
    def compose(self, *args, timeout=600):
        return sh(["docker", "compose", "-f", self.box.get("compose_file", "docker-compose.yml"), *args],
                  cwd=self.box["compose_dir"], timeout=timeout)

    def services(self):
        rc, out, err = self.compose("config", "--format", "json", timeout=60)
        if rc:
            return None, err
        d = json.loads(out)
        project = d.get("name", "")
        svcs = {}
        for k, v in d.get("services", {}).items():
            svcs[k] = {
                "container": v.get("container_name") or f"{project}-{k}-1",
                "image": v.get("image"),
                "build": bool(v.get("build")),
                "network_mode": v.get("network_mode"),
            }
        return svcs, None

    # ---- main ------------------------------------------------------------
    def run(self):
        out = []
        out += self.check_network()
        out += self.check_system()
        inv, err = docker_inventory()
        if inv is None:
            out.append(result("docker:daemon", "docker", "Docker daemon", "fail", err,
                              fix={"kind": "systemctl", "unit": "docker"}))
            return out, {}, {}
        out.append(result("docker:daemon", "docker", "Docker daemon", "ok", f"{len(inv)} containers"))
        svcs, err = self.services()
        if svcs is None:
            out.append(result("docker:compose", "docker", "Compose file", "fail", f"compose config invalid: {err}",
                              fix={"kind": "ai"}))
            svcs = {}
        out += self.check_containers(inv, svcs)
        out += self.check_vpn(inv)
        out += self.check_http(inv)
        out += self.check_storage(inv)
        return out, inv, svcs

    def check_containers(self, inv, svcs):
        out = []
        ignore = set(self.c.get("ignore_containers", []))
        for svc, s in svcs.items():
            cname = s["container"]
            if svc in ignore or cname in ignore:
                continue
            cid = f"container:{svc}"
            c = inv.get(cname)
            if not c:
                out.append(result(cid, "containers", svc, "fail", "container does not exist",
                                  fix={"kind": "compose_up", "service": svc}, container=cname))
                continue
            prev = self.prev_restarts.get(cname)
            self.prev_restarts[cname] = c["restart_count"]
            if c["status"] == "restarting" or (prev is not None and c["restart_count"] - prev >= 2):
                out.append(result(cid, "containers", svc, "fail",
                                  f"crash loop (restart count {c['restart_count']}, exit {c['exit_code']})",
                                  fix={"kind": "ai"}, container=cname))
            elif not c["running"]:
                d = f"{c['status']} (exit code {c['exit_code']})"
                if c["oom"]:
                    d += ", killed: out of memory"
                if c["error"]:
                    d += f": {c['error']}"
                out.append(result(cid, "containers", svc, "fail", d,
                                  fix={"kind": "compose_up", "service": svc}, container=cname))
            elif c["health"] == "unhealthy":
                out.append(result(cid, "containers", svc, "fail", "running but healthcheck reports unhealthy",
                                  fix={"kind": "restart", "container": cname}, container=cname))
            else:
                d = "running" + (f", {c['health']}" if c["health"] else "")
                out.append(result(cid, "containers", svc, "ok", d, container=cname))
        # containers not managed by the compose file
        known = {s["container"] for s in svcs.values()}
        for name, c in inv.items():
            if name not in known and name not in ignore:
                out.append(result(f"container:{name}", "containers", f"{name} (unmanaged)",
                                  "ok" if c["running"] else "warn", c["status"], container=name))
        return out

    def _ns(self, container):
        rc, out, _ = sh(["docker", "exec", container, "readlink", "/proc/1/ns/net"], timeout=15)
        return out if rc == 0 else None

    def check_vpn(self, inv):
        vpn = self.c.get("vpn")
        if not vpn:
            return []
        out = []
        name = vpn["container"]
        g = inv.get(name)
        if not g or not g["running"]:
            return out  # container check already reports it
        gns = self._ns(name)
        for dep in vpn.get("dependents", []):
            d = inv.get(dep)
            if not d or not d["running"]:
                continue
            ok = gns and self._ns(dep) == gns
            out.append(result(f"vpn:netns:{dep}", "vpn", f"{dep} routed through VPN",
                              "ok" if ok else "fail",
                              "shares VPN network namespace" if ok else "stale network namespace (VPN restarted underneath it)",
                              fix=None if ok else {"kind": "reattach_vpn", "container": dep}, container=dep))
        probe = vpn.get("probe_container")
        if probe and inv.get(probe, {}).get("running"):
            ip_url = vpn.get("ip_url", "https://api.ipify.org")
            rc, egress, _ = sh(["docker", "exec", probe, "curl", "-s", "-m", "15", ip_url], timeout=25)
            rc2, home, _ = sh(["curl", "-s", "-m", "10", ip_url], timeout=15)
            egress, home = egress.strip(), home.strip()
            if rc or not egress:
                out.append(result("vpn:egress", "vpn", "VPN tunnel internet", "fail",
                                  "no internet access through the VPN tunnel",
                                  fix={"kind": "restart_vpn"}, container=name))
            elif home and egress == home:
                out.append(result("vpn:egress", "vpn", "VPN tunnel internet", "fail",
                                  "VPN LEAK: torrent traffic exits via the home IP",
                                  fix={"kind": "vpn_leak"}, container=name))
            else:
                out.append(result("vpn:egress", "vpn", "VPN tunnel internet", "ok",
                                  "traffic exits via VPN (IP differs from home)", container=name))
        return out

    def check_http(self, inv):
        out = []
        for h in self.c.get("http", []):
            cname = h.get("container")
            via = h.get("via")
            if (cname and not inv.get(cname, {}).get("running")) or (via and not inv.get(via, {}).get("running")):
                continue  # container check reports the root cause
            code, rc, err = http_probe(h["url"], via=via, resolve=h.get("resolve"), timeout=h.get("timeout", 10))
            ok = 200 <= code < h.get("max_status", 500)
            if ok:
                detail = f"HTTP {code}"
            elif code:
                detail = f"HTTP {code}"
            else:
                detail = CURL_ERR.get(rc, f"curl error {rc}") + (f" ({err[:120]})" if err else "")
            out.append(result(f"http:{h['name']}", h.get("group", "services"), h["name"],
                              "ok" if ok else "fail", detail,
                              fix=None if ok or not cname else {"kind": "restart", "container": cname},
                              container=cname))
        return out

    def check_storage(self, inv):
        out = []
        mounts = self.c.get("mounts", [])
        mounted = {}
        for m in mounts:
            ok = os.path.ismount(m)
            mounted[m] = ok
            out.append(result(f"mount:{m}", "storage", f"{m} mounted", "ok" if ok else "fail",
                              "mounted" if ok else "drive is NOT mounted",
                              fix=None if ok else {"kind": "mount", "path": m}))
        # containers that started before a drive mounted see an empty directory
        for name, c in inv.items():
            if not c["running"]:
                continue
            for src, dst in c["binds"]:
                base = next((m for m in mounts if src == m or src.startswith(m.rstrip("/") + "/")), None)
                if not base or not mounted.get(base):
                    continue
                try:
                    host_dev = str(os.stat(src).st_dev)
                except OSError:
                    continue
                rc, cdev, _ = sh(["docker", "exec", name, "stat", "-c", "%d", dst], timeout=15)
                if rc or not cdev.isdigit():
                    continue
                ok = cdev == host_dev
                out.append(result(f"bind:{name}:{dst}", "storage", f"{name} sees {src}",
                                  "ok" if ok else "fail",
                                  "drive visible in container" if ok else
                                  "container started before the drive mounted; it sees an empty folder",
                                  fix=None if ok else {"kind": "restart", "container": name}, container=name))
        warn, crit = self.c.get("disk_warn", 85), self.c.get("disk_crit", 95)
        for p in self.c.get("disk_paths", ["/"]):
            if p in mounted and not mounted[p]:
                continue
            try:
                u = shutil.disk_usage(p)
            except OSError:
                continue
            pct = u.used / u.total * 100
            st = "fail" if pct >= crit else "warn" if pct >= warn else "ok"
            out.append(result(f"disk:{p}", "storage", f"Disk {p}", st,
                              f"{pct:.0f}% used, {u.free / 1e9:.0f} GB free",
                              fix={"kind": "ai"} if st == "fail" else None))
        return out

    def check_network(self):
        out = []
        try:
            socket.getaddrinfo(self.c.get("dns_probe", "github.com"), 443)
            out.append(result("net:dns", "network", "DNS resolution", "ok", "resolving"))
        except OSError as e:
            out.append(result("net:dns", "network", "DNS resolution", "fail", str(e), fix={"kind": "ai"}))
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=5).close()
            out.append(result("net:internet", "network", "Internet", "ok", "reachable"))
        except OSError as e:
            out.append(result("net:internet", "network", "Internet", "fail", str(e), fix={"kind": "ai"}))
        if self.c.get("tailscale"):
            rc, o, err = sh(["tailscale", "status", "--json"], timeout=15)
            try:
                d = json.loads(o)
                state = d.get("BackendState")
                online = (d.get("Self") or {}).get("Online")
                ok = state == "Running" and online
                detail = f"{state}, {'online' if online else 'offline'}"
            except ValueError:
                ok, detail = False, err or "tailscale not responding"
            out.append(result("net:tailscale", "network", "Tailscale", "ok" if ok else "fail", detail,
                              fix=None if ok else {"kind": "systemctl", "unit": "tailscaled"}))
        return out

    def check_system(self):
        out = []
        rr = Path("/var/run/reboot-required")
        if rr.exists():
            pk = Path("/var/run/reboot-required.pkgs")
            pkgs = ", ".join(sorted(set(pk.read_text().split())))[:200] if pk.exists() else ""
            out.append(result("sys:reboot", "system", "Reboot required", "warn",
                              f"pending reboot for: {pkgs}" if pkgs else "a reboot is pending"))
        rc, o, _ = sh(["systemctl", "--failed", "--no-legend", "--plain"], timeout=15)
        failed = [l.split()[0] for l in o.splitlines() if l.strip()]
        out.append(result("sys:units", "system", "systemd units", "warn" if failed else "ok",
                          ("failed: " + ", ".join(failed)) if failed else "no failed units"))
        try:
            mi = dict(l.split(":", 1) for l in Path("/proc/meminfo").read_text().splitlines())
            total = int(mi["MemTotal"].split()[0])
            avail = int(mi["MemAvailable"].split()[0])
            pct = avail / total * 100
            out.append(result("sys:memory", "system", "Memory", "warn" if pct < 5 else "ok",
                              f"{pct:.0f}% available ({avail / 1048576:.1f} GB)"))
        except Exception:
            pass
        load = os.getloadavg()[1]
        cpus = os.cpu_count() or 1
        out.append(result("sys:load", "system", "CPU load", "warn" if load > cpus * 2 else "ok",
                          f"5-min load {load:.2f} on {cpus} CPUs"))
        return out
