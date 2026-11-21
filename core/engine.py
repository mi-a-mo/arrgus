"""Monitoring engine: check → confirm → playbook fix → verify → escalate to AI. Handles boot."""
import json
import threading
import time
import urllib.request

from .ai import AIRunner
from .checks import Checker
from .fixes import Fixer, describe
from .logbook import Logbook
from .updates import Updater
from .util import STATE_DIR, JsonStore, duration, human_ts, log, now_iso, sh, uptime_seconds


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.sched = cfg["schedule"]
        self.log = Logbook(cfg)
        self.checker = Checker(cfg)
        self.fixer = Fixer(cfg, self.checker)
        self.state = JsonStore(STATE_DIR / "state.json",
                               {"incidents": {}, "warnings": {}, "ai_runs": [], "updates": {}, "status_sig": ""})
        self.lock = threading.RLock()
        self.trigger = threading.Event()
        self.trigger_reason = "scheduled"
        self.results, self.inv = [], {}
        self.last_check = None
        self.checking = False
        self.ai = AIRunner(cfg, self)
        self.updater = Updater(cfg, self)
        self.started = now_iso()

    # ---- helpers ---------------------------------------------------------
    def trigger_check(self, reason="manual"):
        self.trigger_reason = reason
        self.trigger.set()

    def notify(self, title, body):
        url = self.cfg["notify"].get("ntfy_url")
        if not url:
            return
        try:
            req = urllib.request.Request(url, data=body.encode(), headers={"Title": title, "Tags": "robot"})
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:
            log.warning("notify failed: %s", e)

    def overall(self):
        if any(r["status"] == "fail" for r in self.results):
            return "fail"
        if any(r["status"] == "warn" for r in self.results):
            return "warn"
        return "ok" if self.results else "unknown"

    # ---- core cycle ------------------------------------------------------
    def check_cycle(self, reason="scheduled", allow_fix=True):
        with self.lock:
            self.checking = True
            try:
                return self._cycle(reason, allow_fix)
            finally:
                self.checking = False

    def _fails(self, results):
        return {r["id"]: r for r in results if r["status"] == "fail"}

    def _cycle(self, reason, allow_fix):
        inc = self.state.data["incidents"]
        results, inv, _ = self.checker.run()
        fails = self._fails(results)
        if any(i not in inc for i in fails):
            time.sleep(15)  # confirm new failures to avoid acting on blips
            results, inv, _ = self.checker.run()
            fails = self._fails(results)

        for iid, r in fails.items():
            if iid not in inc:
                inc[iid] = {"id": iid, "name": r["name"], "detail": r["detail"], "opened": now_iso(),
                            "attempts": 0, "ai_runs": 0, "last_ai": 0, "last_fixer": None, "fix": r["fix"]}
                self.log.event("issue", f"{r['name']}: {r['detail']}", level="error",
                               data={"check": iid, "reason": reason})
            else:
                inc[iid]["detail"] = r["detail"]

        warns = self.state.data["warnings"]
        for r in results:
            if r["status"] == "warn" and r["id"] not in warns:
                warns[r["id"]] = now_iso()
                self.log.event("warning", f"{r['name']}: {r['detail']}", level="warn")
        for wid in [w for w in warns if not any(r["id"] == w and r["status"] == "warn" for r in results)]:
            warns.pop(wid)

        ai_active = self.ai.busy()
        if allow_fix and not ai_active and fails:
            applied = False
            max_attempts = self.cfg["checks"].get("max_fix_attempts", 2)
            done = set()
            for iid, r in fails.items():
                f, i = r.get("fix"), inc[iid]
                if not f or f["kind"] == "ai" or i["attempts"] >= max_attempts:
                    continue
                key = json.dumps(f, sort_keys=True)
                i["attempts"] += 1
                if key in done:  # same fix already applied this cycle (e.g. VPN restart)
                    continue
                done.add(key)
                ok, out = self.fixer.apply(f, inv)
                i["last_fixer"] = f"playbook: {describe(f)}"
                self.log.event("fix", f"{r['name']}: {describe(f)} (attempt {i['attempts']}/{max_attempts})",
                               out[-1500:], level="warn" if ok else "error")
                applied = True
            if applied:
                time.sleep(self.sched.get("fix_settle", 30))
                results, inv, _ = self.checker.run()
                fails = self._fails(results)

        for iid in [i for i in inc if i not in fails]:
            i = inc.pop(iid)
            from datetime import datetime
            open_for = (datetime.now().astimezone() - datetime.fromisoformat(i["opened"])).total_seconds()
            self.log.event("resolved", f"{i['name']}",
                           f"Open {duration(open_for)}. Resolved by: {i.get('last_fixer') or 'recovered on its own'}",
                           level="ok", push=True)

        if allow_fix and not self.ai.busy() and self.ai.enabled and self.cfg["ai"].get("auto_escalate", True):
            self._maybe_escalate(fails, results)

        self.results, self.inv, self.last_check = results, inv, now_iso()
        self.state.save()
        self._write_status()
        return results

    def _maybe_escalate(self, fails, results):
        inc = self.state.data["incidents"]
        ai = self.cfg["ai"]
        max_attempts = self.cfg["checks"].get("max_fix_attempts", 2)
        cooldown = ai.get("cooldown", 3600)
        now = time.time()
        due = []
        for iid, r in fails.items():
            i = inc[iid]
            exhausted = not r.get("fix") or r["fix"]["kind"] == "ai" or i["attempts"] >= max_attempts
            if exhausted and now - i.get("last_ai", 0) > cooldown and i["ai_runs"] < ai.get("max_runs_per_incident", 3):
                due.append(i)
        if not due:
            return
        runs = [t for t in self.state.data["ai_runs"] if now - t < 86400]
        if len(runs) >= ai.get("max_auto_runs_per_day", 12):
            return
        self.state.data["ai_runs"] = runs + [now]
        for i in due:
            i["last_ai"], i["ai_runs"] = now, i["ai_runs"] + 1
        req = "Unresolved issues:\n" + "\n".join(
            f"- {i['name']}: {i['detail']} (open since {human_ts(i['opened'])}; "
            f"scripted fix attempts: {i['attempts']}; last: {i.get('last_fixer') or 'none'})" for i in due)
        title = "Auto-escalation: " + ", ".join(i["name"] for i in due)
        self.log.event("ai", f"Escalating to AI: {', '.join(i['name'] for i in due)}", level="warn")
        self.ai.submit("incident", title, req, incidents=[i["id"] for i in due])

    # ---- status snapshot (STATUS.md) ------------------------------------
    def _write_status(self, force=False):
        upd = self.state.data.get("updates", {})
        pending = sorted(s for s, e in upd.get("images", {}).items() if e["status"] in ("available", "pulled"))
        sig = json.dumps([[r["id"], r["status"]] for r in self.results] + pending + [upd.get("os", {}).get("count")])
        day = time.strftime("%Y-%m-%d")
        if not force and sig == self.state.data.get("status_sig") and self.state.data.get("status_day") == day:
            return
        self.state.data["status_sig"], self.state.data["status_day"] = sig, day
        icon = {"ok": "🟢", "warn": "🟡", "fail": "🔴"}
        lines = [f"# {self.cfg['box']['name']} — {icon.get(self.overall(), '⚪')} {self.overall().upper()}", "",
                 f"_Snapshot written {human_ts()} by Arrgus. Updated when anything changes, and at least daily._", ""]
        inc = self.state.data["incidents"]
        if inc:
            lines += ["## Open issues", ""] + [f"- **{i['name']}**: {i['detail']} (since {human_ts(i['opened'])})"
                                               for i in inc.values()] + [""]
        lines += ["## Checks", "", "| | Check | Detail |", "|---|---|---|"]
        for r in sorted(self.results, key=lambda r: ({"fail": 0, "warn": 1}.get(r["status"], 2), r["group"])):
            lines.append(f"| {icon.get(r['status'], '')} | {r['name']} | {r['detail']} |")
        lines += ["", "## Updates", ""]
        if pending:
            for s in pending:
                e = upd["images"][s]
                lines.append(f"- {s}: {e.get('current') or '?'} → {e.get('available') or 'new build'} (awaiting approval)")
        else:
            lines.append("- All container images current" + (f" (checked {human_ts(upd['checked'])})" if upd.get("checked") else ""))
        if upd.get("os"):
            lines.append(f"- OS packages pending: {upd['os']['count']} ({upd['os']['security']} security)")
        self.log.write_file("STATUS.md", "\n".join(lines) + "\n")

    # ---- owner-requested reboot -------------------------------------------
    REBOOT_MARKER = STATE_DIR / "reboot_requested.json"

    def reboot_info(self):
        from pathlib import Path
        pk = Path("/var/run/reboot-required.pkgs")
        pkgs = sorted(set(pk.read_text().split())) if pk.exists() else []
        _, running, _ = sh(["uname", "-r"])
        new = sorted((p[len("linux-image-"):] for p in pkgs if p.startswith("linux-image-")), reverse=True)
        sched = getattr(self, "_reboot", None)
        return {"required": Path("/var/run/reboot-required").exists(), "packages": pkgs,
                "kernel_running": running, "kernel_new": new[0] if new else None,
                "scheduled": None if not sched else {"at": sched["at"], "by": sched["by"],
                                                      "seconds_left": max(0, int(sched["t"] - time.time()))},
                "rebooting": bool(sched and sched.get("now"))}

    def request_reboot(self, by="owner", delay=15):
        if getattr(self, "_reboot", None):
            return False, "a reboot is already scheduled"
        info = self.reboot_info()
        self._reboot = {"at": now_iso(), "by": by, "t": time.time() + delay, "cancel": threading.Event()}
        self.REBOOT_MARKER.write_text(json.dumps({"requested": now_iso(), "by": by,
                                                  "kernel": info["kernel_running"], "packages": info["packages"]}))
        why = (f"pending: {', '.join(info['packages'])}" if info["packages"] else "no reboot was pending")
        self.log.event("boot", f"Reboot requested from the dashboard; rebooting in {delay}s", why, level="warn")
        threading.Thread(target=self._reboot_after, args=(self._reboot,), daemon=True).start()
        return True, None

    def cancel_reboot(self):
        r = getattr(self, "_reboot", None)
        if not r or r.get("now"):
            return False, "no cancellable reboot is scheduled"
        r["cancel"].set()
        return True, None

    def _reboot_after(self, r):
        if r["cancel"].wait(max(0, r["t"] - time.time())):
            self._reboot = None
            self.REBOOT_MARKER.unlink(missing_ok=True)
            self.log.event("boot", "Reboot cancelled from the dashboard", level="info")
            return
        r["now"] = True
        self.log.event("boot", "Rebooting now", level="warn")
        try:
            self.state.save()
            self._write_status(force=True)
            t = threading.Thread(target=self.log.publish)
            t.start()
            t.join(60)  # don't let a slow push block the reboot forever
        except Exception:
            log.exception("pre-reboot publish failed")
        rc, out, err = sh(["systemctl", "--no-ask-password", "reboot"], timeout=30)
        if rc:
            rc, out, err = sh(["sudo", "-n", "systemctl", "reboot"], timeout=30)
        if rc:
            self._reboot = None
            self.REBOOT_MARKER.unlink(missing_ok=True)
            self.log.event("boot", "Reboot FAILED", (err or out)[-800:], level="error", push=True)

    def _boot_report(self):
        """After a dashboard-requested reboot: log downtime and kernel change, once."""
        m = self.REBOOT_MARKER
        if not m.exists():
            return
        try:
            d = json.loads(m.read_text())
        except ValueError:
            d = {}
        m.unlink(missing_ok=True)
        from datetime import datetime
        req = datetime.fromisoformat(d["requested"]) if d.get("requested") else None
        booted = time.time() - uptime_seconds()
        if not req or booted < req.timestamp():
            return  # Arrgus restarted, but the host never rebooted
        _, kernel, _ = sh(["uname", "-r"])
        down = booted - req.timestamp()
        kern = f"kernel {d.get('kernel')} → {kernel}" if d.get("kernel") and d["kernel"] != kernel else f"kernel {kernel} (unchanged)"
        self.log.event("boot", f"Reboot completed: Arrgus back {duration(down + uptime_seconds())} after the request; {kern}",
                       f"Requested {human_ts(d['requested'])} by {d.get('by', 'owner')}.", level="ok", push=True)

    # ---- loops -----------------------------------------------------------
    def boot_sequence(self, force_boot=False, allow_fix=True):
        try:
            self._boot_report()
        except Exception:
            log.exception("boot report failed")
        up = uptime_seconds()
        if force_boot or up < self.sched.get("boot_window", 900):
            grace = self.sched.get("boot_grace", 180)
            self.log.event("boot", f"System boot detected (up {duration(up)}) — waiting up to {grace}s for services")
            end = time.time() + grace
            while time.time() < end:
                rc, out, _ = sh(["docker", "ps", "-a", "--format", "{{.State}}"], timeout=20)
                states = out.split()
                if rc == 0 and states and all(s == "running" for s in states):
                    break
                time.sleep(10)
            results = self.check_cycle("boot", allow_fix=allow_fix)
            fails = [r for r in results if r["status"] == "fail"]
            ok = sum(r["status"] == "ok" for r in results)
            detail = "\n".join(f"FAIL {r['name']}: {r['detail']}" for r in fails) or "All checks passing."
            self.log.event("boot", f"Post-boot check{'' if allow_fix else ' (dry run, no fixes)'}: "
                                   f"{ok}/{len(results)} checks OK, "
                                   f"{len(self.state.data['incidents'])} open issue(s)",
                           detail, level="ok" if not self.state.data["incidents"] else "error", push=True)
        else:
            self.log.event("info", "Arrgus started", f"Host up {duration(up)}")
            self.check_cycle("startup")

    def monitor_loop(self):
        try:
            self.boot_sequence()
        except Exception:
            log.exception("boot sequence failed")
        interval = self.sched.get("check_interval", 300)
        while True:
            self.trigger.wait(interval)
            reason = self.trigger_reason if self.trigger.is_set() else "scheduled"
            self.trigger.clear()
            self.trigger_reason = "scheduled"
            try:
                self.check_cycle(reason)
            except Exception:
                log.exception("check cycle failed")

    def update_loop(self):
        time.sleep(120)
        interval = self.sched.get("update_check_interval", 21600)
        while True:
            try:
                self.updater.check()
                self._write_status()
            except Exception:
                log.exception("update check failed")
            time.sleep(interval)

    def docker_events_loop(self):
        """React to container deaths / unhealthy transitions within ~45s instead of waiting for the next cycle."""
        import subprocess
        while True:
            try:
                p = subprocess.Popen(["docker", "events", "--format", "{{.Action}} {{.Actor.Attributes.name}}",
                                      "--filter", "type=container", "--filter", "event=die",
                                      "--filter", "event=oom", "--filter", "event=health_status"],
                                     stdout=subprocess.PIPE, text=True)
                last = 0
                for line in p.stdout:
                    if line.startswith("health_status") and "unhealthy" not in line:
                        continue
                    if time.time() - last > 60 and not self.checking:
                        last = time.time()
                        threading.Timer(45, self.trigger_check, args=(f"docker event: {line.strip()}",)).start()
            except Exception:
                log.exception("docker events watcher crashed")
            time.sleep(15)

    def start(self):
        for target in (self.monitor_loop, self.update_loop, self.docker_events_loop, self.ai.worker,
                       lambda: self.log.loop(self.cfg["github"].get("push_interval", 300))):
            threading.Thread(target=target, daemon=True).start()
