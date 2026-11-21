"""Playbook fixes for known failure modes. Each returns (ok, output)."""
import time

from .util import sh


def describe(fix):
    k = fix.get("kind")
    return {
        "compose_up": lambda: f"start service {fix.get('service')} (docker compose up -d)",
        "restart": lambda: f"restart container {fix.get('container')}",
        "restart_vpn": lambda: "restart VPN and re-attach dependent containers",
        "reattach_vpn": lambda: f"recreate {fix.get('container')} on the current VPN container",
        "vpn_leak": lambda: "stop torrent traffic, restart VPN, re-attach dependents",
        "mount": lambda: f"mount {fix.get('path')} and restart containers using it",
        "systemctl": lambda: f"restart system service {fix.get('unit')}",
        "ai": lambda: "escalate to AI",
    }.get(k, lambda: k)()


class Fixer:
    def __init__(self, cfg, checker):
        self.cfg = cfg
        self.checker = checker
        self.vpn = cfg["checks"].get("vpn") or {}

    def apply(self, fix, inv):
        k = fix["kind"]
        try:
            if k == "compose_up":
                cname = fix.get("container") or self._container_of(fix["service"])
                if cname in self.vpn.get("dependents", []):
                    # it may be bound to a VPN container that no longer exists; only a recreate fixes that
                    return self.reattach([cname])
                # --no-recreate: start/create the service without recreating anything already running (e.g. the VPN)
                rc, out, err = self.checker.compose("up", "-d", "--no-recreate", fix["service"])
                return rc == 0, (out + "\n" + err).strip()
            if k == "reattach_vpn":
                return self.reattach([fix["container"]])
            if k == "restart":
                rc, out, err = sh(["docker", "restart", fix["container"]], timeout=180)
                return rc == 0, (out + "\n" + err).strip()
            if k == "restart_vpn":
                return self._restart_vpn(stop_first=False)
            if k == "vpn_leak":
                return self._restart_vpn(stop_first=True)
            if k == "mount":
                return self._mount(fix["path"], inv)
            if k == "systemctl":
                rc, out, err = sh(["sudo", "-n", "systemctl", "restart", fix["unit"]], timeout=120)
                return rc == 0, (out + "\n" + err).strip()
        except Exception as e:
            return False, f"fix crashed: {e}"
        return False, f"no playbook for {k}"

    # ---- VPN namespace helpers ---------------------------------------------
    def _container_of(self, service):
        svcs, _ = self.checker.services()
        return ((svcs or {}).get(service) or {}).get("container", service)

    def _service_of(self, container):
        svcs, _ = self.checker.services()
        return next((k for k, v in (svcs or {}).items() if v["container"] == container), container)

    def attached(self, dep):
        """True if `dep` is bound to the VPN container that exists right now (same container ID)."""
        rc, vid, _ = sh(["docker", "inspect", "-f", "{{.Id}}", self.vpn.get("container", "")])
        rc2, mode, _ = sh(["docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", dep])
        return rc == 0 and rc2 == 0 and mode in (f"container:{vid}", f"container:{self.vpn.get('container')}")

    def reattach(self, deps=None):
        """Recreate VPN-routed containers so they join the current VPN container. A plain restart can't do this
        after the VPN container was recreated (new ID): they would try to join the old, deleted one."""
        deps = deps or self.vpn.get("dependents", [])
        svcs = [self._service_of(d) for d in deps]
        rc, out, err = self.checker.compose("up", "-d", "--no-deps", "--force-recreate", *svcs, timeout=600)
        bad = [d for d in deps if not self.attached(d)]
        msg = (out + "\n" + err).strip() + (f"\nstill not attached: {', '.join(bad)}" if bad else "")
        return rc == 0 and not bad, msg

    def _wait_healthy(self, name, timeout=240):
        end = time.time() + timeout
        while time.time() < end:
            rc, st, _ = sh(["docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", name])
            if st in ("healthy", "running") and rc == 0:
                return True
            time.sleep(5)
        return False

    def _restart_vpn(self, stop_first):
        name = self.vpn.get("container")
        deps = self.vpn.get("dependents", [])
        log = []
        if stop_first:
            for d in deps:
                sh(["docker", "stop", d], timeout=60)
            log.append(f"stopped {', '.join(deps)} to halt unprotected traffic")
        rc, _, err = sh(["docker", "restart", name], timeout=180)
        log.append(f"restart {name}: {'ok' if rc == 0 else err}")
        healthy = self._wait_healthy(name)
        log.append(f"{name} healthy: {healthy}")
        stale = []
        for d in deps:
            if not self.attached(d):
                stale.append(d)
                continue
            rc, _, err = sh(["docker", "restart", d], timeout=120)
            log.append(f"restart {d}: {'ok' if rc == 0 else err}")
        if stale:
            ok, out = self.reattach(stale)
            log.append(f"recreated {', '.join(stale)} (bound to an old VPN container): {'ok' if ok else out[-300:]}")
            healthy = healthy and ok
        return healthy, "\n".join(log)

    def _mount(self, path, inv):
        rc, out, err = sh(["sudo", "-n", "mount", path], timeout=60)
        log = [f"mount {path}: {'ok' if rc == 0 else err or out}"]
        if rc:
            return False, "\n".join(log)
        for name, c in inv.items():
            if any(src == path or src.startswith(path.rstrip("/") + "/") for src, _ in c["binds"]):
                r, _, e = sh(["docker", "restart", name], timeout=180)
                log.append(f"restart {name}: {'ok' if r == 0 else e}")
        return True, "\n".join(log)
