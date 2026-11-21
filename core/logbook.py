"""Event log: JSONL + human-readable markdown in a git repo, pushed to GitHub."""
import collections
import ipaddress
import json
import os
import re
import threading
import time
from pathlib import Path

from .util import human_ts, log, month_key, now_iso, sh

LABELS = {
    "issue": "🔴 ISSUE",
    "fix": "🔧 FIX",
    "resolved": "✅ RESOLVED",
    "ai": "🤖 AI",
    "update": "⬆️ UPDATE",
    "boot": "🔄 BOOT",
    "warning": "⚠️ WARNING",
    "info": "ℹ️ INFO",
}

PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "0.0.0.0/32")]


class Scrubber:
    """Removes secrets, emails and public/Tailscale IPs before anything is published."""

    def __init__(self, cfg):
        self.literals = set(cfg.get("scrub", {}).get("literals", []))
        env_file = cfg["box"].get("env_file")
        if env_file and Path(env_file).exists():
            for line in Path(env_file).read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    v = v.strip().strip("'\"")
                    if re.search(r"PASS|TOKEN|KEY|SECRET|USER", k, re.I) and len(v) >= 4:
                        self.literals.add(v)
        self.rules = [
            (re.compile(r"(?i)\b(password|passwd|pwd|token|api[_-]?key|apikey|secret)(\"?'?\s*[:=]\s*\"?'?)([^\s\"'&<,;]+)"),
             r"\1\2<redacted>"),
            (re.compile(r"(?i)<(ApiKey)>[^<]+</ApiKey>"), r"<\1><redacted></\1>"),
            (re.compile(r"(?i)(authorization:\s*\w+\s+)\S+"), r"\1<redacted>"),
            (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "<email>"),
        ]
        self.ip_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

    def _ip(self, m):
        try:
            ip = ipaddress.ip_address(m.group(0))
        except ValueError:
            return m.group(0)
        return m.group(0) if any(ip in n for n in PRIVATE_NETS) else "<ip>"

    def __call__(self, text):
        if not text:
            return text
        text = str(text)
        for lit in self.literals:
            text = text.replace(lit, "<redacted>")
        for rx, rep in self.rules:
            text = rx.sub(rep, text)
        return self.ip_re.sub(self._ip, text)


class Logbook:
    def __init__(self, cfg):
        gh = cfg["github"]
        self.box = cfg["box"]["name"]
        self.dir = Path(gh.get("log_dir") or Path.home() / f"sentinel-{self.box}")
        self.remote = gh.get("remote", "")
        self.key = os.path.expanduser(gh.get("ssh_key", "")) if gh.get("ssh_key") else ""
        self.scrub = Scrubber(cfg)
        self.lock = threading.RLock()
        self.dirty = False
        self.push_now = threading.Event()
        self.recent = collections.deque(maxlen=300)
        self.last_push = {"ok": None, "at": None, "error": None}
        self._init_repo()
        self._load_recent()

    # ---- git -------------------------------------------------------------
    def _git(self, *args, timeout=120):
        env = os.environ.copy()
        if self.key:
            env["GIT_SSH_COMMAND"] = (f"ssh -i {self.key} -o IdentitiesOnly=yes "
                                      "-o StrictHostKeyChecking=accept-new -o BatchMode=yes")
        return sh(["git", "-C", str(self.dir), *args], timeout=timeout, env=env)

    def _init_repo(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.dir / ".git").exists():
            sh(["git", "init", "-b", "main", str(self.dir)])
        self._git("config", "user.name", f"sentinel@{self.box}")
        self._git("config", "user.email", f"sentinel@{self.box}.local")
        if self.remote:
            rc, out, _ = self._git("remote", "get-url", "origin")
            if rc:
                self._git("remote", "add", "origin", self.remote)
            elif out != self.remote:
                self._git("remote", "set-url", "origin", self.remote)
        readme = self.dir / "README.md"
        if not readme.exists():
            readme.write_text(
                f"# Sentinel log — {self.box}\n\n"
                "Automatically maintained by Sentinel, the AI monitor for this box.\n\n"
                "- [`STATUS.md`](STATUS.md): current health snapshot\n"
                "- [`log/`](log/): monthly human-readable log of issues, fixes, updates and boots\n"
                "- [`events/`](events/): the same events as JSON Lines\n"
                "- [`ai/`](ai/): full transcripts of every AI troubleshooting session\n\n"
                "Secrets, e-mail addresses and public IPs are scrubbed before commit.\n")
            self.dirty = True

    def _load_recent(self):
        f = self.dir / "events" / f"{month_key()}.jsonl"
        if f.exists():
            for line in f.read_text().splitlines()[-300:]:
                try:
                    self.recent.append(json.loads(line))
                except ValueError:
                    pass

    # ---- writing ---------------------------------------------------------
    def event(self, kind, title, detail="", level="info", data=None, push=False):
        ev = {"ts": now_iso(), "kind": kind, "level": level,
              "title": self.scrub(title), "detail": self.scrub(detail or ""), "data": data or {}}
        with self.lock:
            m = month_key()
            (self.dir / "events").mkdir(exist_ok=True)
            (self.dir / "log").mkdir(exist_ok=True)
            with open(self.dir / "events" / f"{m}.jsonl", "a") as f:
                f.write(json.dumps(ev) + "\n")
            md = self.dir / "log" / f"{m}.md"
            new = not md.exists()
            with open(md, "a") as f:
                if new:
                    f.write(f"# {self.box} — {m}\n\n")
                f.write(f"### {human_ts(ev['ts'])} · {LABELS.get(kind, kind.upper())} · {ev['title']}\n\n")
                if ev["detail"]:
                    body = ev["detail"].strip()
                    if "\n" in body or len(body) > 200:
                        f.write("```\n" + body.replace("```", "'''") + "\n```\n\n")
                    else:
                        f.write(body + "\n\n")
            self.recent.append(ev)
            self.dirty = True
        log.info("[%s] %s %s", kind, ev["title"], ("— " + ev["detail"][:200]) if ev["detail"] else "")
        if push or level in ("error", "critical"):
            self.push_now.set()
        return ev

    def write_file(self, rel, content, push=False):
        with self.lock:
            p = self.dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self.scrub(content))
            self.dirty = True
        if push:
            self.push_now.set()

    def publish(self):
        with self.lock:
            if self.dirty:
                self._git("add", "-A")
                rc, out, _ = self._git("status", "--porcelain")
                if out:
                    n = len(out.splitlines())
                    self._git("commit", "-q", "-m", f"sentinel: update log ({n} file{'s' if n != 1 else ''})")
                self.dirty = False
        if not self.remote:
            return
        rc, _, err = self._git("push", "-q", "-u", "origin", "main")
        if rc and ("rejected" in err or "fetch first" in err or "non-fast-forward" in err):
            self._git("pull", "-q", "--rebase", "-X", "theirs", "origin", "main")
            rc, _, err = self._git("push", "-q", "-u", "origin", "main")
        self.last_push = {"ok": rc == 0, "at": now_iso(), "error": None if rc == 0 else err[-500:]}
        if rc:
            log.warning("git push failed: %s", err[-300:])

    def loop(self, interval):
        while True:
            self.push_now.wait(interval)
            self.push_now.clear()
            time.sleep(3)  # let bursts of events coalesce into one commit
            try:
                self.publish()
            except Exception as e:
                log.exception("publish failed: %s", e)
