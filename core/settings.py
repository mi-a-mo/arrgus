"""Settings made on the dashboard's Settings page.

They are stored as a partial config in `config/<box>.settings.json`, next to the hand-written TOML, and
override the same keys in it (dicts merge, lists and values replace). The TOML is never rewritten.
Saving re-applies the combined config in place, so running components pick it up without a restart.

The Settings page is protected by its own password (scrypt hash in state/settings_auth.json; reset with
`arrgus.py set-password`). Sessions are random tokens held in memory, so a restart signs everyone out.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from . import catalog
from .util import STATE_DIR, now_iso

AUTH_FILE = STATE_DIR / "settings_auth.json"
SESSION_TTL = 12 * 3600
MIN_PASSWORD = 8


def overlay_path(cfg_path):
    p = Path(cfg_path)
    return p.with_name(p.stem + ".settings.json")


def read_overlay(cfg_path):
    p = overlay_path(cfg_path)
    if not p.exists():
        return {}
    d = json.loads(p.read_text())
    return d if isinstance(d, dict) else {}


def merge(base, over):
    """Deep-merge `over` into `base` (in place) and return base."""
    for k, v in over.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            merge(base[k], v)
        else:
            base[k] = v
    return base


def sync(dst, src):
    """Make dict `dst` equal to `src` while keeping the same dict objects, so holders of sub-dicts
    (e.g. Checker.c = cfg["checks"]) see the change."""
    for k in [k for k in dst if k not in src and not k.startswith("_")]:
        del dst[k]
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            sync(dst[k], v)
        else:
            dst[k] = v


# ---- password ------------------------------------------------------------------------------------
def _hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def password_set():
    return AUTH_FILE.exists()


def set_password(password):
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"use at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"salt": salt.hex(), "hash": _hash(password, salt).hex(), "set": now_iso()}))
    tmp.chmod(0o600)
    tmp.replace(AUTH_FILE)


def check_password(password):
    try:
        d = json.loads(AUTH_FILE.read_text())
        return hmac.compare_digest(_hash(password, bytes.fromhex(d["salt"])).hex(), d["hash"])
    except (OSError, ValueError, KeyError):
        return False


class Sessions:
    def __init__(self):
        self.tokens = {}
        self.fails = []
        self.lock = threading.Lock()

    def create(self):
        t = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.tokens = {k: v for k, v in self.tokens.items() if v > now}
            self.tokens[t] = now + SESSION_TTL
        return t

    def valid(self, token):
        with self.lock:
            exp = self.tokens.get(token or "")
            return bool(exp and exp > time.time())

    def drop(self, token=None):
        with self.lock:
            if token is None:
                self.tokens.clear()
            else:
                self.tokens.pop(token, None)

    def throttled(self):
        """At most 5 failed sign-ins per 5 minutes."""
        with self.lock:
            now = time.time()
            self.fails = [t for t in self.fails if t > now - 300]
            return len(self.fails) >= 5

    def failed(self):
        with self.lock:
            self.fails.append(time.time())


# ---- services view --------------------------------------------------------------------------------
def _host_port(inv, c, port):
    """127.0.0.1:<port> (or the bound address) where `port` of container `c` is published, if anywhere.
    Containers sharing another container's network (VPN) publish their ports on that container."""
    owner = c
    nm = c.get("network_mode") or ""
    if nm.startswith("container:"):
        ref = nm.split(":", 1)[1]
        owner = next((x for x in inv.values() if x.get("id") == ref or x["name"] == ref), c)
    for ip, hp in (owner.get("ports") or {}).get(f"{port}/tcp", []):
        if ip in ("", "0.0.0.0", "::", "127.0.0.1"):
            return f"127.0.0.1:{hp}"
        if ":" not in ip:
            return f"{ip}:{hp}"
    return None


def _domain(links):
    """The shared parent domain of existing https links (e.g. example.com for plex.example.com)."""
    hosts = [urlparse(l.get("url", "")).hostname or "" for l in links]
    parents = [h.split(".", 1)[1] for h in hosts if h.count(".") >= 2]
    return max(set(parents), key=parents.count) if parents else ""


def services_view(cfg, inv, svcs):
    """Everything the Services section shows: one row per compose service, plus links and checks
    that are not tied to a service."""
    checks = cfg.get("checks", {})
    ignore = set(checks.get("ignore_containers", []))
    http = checks.get("http", [])
    links = cfg.get("links", [])
    domain = _domain(links)
    used_links, used_checks, rows = set(), set(), []
    names = {s.get("container") for s in svcs.values()}
    for svc, s in sorted(svcs.items()):
        cname = s["container"]
        c = inv.get(cname, {"name": cname})
        app = catalog.match(s.get("image") or c.get("image"), svc)
        hc = next((i for i, h in enumerate(http) if h.get("container") == cname and not h.get("resolve")
                   and i not in used_checks), None)
        if hc is not None:
            used_checks.add(hc)
        label = app["name"] if app else svc
        keys = {svc.lower(), cname.lower(), label.lower()} | ({app["id"]} if app else set())
        li = next((i for i, l in enumerate(links) if i not in used_links and (
            l.get("name", "").lower() in keys or
            (urlparse(l.get("url", "")).hostname or "").split(".")[0].lower() in keys)), None)
        if li is not None:
            used_links.add(li)
        # suggestions for anything not configured yet
        sug_url, sug_via = "", ""
        if app and app.get("port"):
            hp = "127.0.0.1:" + str(app["port"]) if c.get("network_mode") == "host" else _host_port(inv, c, app["port"])
            if hp:
                sug_url = f"http://{hp}{app['path']}"
            else:
                sug_url, sug_via = f"http://localhost:{app['port']}{app['path']}", cname
        sub = (app["id"] if app else svc).replace("_", "-")
        h = http[hc] if hc is not None else None
        l = links[li] if li is not None else None
        rows.append({
            "service": svc, "container": cname, "image": s.get("image") or c.get("image") or "",
            "running": c.get("running"), "status": c.get("status") or "not created",
            "app": app, "label": label,
            "monitor": cname not in ignore,
            "health": {"enabled": h is not None, "url": (h or {}).get("url", sug_url),
                       "via": (h or {}).get("via", sug_via if h is None else ""),
                       "name": (h or {}).get("name", label), "extra": {k: v for k, v in (h or {}).items()
                                                                     if k not in ("name", "url", "via", "container")}},
            "link": {"enabled": l is not None, "name": (l or {}).get("name", label),
                     "url": (l or {}).get("url", f"https://{sub}.{domain}" if domain and app and app["kind"] != "infra" else ""),
                     "icon": (l or {}).get("icon", (app or {}).get("icon", ""))},
            "suggested": {"url": sug_url, "via": sug_via},
        })
    return {
        "services": rows,
        "extra_links": [l for i, l in enumerate(links) if i not in used_links],
        "other_checks": [h for i, h in enumerate(http) if i not in used_checks],
        "containers": sorted(inv),
        "unmanaged": sorted(n for n in inv if n not in names),
    }


# ---- validation + save -----------------------------------------------------------------------------
def _url(v, field, schemes=("http", "https"), required=True):
    v = (v or "").strip()
    if not v and not required:
        return ""
    u = urlparse(v)
    if u.scheme not in schemes or not u.hostname or any(ch.isspace() for ch in v):
        raise ValueError(f"{field}: enter a full {' or '.join(schemes)} URL")
    return v


def _name(v, field):
    v = re.sub(r"\s+", " ", (v or "").strip())
    if not v or len(v) > 40:
        raise ValueError(f"{field}: 1–40 characters")
    return v


def build_services_overlay(body, cfg, view):
    """Turn the Services form into config keys. Checks and links the form doesn't manage are kept."""
    known = {r["service"]: r for r in view["services"]}
    containers = set(view["containers"])
    http, links, ignore = [], [], set(cfg.get("checks", {}).get("ignore_containers", []))
    for item in body.get("services", []):
        r = known.get(item.get("service"))
        if not r:
            raise ValueError(f"unknown service {item.get('service')!r}")
        cname, label = r["container"], r["label"]
        if item.get("monitor", True):
            ignore.discard(cname)
        else:
            ignore.add(cname)
        h = item.get("health") or {}
        if h.get("enabled") and item.get("monitor", True):  # no health check on a container Arrgus ignores
            via = (h.get("via") or "").strip()
            if via and via not in containers:
                raise ValueError(f"{label}: unknown container {via!r} to probe from")
            entry = {"name": _name(h.get("name") or label, f"{label} check name"),
                     "url": _url(h.get("url"), f"{label} health check"), "container": cname}
            if via:
                entry["via"] = via
            entry.update({k: v for k, v in (r["health"].get("extra") or {}).items() if k in ("group", "timeout", "max_status")})
            http.append(entry)
        l = item.get("link") or {}
        if l.get("enabled"):
            links.append({"name": _name(l.get("name") or label, f"{label} link name"),
                          "url": _url(l.get("url"), f"{label} link"),
                          "icon": _url(l.get("icon"), f"{label} icon", ("https",), required=False)})
    for i, l in enumerate(body.get("extra_links", [])):
        links.append({"name": _name(l.get("name"), f"extra link {i + 1} name"),
                      "url": _url(l.get("url"), f"extra link {i + 1}"),
                      "icon": _url(l.get("icon"), f"extra link {i + 1} icon", ("https",), required=False)})
    names = [h["name"] for h in http] + [h["name"] for h in view["other_checks"]]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ValueError(f"two health checks are named {sorted(dup)[0]!r}; names must be unique")
    # keep the dashboard's current link order; new links go at the end
    order = [l.get("name") for l in cfg.get("links", [])]
    links.sort(key=lambda l: order.index(l["name"]) if l["name"] in order else len(order))
    return {"checks": {"http": http + view["other_checks"], "ignore_containers": sorted(ignore)},
            "links": [{k: v for k, v in l.items() if v} for l in links]}


def save(cfg, changes, load_config):
    """Merge `changes` into the overlay file, then re-apply TOML + overlay to the live config in place."""
    path = overlay_path(cfg["_path"])
    over = merge(read_overlay(cfg["_path"]), changes)
    over["_note"] = ("Written by the Arrgus Settings page. Overrides the same keys in "
                     f"{Path(cfg['_path']).name}. Edit with care.")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(over, indent=2) + "\n")
    os.replace(tmp, path)
    fresh = load_config(cfg["_path"])
    sync(cfg, fresh)
