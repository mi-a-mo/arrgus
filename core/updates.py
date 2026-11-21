"""Update detection (container images via registry API, OS packages via apt) and owner-approved apply."""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError

from .checks import docker_inventory
from .util import log, now_iso, sh, sh_stream

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])
VERSION_LABELS = ("org.opencontainers.image.version", "build_version", "version")


class _StripAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Don't forward registry bearer tokens to blob CDNs (signed URLs reject them)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).hostname != urllib.parse.urlparse(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_StripAuthRedirect)


def parse_ref(ref):
    name, tag = ref.split("@")[0], "latest"
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        return parts[0], "/".join(parts[1:]), tag
    return "registry-1.docker.io", name if "/" in name else f"library/{name}", tag


class Registry:
    def __init__(self):
        self.tokens = {}

    def _token(self, challenge):
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge or ""))
        realm = params.pop("realm", None)
        if not realm:
            return None
        url = realm + "?" + urllib.parse.urlencode(params)
        with _opener.open(url, timeout=20) as r:
            d = json.load(r)
        return d.get("token") or d.get("access_token")

    def request(self, reg, repo, path, accept=ACCEPT, method="GET"):
        url = f"https://{reg}/v2/{repo}/{path}"
        key = (reg, repo)
        for attempt in range(2):
            headers = {"Accept": accept, "User-Agent": "sentinel-monitor"}
            if self.tokens.get(key):
                headers["Authorization"] = f"Bearer {self.tokens[key]}"
            try:
                return _opener.open(urllib.request.Request(url, headers=headers, method=method), timeout=30)
            except HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self.tokens[key] = self._token(e.headers.get("WWW-Authenticate"))
                    continue
                raise

    def digest(self, ref):
        reg, repo, tag = parse_ref(ref)
        with self.request(reg, repo, f"manifests/{tag}", method="HEAD") as r:
            d = r.headers.get("Docker-Content-Digest")
        if d:
            return d
        with self.request(reg, repo, f"manifests/{tag}") as r:
            return r.headers.get("Docker-Content-Digest")

    def version(self, ref, arch="amd64"):
        """Best effort: read the version label from the remote image config."""
        reg, repo, tag = parse_ref(ref)
        with self.request(reg, repo, f"manifests/{tag}") as r:
            m = json.load(r)
        if "manifests" in m:
            pick = next((x for x in m["manifests"] if (x.get("platform") or {}).get("architecture") == arch
                         and (x.get("platform") or {}).get("os") == "linux"), None)
            if not pick:
                return None
            with self.request(reg, repo, f"manifests/{pick['digest']}") as r:
                m = json.load(r)
        cfg_digest = m.get("config", {}).get("digest")
        if not cfg_digest:
            return None
        with self.request(reg, repo, f"blobs/{cfg_digest}", accept="*/*") as r:
            labels = (json.load(r).get("config") or {}).get("Labels") or {}
        return clean_version(labels)


def clean_version(labels):
    for k in VERSION_LABELS:
        v = labels.get(k)
        if v and v != "latest":
            m = re.search(r"version:-\s*(\S+)", v)
            return m.group(1) if m else v[:60]
    return None


class Updater:
    def __init__(self, cfg, engine):
        self.cfg = cfg
        self.engine = engine
        self.reg = Registry()
        self.running = threading.Lock()
        self.progress = {"active": False}
        self.plock = threading.Lock()

    @property
    def state(self):
        return self.engine.state.data.setdefault("updates", {})

    # ---- detection -------------------------------------------------------
    def check(self, reason="scheduled", wait=False):
        if not self.running.acquire(blocking=wait):
            return self.state
        try:
            return self._check(reason)
        finally:
            self.running.release()

    def _check(self, reason):
        svcs, err = self.engine.checker.services()
        inv, _ = docker_inventory()
        inv = inv or {}
        images = {}
        for svc, s in (svcs or {}).items():
            c = inv.get(s["container"], {})
            if s["build"]:
                images[svc] = {"service": svc, "image": "(built locally)", "built": True, "status": "rebuild_only",
                               "current": None, "available": None}
                continue
            ref = s["image"]
            entry = {"service": svc, "image": ref, "built": False, "current": None, "available": None,
                     "status": "unknown", "error": None}
            # Read the whole config: a template on .Config.Labels fails for images that have no labels (e.g. docker:cli).
            rc, out, _ = sh(["docker", "image", "inspect", ref, "-f",
                             "{{json .RepoDigests}}|{{.Id}}|{{json .Config}}"], timeout=30)
            local_digests, local_id, labels = [], None, {}
            if rc == 0:
                a, local_id, b = out.split("|", 2)
                local_digests = json.loads(a) or []
                labels = (json.loads(b) or {}).get("Labels") or {}
                entry["current"] = clean_version(labels)
            try:
                remote = self.reg.digest(ref)
                entry["remote_digest"] = remote
                if remote and any(d.endswith(remote) for d in local_digests):
                    entry["status"] = "current"
                else:
                    entry["status"] = "available"
                    try:
                        entry["available"] = self.reg.version(ref)
                    except Exception:
                        pass
            except Exception as e:
                entry["error"] = str(e)[:200]
            if entry["status"] == "current" and c.get("image_id") and local_id and c["image_id"] != local_id:
                entry["status"] = "pulled"  # newer image already downloaded, container not recreated
            images[svc] = entry

        os_info = self._apt()
        prev = self.state.get("images", {})
        newly = [e for s, e in images.items() if e["status"] in ("available", "pulled")
                 and (prev.get(s, {}).get("remote_digest") != e.get("remote_digest")
                      or prev.get(s, {}).get("status") not in ("available", "pulled"))]
        self.state.update({"checked": now_iso(), "images": images, "os": os_info})
        self.engine.state.save()

        if newly:
            lines = [f"{e['service']}: {e['current'] or '?'} → {e['available'] or 'new build'}" for e in newly]
            self.engine.log.event("update", f"{len(newly)} container update(s) available — awaiting approval",
                                  "\n".join(lines), level="notice", push=True)
            self.engine.notify(f"{len(newly)} update(s) available on {self.cfg['box']['name']}",
                               "\n".join(lines) + "\nApprove them in the Sentinel dashboard.")
        prev_os = self.engine.state.data.get("os_reported", 0)
        if os_info["count"] and os_info["count"] != prev_os:
            self.engine.log.event("update", f"{os_info['count']} OS package update(s) available "
                                            f"({os_info['security']} security)",
                                  ", ".join(os_info["packages"][:40]), level="info")
            self.engine.state.data["os_reported"] = os_info["count"]
        if reason == "manual" and not newly:
            self.engine.log.event("info", "Manual version check: "
                                  f"{sum(1 for e in images.values() if e['status'] in ('available', 'pulled'))} "
                                  f"container update(s), {os_info['count']} OS package update(s) pending")
        return self.state

    def _apt(self):
        rc, out, _ = sh(["apt", "list", "--upgradable"], timeout=60)
        pk = [l for l in out.splitlines() if "/" in l and "upgradable" in l]
        names = [l.split("/")[0] for l in pk]
        # packages a plain `apt-get upgrade` keeps back because they need NEW packages (typically a new kernel)
        held, grab = [], False
        _, sim, _ = sh(["apt-get", "-s", "upgrade"], timeout=120)
        for l in sim.splitlines():
            if "have been kept back" in l:
                grab = True
            elif grab and l.startswith("  "):
                held += l.split()
            else:
                grab = False
        return {"count": len(pk), "security": sum("-security" in l for l in pk), "packages": names,
                "held": held, "kernel": any(n.startswith("linux-") for n in names),
                "docker": any(n.startswith(("docker-ce", "containerd")) for n in names)}

    # ---- progress ----------------------------------------------------------
    def _start_progress(self, kind, label, names):
        with self.plock:
            self.progress = {"active": True, "kind": kind, "label": label, "stage": "Starting…",
                             "started": now_iso(), "finished": None, "ok": None,
                             "items": [{"name": n, "percent": 0, "stage": "Waiting", "status": "pending"} for n in names]}

    def _item(self, name, **kw):
        with self.plock:
            for it in self.progress.get("items", []):
                if it["name"] == name:
                    if "percent" in kw:
                        kw["percent"] = max(it["percent"], min(100, int(kw["percent"])))
                    it.update(kw)
                    if kw.get("stage") and it["status"] == "running":
                        self.progress["stage"] = f"{name}: {kw['stage']}"

    def _stage(self, text):
        with self.plock:
            self.progress["stage"] = text

    def _finish_progress(self, ok):
        with self.plock:
            for it in self.progress.get("items", []):
                if it["status"] in ("pending", "running"):
                    it.update(status="failed", stage=it["stage"] if it["status"] == "running" else "Not started",
                              percent=100)
            self.progress.update(active=False, finished=now_iso(), ok=ok,
                                 stage="Finished" if ok else "Finished with problems")

    def progress_view(self):
        with self.plock:
            p = json.loads(json.dumps(self.progress))
        items = p.get("items") or []
        if items:
            pct = sum(i["percent"] for i in items) / len(items)
            p["percent"] = 100 if not p.get("active") else min(99, int(pct * 0.95))
        else:
            p["percent"] = 0
        return p

    # ---- apply -----------------------------------------------------------
    def apply(self, services):
        """Pull + recreate each service; verify health; roll back on failure."""
        eng = self.engine
        results = []
        self._start_progress("containers", f"Updating {', '.join(services)}", services)
        try:
            if not eng.lock.acquire(blocking=False):
                self._stage("Waiting for the running system check to finish…")
                eng.lock.acquire()
            try:
                svcs, _ = eng.checker.services()
                vpn = (self.cfg["checks"].get("vpn") or {}).get("container")
                # Update the VPN container first, so containers routed through it are rebuilt on the new one.
                services = sorted(services, key=lambda x: ((svcs or {}).get(x, {}).get("container") != vpn))
                for svc in services:
                    s = (svcs or {}).get(svc)
                    if not s:
                        self._item(svc, status="failed", stage="Not in the compose file", percent=100)
                        results.append({"service": svc, "ok": False, "error": "unknown service"})
                        continue
                    results.append(self._apply_one(svc, s))
                if self.cfg.get("updates", {}).get("prune_images", True):
                    self._stage("Removing old images…")
                    sh(["docker", "image", "prune", "-f"], timeout=300)
            finally:
                eng.lock.release()
            self._stage("Re-checking versions…")
            self.check(reason="post-apply", wait=True)
        finally:
            self._finish_progress(bool(results) and all(r["ok"] for r in results))
        eng.trigger_check("post-update")
        return results

    def _pull(self, svc, build):
        """docker compose pull/build with layer-level progress (3% → 55% of this item)."""
        layers, done, downloaded = set(), set(), set()

        def on_line(line):
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 12 and all(c in "0123456789abcdef" for c in parts[0]):
                lid, what = parts[0], " ".join(parts[1:3])
                layers.add(lid)
                if what.startswith(("Pull complete", "Already exists")):
                    done.add(lid)
                    downloaded.add(lid)
                elif what.startswith("Download complete"):
                    downloaded.add(lid)
                n = len(layers)
                frac = (len(downloaded) * 0.6 + len(done) * 0.4) / n if n else 0
                self._item(svc, percent=3 + 52 * frac, stage=f"Downloading image ({len(done)}/{n} layers)")
            elif build and line.lstrip().startswith("#"):
                self._item(svc, percent=min(50, self.progress_item_pct(svc) + 1), stage="Building image…")

        box = self.cfg["box"]
        cmd = ["docker", "compose", "-f", box.get("compose_file", "docker-compose.yml")]
        cmd += ["build", "--pull", svc] if build else ["pull", svc]
        self._item(svc, stage="Building image…" if build else "Downloading image…", percent=3)
        return sh_stream(cmd, on_line, timeout=1800, cwd=box["compose_dir"])

    def progress_item_pct(self, name):
        with self.plock:
            return next((i["percent"] for i in self.progress.get("items", []) if i["name"] == name), 0)

    def _apply_one(self, svc, s):
        eng, cname = self.engine, s["container"]
        self._item(svc, status="running", stage="Saving a rollback point", percent=1)
        inv, _ = docker_inventory()
        old_id = (inv or {}).get(cname, {}).get("image_id")
        before = self.state.get("images", {}).get(svc, {}).get("current")
        rb_tag = f"sentinel-rollback/{svc}:previous"
        if old_id:
            sh(["docker", "tag", old_id, rb_tag])
        rc, out = self._pull(svc, s["build"])
        if rc:
            eng.log.event("update", f"{svc}: download failed, nothing changed", out[-1500:], level="error")
            self._item(svc, status="failed", stage="Download failed; nothing changed", percent=100)
            return {"service": svc, "ok": False, "error": "pull failed"}
        self._item(svc, stage="Recreating container", percent=58)
        rc, out, err = eng.checker.compose("up", "-d", svc, timeout=600)
        self._item(svc, stage="Health check", percent=70)
        ok = rc == 0 and self._verify(svc, cname, on_tick=lambda f, t: self._item(
            svc, percent=70 + 28 * f, stage=f"Checking health… {int(t)}s (rolls back if not healthy within 3 min)"))
        if ok:
            _, lbl, _ = sh(["docker", "inspect", cname, "-f", "{{json .Config.Labels}}"])
            try:
                after = clean_version(json.loads(lbl))
            except ValueError:
                after = None
            eng.log.event("update", f"{svc} updated {before or '?'} → {after or 'latest'}",
                          "Health verified after update.", level="ok", push=True)
            if cname == (self.cfg["checks"].get("vpn") or {}).get("container"):
                # The VPN container was recreated with a new ID: dependents must be recreated to join it.
                self._item(svc, stage="Re-attaching VPN-routed containers", percent=99)
                rok, rout = eng.fixer.reattach()
                eng.log.event("fix", "Re-attached VPN-routed containers to the updated VPN container", rout[-1500:],
                              level="ok" if rok else "error")
            self._item(svc, status="ok", stage=f"Updated {before or '?'} → {after or 'latest'}", percent=100)
            return {"service": svc, "ok": True}
        # roll back
        detail = (err or out)[-1000:]
        if old_id and not s["build"]:
            self._item(svc, stage="Health check failed; rolling back", percent=85)
            sh(["docker", "tag", rb_tag, s["image"]])
            sh(["docker", "compose", "-f", self.cfg["box"].get("compose_file", "docker-compose.yml"),
                "up", "-d", "--pull", "never", svc], cwd=self.cfg["box"]["compose_dir"], timeout=600)
            rolled = self._verify(svc, cname)
            eng.log.event("update", f"{svc} update FAILED health check — rolled back "
                                    f"({'healthy again' if rolled else 'still unhealthy!'})",
                          detail, level="error", push=True)
            self._item(svc, status="rolled_back", percent=100,
                       stage="Failed health check; rolled back" + ("" if rolled else " (STILL UNHEALTHY)"))
        else:
            eng.log.event("update", f"{svc} update FAILED health check", detail, level="error", push=True)
            self._item(svc, status="failed", stage="Failed health check", percent=100)
        return {"service": svc, "ok": False, "error": "failed verification"}

    def _verify(self, svc, cname, timeout=180, on_tick=None):
        start = time.time()
        end = start + timeout
        time.sleep(10)
        while time.time() < end:
            if on_tick:
                on_tick((time.time() - start) / timeout, time.time() - start)
            results, _, _ = self.engine.checker.run()
            mine = [r for r in results if r["id"] == f"container:{svc}" or r.get("container") == cname]
            if mine and all(r["status"] != "fail" for r in mine):
                return True
            time.sleep(15)
        return False

    def apply_os(self):
        """apt-get update + upgrade (exact commands allowed by deploy/sudoers.example), with progress parsed
        from apt's normal output: Get:N (download), Unpacking, Setting up."""
        eng = self.engine
        name = "OS packages"
        self._start_progress("os", "Upgrading OS packages", [name])
        self._item(name, status="running", stage="Refreshing package lists", percent=2)
        st = {"total": 0, "get": 0, "unpack": 0, "setup": 0}
        ok = False
        try:
            rc1, out1 = sh_stream(["sudo", "-n", "apt-get", "update"], lambda l: None, timeout=600)
            self._item(name, stage="Calculating upgrade", percent=8)

            def on_line(line):
                m = re.search(r"(\d+) upgraded, (\d+) newly installed", line)
                if m:
                    st["total"] = int(m.group(1)) + int(m.group(2))
                if line.startswith("Get:"):
                    st["get"] += 1
                elif line.startswith("Unpacking "):
                    st["unpack"] += 1
                elif line.startswith("Setting up "):
                    st["setup"] += 1
                else:
                    return
                n = max(st["total"], 1)
                pct = 8 + 30 * min(st["get"], n) / n + 30 * min(st["unpack"], n) / n + 30 * min(st["setup"], n) / n
                if st["setup"]:
                    stage, count = "Configuring", st["setup"]
                elif st["unpack"]:
                    stage, count = "Installing", st["unpack"]
                else:
                    stage, count = "Downloading", st["get"]
                self._item(name, percent=pct, stage=f"{stage} ({count}/{st['total'] or '?'} packages)")

            opts = ["-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
            # --with-new-pkgs also installs packages that need new dependencies (new kernels); it never removes any.
            rc2, out2 = sh_stream(["sudo", "-n", "apt-get", "-y", "--with-new-pkgs", *opts, "upgrade"], on_line,
                                  timeout=3600)
            note = ""
            if rc2 and "password is required" in out2:
                note = ("\nKernel/new packages were held back: the sudoers rule does not allow --with-new-pkgs yet "
                        "(see deploy/sudoers.example).")
                rc2, out2 = sh_stream(["sudo", "-n", "apt-get", "-y", *opts, "upgrade"], on_line, timeout=3600)
            ok = rc1 == 0 and rc2 == 0
            tail = "\n".join((out2 or out1).splitlines()[-25:]) + note
            eng.log.event("update", "OS packages upgraded" if ok else "OS package upgrade FAILED", tail,
                          level="ok" if ok else "error", push=True)
            self._item(name, percent=98, stage="Re-checking for updates")
            self.check(reason="post-apply", wait=True)
            reboot = os.path.exists("/var/run/reboot-required")
            self._item(name, status="ok" if ok else "failed", percent=100,
                       stage=(f"Upgraded {st['setup'] or st['total']} package(s)" + ("; reboot required" if reboot else "")
                              + ("; kernel packages held back (sudoers needs --with-new-pkgs)" if note else ""))
                       if ok else "Upgrade failed; see the activity log")
        finally:
            self._finish_progress(ok)
        eng.trigger_check("post-os-update")
        return ok
