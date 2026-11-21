"""Shared helpers: config, shell, time, persistent JSON state."""
import datetime
import json
import logging
import os
import subprocess
import threading
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("ARRGUS_STATE_DIR") or ROOT / "state")
log = logging.getLogger("arrgus")


def load_config(path=None):
    p = Path(path or os.environ.get("ARRGUS_CONFIG") or ROOT / "config.toml")
    cfg = tomllib.loads(p.read_text())
    cfg["_path"] = str(p)
    cfg.setdefault("checks", {})
    cfg.setdefault("ai", {})
    cfg.setdefault("github", {})
    cfg.setdefault("web", {})
    cfg.setdefault("schedule", {})
    cfg.setdefault("notify", {})
    cfg.setdefault("links", [])
    return cfg


def sh(cmd, timeout=60, cwd=None, input=None, env=None):
    """Run a command; never raises. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            input=input, env=env, shell=isinstance(cmd, str),
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except (FileNotFoundError, PermissionError) as e:
        return 127, "", str(e)


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def human_ts(iso=None):
    d = datetime.datetime.fromisoformat(iso) if iso else datetime.datetime.now()
    return d.strftime("%Y-%m-%d %H:%M:%S")


def month_key():
    return datetime.datetime.now().strftime("%Y-%m")


def uptime_seconds():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except Exception:
        return 1e9


def duration(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


class JsonStore:
    """A dict persisted atomically to disk."""

    def __init__(self, path, default):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = default
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text())
                default.update(loaded)
                self.data = default
            except Exception as e:
                log.warning("could not read %s: %s", self.path, e)

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, default=str))
            os.replace(tmp, self.path)


def sh_stream(cmd, on_line, timeout=1800, cwd=None, env=None):
    """Run a command, calling on_line(line) for each line of combined stdout/stderr as it arrives.
    Never raises. Returns (rc, last ~200 lines of output)."""
    import collections
    tail = collections.deque(maxlen=200)
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                             cwd=cwd, env=env, errors="replace")
    except (FileNotFoundError, PermissionError) as e:
        return 127, str(e)
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            tail.append(line)
            try:
                on_line(line)
            except Exception as e:  # a progress parser must never break the command
                log.debug("on_line failed: %s", e)
        p.wait()
    finally:
        timer.cancel()
    rc = p.returncode if p.returncode is not None else 124
    return (124 if rc == -9 else rc), "\n".join(tail)
