"""Dependency-free HTTP dashboard + JSON API for Arrgus.

Security model: the server binds to a private address that only the reverse proxy can reach, and every
request except /healthz must carry the X-Arrgus-Proxy header with the shared secret that the proxy
injects. POSTs additionally need `X-Arrgus: 1` and a JSON body, so a third-party page in the owner's
browser cannot forge them (custom headers force a CORS preflight, which this server never approves).
"""
import hmac
import html
import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from http.cookies import SimpleCookie

from core import settings as S
from core.ai import strip_result
from core.checks import docker_inventory, http_probe
from core.metrics import Metrics
from core.util import STATE_DIR, load_config, log, now_iso, sh

WEB_DIR = Path(__file__).resolve().parent
SECRET_FILE = STATE_DIR / "proxy_secret"
MAX_BODY = 64 * 1024


def proxy_secret():
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secrets.token_urlsafe(32))
        SECRET_FILE.chmod(0o600)
    return SECRET_FILE.read_text().strip()


class Actions:
    """Tracks long-running dashboard actions (checks, version checks, update installs)."""

    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def running(self, kind):
        with self.lock:
            return next((a for a in self.items.values() if a["kind"] == kind and a["status"] == "running"), None)

    def start(self, kind, label, fn):
        aid = time.strftime("%H%M%S-") + secrets.token_hex(2)
        a = {"id": aid, "kind": kind, "label": label, "status": "running", "started": now_iso(),
             "finished": None, "result": None}
        with self.lock:
            self.items[aid] = a
            for old in sorted(self.items, key=lambda k: self.items[k]["started"])[:-30]:
                self.items.pop(old)

        def run():
            try:
                a["result"] = fn()
                a["status"] = "done"
            except Exception as e:
                log.exception("action %s failed", kind)
                a["status"], a["result"] = "failed", str(e)
            a["finished"] = now_iso()

        threading.Thread(target=run, daemon=True).start()
        return a

    def list(self):
        with self.lock:
            return sorted(self.items.values(), key=lambda a: a["started"], reverse=True)


class App:
    def __init__(self, cfg, engine):
        self.cfg = cfg
        self.eng = engine
        self.secret = proxy_secret()
        self.actions = Actions()
        self.metrics = Metrics(cfg)
        self.page = None
        self.sessions = S.Sessions()
        self.save_lock = threading.Lock()

    # ---- views -------------------------------------------------------------
    def render_page(self):
        tpl = (WEB_DIR / "dashboard.html").read_text()
        w = self.cfg["web"]
        boot = {"box": self.cfg["box"]["name"], "links": self.cfg["links"], "ui": self.ui_version(),
                "title": w.get("title") or self.cfg["box"]["name"], "subtitle": w.get("subtitle", "")}
        return (tpl.replace("{{TITLE}}", html.escape(boot["title"])).replace("{{UI}}", self.ui_version())
                   .replace("{{BOOT_JSON}}", json.dumps(boot).replace("</", "<\\/")))

    def render_settings(self):
        tpl = (WEB_DIR / "settings.html").read_text()
        title = self.cfg["web"].get("title") or self.cfg["box"]["name"]
        boot = {"box": self.cfg["box"]["name"], "ui": self.ui_version(),
                "overlay": S.overlay_path(self.cfg["_path"]).name, "toml": Path(self.cfg["_path"]).name}
        return (tpl.replace("{{TITLE}}", html.escape(title)).replace("{{UI}}", self.ui_version())
                   .replace("{{BOOT_JSON}}", json.dumps(boot).replace("</", "<\\/")))

    def ui_version(self):
        """Changes whenever the web UI or server code changes, so open tabs know to reload."""
        return str(int(max((WEB_DIR / f).stat().st_mtime for f in
                           ("dashboard.html", "settings.html", "theme.css", "server.py") if (WEB_DIR / f).exists())))

    # ---- settings ----------------------------------------------------------
    def settings_auth(self, token):
        return {"password_set": S.password_set(), "signed_in": self.sessions.valid(token)}

    def settings_view(self):
        inv, err = docker_inventory()
        if inv is None:
            raise RuntimeError(f"cannot read Docker containers: {err}")
        svcs, err = self.eng.checker.services()
        if svcs is None:
            raise RuntimeError(f"cannot read the compose file: {err}")
        return S.services_view(self.cfg, inv, svcs)

    def do_settings_password(self, body, token):
        new = body.get("password") or ""
        if S.password_set():
            if not self.sessions.valid(token):
                return 401, {"error": "sign in first"}
            if not S.check_password(body.get("current") or ""):
                return 403, {"error": "current password is wrong"}
        S.set_password(new)  # raises ValueError for a short password
        self.sessions.drop()
        self.eng.log.event("info", "Settings password " + ("changed" if body.get("current") else "set"))
        return 200, {"ok": True}, self.sessions.create()

    def do_settings_login(self, body):
        if self.sessions.throttled():
            return 429, {"error": "too many attempts; wait a few minutes"}
        if not S.password_set():
            return 409, {"error": "no password set yet"}
        if not S.check_password(body.get("password") or ""):
            self.sessions.failed()
            return 403, {"error": "wrong password"}
        return 200, {"ok": True}, self.sessions.create()

    def do_settings_services(self, body):
        with self.save_lock:
            view = self.settings_view()
            changes = S.build_services_overlay(body, self.cfg, view)
            before = {"links": [l["name"] for l in self.cfg["links"]],
                      "checks": [h["name"] for h in self.cfg["checks"].get("http", [])],
                      "ignored": list(self.cfg["checks"].get("ignore_containers", []))}
            S.save(self.cfg, changes, load_config)
            after = {"links": [l["name"] for l in self.cfg["links"]],
                     "checks": [h["name"] for h in self.cfg["checks"].get("http", [])],
                     "ignored": list(self.cfg["checks"].get("ignore_containers", []))}
        diff = []
        for k, label in (("links", "dashboard links"), ("checks", "health checks"), ("ignored", "ignored containers")):
            add = [x for x in after[k] if x not in before[k]]
            rem = [x for x in before[k] if x not in after[k]]
            if add or rem:
                diff.append(f"{label}: " + ", ".join([f"+{x}" for x in add] + [f"−{x}" for x in rem]))
        self.eng.log.event("info", "Services settings saved from the dashboard",
                           "; ".join(diff) or "no changes to links, checks or ignored containers", push=True)
        self.eng.trigger_check("settings")
        return 200, {"ok": True, "changes": diff}

    # ---- settings: GitHub + logs ---------------------------------------------
    def github_view(self):
        gh = self.cfg["github"]
        return {"remote": gh.get("remote", ""), "push_enabled": gh.get("push_enabled", True),
                "key": S.key_info(self.cfg), "last_push": self.eng.log.last_push,
                "push_interval": gh.get("push_interval", 300), "log_dir": str(self.eng.log.dir)}

    def do_github_key(self, body):
        info = S.generate_key(self.cfg, sh)
        if not self.cfg["github"].get("ssh_key"):
            with self.save_lock:
                S.save(self.cfg, {"github": {"ssh_key": S.DEFAULT_KEY}}, load_config)
            self.eng.log.reconfigure(self.cfg["github"])
        self.eng.log.event("info", "Created a GitHub deploy key from the Settings page", info["path"])
        return 200, {"key": info}

    def do_github_test(self, body):
        remote = S.normalize_remote(body.get("remote"))
        if not remote:
            raise ValueError("enter a repository first")
        return 200, dict(S.test_remote(self.cfg, remote, self.eng.log.dir, sh), remote=remote)

    def do_github_save(self, body):
        remote = S.normalize_remote(body.get("remote"))
        enabled = bool(body.get("push_enabled", True)) and bool(remote)
        if remote and enabled and remote != self.cfg["github"].get("remote"):
            t = S.test_remote(self.cfg, remote, self.eng.log.dir, sh)
            if not t["ok"]:
                return 400, {"error": t["detail"], "test": t}
        changes = {"remote": remote, "push_enabled": enabled}
        if not self.cfg["github"].get("ssh_key") and S.key_path(self.cfg).exists():
            changes["ssh_key"] = S.DEFAULT_KEY
        with self.save_lock:
            before = dict(self.cfg["github"])
            S.save(self.cfg, {"github": changes}, load_config)
            self.eng.log.reconfigure(self.cfg["github"])
        what = (f"log repo {remote}" if remote else "log repo disconnected") + \
               ("" if enabled or not remote else " (pushing off)")
        if before.get("remote") != remote or before.get("push_enabled", True) != enabled:
            self.eng.log.event("info", "GitHub settings saved from the dashboard", what, push=True)
        return 200, {"ok": True, "github": self.github_view()}

    def do_github_push(self, body):
        lg = self.eng.log
        if not lg.remote or not lg.push_enabled:
            return 409, {"error": "pushing is off or no repository is connected"}
        t = threading.Thread(target=lg.publish, daemon=True)
        t.start()
        t.join(90)
        return 200, {"last_push": lg.last_push}

    def do_settings_probe(self, body):
        url = S._url(body.get("url"), "URL")
        via = (body.get("via") or "").strip()
        if via:
            inv, _ = docker_inventory()
            if via not in (inv or {}):
                return 400, {"error": f"unknown container {via!r}"}
        code, rc, err = http_probe(url, via=via or None, timeout=8)
        ok = 200 <= code < 500
        return 200, {"ok": ok, "code": code, "detail": f"HTTP {code}" if code else (err[:200] or f"no response (curl {rc})")}

    def status(self):
        eng = self.eng
        st = eng.state.data
        upd = st.get("updates", {})
        applying = {s for a in self.actions.list() if a["kind"] == "update" and a["status"] == "running"
                    for s in a.get("services", [])}
        images = []
        for svc, e in sorted(upd.get("images", {}).items()):
            e = dict(e)
            if svc in applying:
                e["status"] = "applying"
            images.append(e)
        ai = eng.ai
        return {
            "box": self.cfg["box"]["name"],
            "ui": self.ui_version(),
            "now": now_iso(),
            "started": eng.started,
            "overall": eng.overall(),
            "checking": eng.checking or self.actions.running("check") is not None,
            "last_check": eng.last_check,
            "results": eng.results,
            "incidents": list(st.get("incidents", {}).values()),
            "updates": {"checked": upd.get("checked"), "images": images, "os": upd.get("os"),
                        "checking": eng.updater.running.locked(), "progress": eng.updater.progress_view()},
            "system": self.metrics.snapshot(),
            "reboot": eng.reboot_info(),
            "ai": {"enabled": ai.enabled, "current": ai.current, "queued": ai.q.qsize(), "jobs": ai.list(15)},
            "actions": self.actions.list()[:10],
            "events": list(eng.log.recent)[-60:][::-1],
            "push": eng.log.last_push,
            "remote": bool(eng.log.remote),
        }

    # ---- actions -----------------------------------------------------------
    def do_check(self, body):
        a = self.actions.running("check")
        if a:
            return 409, {"error": "a system check is already running", "action": a}
        eng = self.eng
        return 202, {"action": self.actions.start("check", "Full system check (with auto-fix)",
                                                  lambda: _summary(eng.check_cycle("manual")))}

    def do_version_check(self, body):
        a = self.actions.running("versions")
        if a:
            return 409, {"error": "a version check is already running", "action": a}
        eng = self.eng

        def run():
            # wait for a scheduled check that may hold the lock, then force a fresh one
            for _ in range(120):
                if not eng.updater.running.locked():
                    break
                time.sleep(2)
            s = eng.updater.check(reason="manual")
            eng._write_status()
            imgs = s.get("images", {}).values()
            return {"available": sum(1 for e in imgs if e["status"] in ("available", "pulled")),
                    "errors": sum(1 for e in imgs if e.get("error")),
                    "os": (s.get("os") or {}).get("count", 0)}

        return 202, {"action": self.actions.start("versions", "Version check (forced)", run)}

    def do_approve(self, body):
        services = body.get("services") or []
        known = self.eng.state.data.get("updates", {}).get("images", {})
        services = [s for s in services if isinstance(s, str) and s in known
                    and known[s]["status"] in ("available", "pulled", "rebuild_only")]
        if not services:
            return 400, {"error": "no approvable updates in request"}
        if self.actions.running("update"):
            return 409, {"error": "an update is already being applied"}
        eng = self.eng
        eng.log.event("update", f"Owner approved update: {', '.join(services)}", level="info")
        a = self.actions.start("update", f"Update {', '.join(services)}", lambda: eng.updater.apply(services))
        a["services"] = services
        return 202, {"action": a}

    def do_approve_os(self, body):
        if self.actions.running("update"):
            return 409, {"error": "an update is already being applied"}
        eng = self.eng
        eng.log.event("update", "Owner approved OS package upgrade", level="info")
        a = self.actions.start("update", "OS package upgrade", eng.updater.apply_os)
        a["services"] = []
        return 202, {"action": a}

    def do_reboot(self, body):
        eng = self.eng
        busy = []
        if eng.updater.progress.get("active"):
            busy.append("an update is being applied")
        if eng.ai.current:
            busy.append("an AI session is running")
        if busy and not body.get("force"):
            return 409, {"error": " and ".join(busy).capitalize() + ".", "busy": busy}
        ok, err = eng.request_reboot(by="owner (dashboard)", delay=15)
        return (202, {"ok": True, "reboot": eng.reboot_info()}) if ok else (409, {"error": err})

    def do_reboot_cancel(self, body):
        ok, err = self.eng.cancel_reboot()
        return (200, {"ok": True}) if ok else (409, {"error": err})

    def do_ai(self, kind, body):
        text = (body.get("text") or "").strip()
        if not text:
            return 400, {"error": "empty request"}
        if len(text) > 8000:
            return 400, {"error": "request too long (8000 characters max)"}
        if not self.eng.ai.enabled:
            return 503, {"error": "AI is disabled in this box's config"}
        title = ("Question: " if kind == "ask" else "Owner report: ") + text.splitlines()[0][:120]
        parent = None
        if kind == "followup":
            parent, err = self.eng.ai.can_follow_up(str(body.get("job") or ""))
            if err:
                return (409 if parent else 404), {"error": err}
            kind, title = parent["kind"], "Follow-up: " + text.splitlines()[0][:120]
        verb = "followed up" if parent else ("asked" if kind == "ask" else "reported")
        self.eng.log.event("ai", f"Owner {verb}: {text.splitlines()[0][:160]}",
                           text if "\n" in text or len(text) > 160 else "", level="info")
        job = self.eng.ai.submit(kind, title, text, parent=parent)
        return 202, {"job": {k: job[k] for k in ("id", "kind", "title", "status", "created")},
                     "position": self.eng.ai.q.qsize() + (1 if self.eng.ai.current else 0)}


def _view_job(job):
    """A job as the dashboard shows it: result block stripped from all text."""
    j = {k: job.get(k) for k in ("id", "kind", "title", "request", "status", "created", "started", "finished",
                                 "result", "cost_usd", "parent")}
    j["transcript"] = [dict(t, text=strip_result(t["text"])) if t["type"] == "text" else t for t in job["transcript"]]
    j["result_text"] = strip_result(job.get("result_text"))
    return j


def _summary(results):
    return {s: sum(1 for r in results if r["status"] == s) for s in ("ok", "warn", "fail")}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Arrgus"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("web %s - %s", self.address_string(), fmt % args)

        # ---- plumbing -------------------------------------------------------
        def _send(self, code, body, ctype="application/json", cookie=None, headers=None):
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body, default=str)
            if isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            if cookie is not None:  # settings session; "" clears it
                self.send_header("Set-Cookie", f"arrgus_settings={cookie}; Path=/; HttpOnly; Secure; SameSite=Strict"
                                 + ("; Max-Age=0" if not cookie else f"; Max-Age={S.SESSION_TTL}"))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authorized(self):
            got = self.headers.get("X-Arrgus-Proxy", "")
            return hmac.compare_digest(got.encode(), app.secret.encode())

        def _token(self):
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie") or "")
            except Exception:
                return ""
            return c["arrgus_settings"].value if "arrgus_settings" in c else ""

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(n) if n else b"{}"
            d = json.loads(raw or b"{}")
            if not isinstance(d, dict):
                raise ValueError("expected a JSON object")
            return d

        # ---- routes ---------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/healthz":
                return self._send(200, {"ok": True, "box": app.cfg["box"]["name"]})
            if not self._authorized():
                return self._send(403, {"error": "forbidden"})
            if path in ("/", "/index.html"):
                return self._send(200, app.render_page(), "text/html")
            if path == "/settings":
                return self._send(200, app.render_settings(), "text/html")
            if path == "/theme.css":
                return self._send(200, (WEB_DIR / "theme.css").read_text(), "text/css")
            if path == "/api/settings/auth":
                return self._send(200, app.settings_auth(self._token()))
            if path.startswith("/api/settings") and path != "/api/settings/auth":
                if not app.sessions.valid(self._token()):
                    return self._send(401, {"error": "sign in to Settings first"})
                return self._settings_get(path, parse_qs(urlparse(self.path).query))
            if path == "/api/status":
                return self._send(200, app.status())
            if path == "/api/ai/jobs":
                return self._send(200, app.eng.ai.list(40))
            if path.startswith("/api/ai/thread/"):
                turns = app.eng.ai.thread(path.rsplit("/", 1)[-1])
                if not turns:
                    return self._send(404, {"error": "no such conversation"})
                _, err = app.eng.ai.can_follow_up(turns[-1]["id"])
                return self._send(200, {"id": turns[0]["id"], "title": turns[0]["title"], "kind": turns[0]["kind"],
                                        "can_follow_up": err is None, "follow_up_error": err,
                                        "turns": [_view_job(j) for j in turns]})
            if path.startswith("/api/ai/jobs/"):
                job = app.eng.ai.get(path.rsplit("/", 1)[-1])
                if not job:
                    return self._send(404, {"error": "no such job"})
                q = parse_qs(urlparse(self.path).query)
                since = int((q.get("since") or ["0"])[0] or 0)
                j = dict(job)
                j["transcript_total"] = len(job["transcript"])
                j["transcript"] = [dict(t, text=strip_result(t["text"])) if t["type"] == "text" else t
                                   for t in job["transcript"][since:]]
                j["result_text"] = strip_result(job.get("result_text"))
                j["transcript_from"] = since
                return self._send(200, j)
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if not self._authorized():
                return self._send(403, {"error": "forbidden"})
            if self.headers.get("X-Arrgus") != "1" or "json" not in (self.headers.get("Content-Type") or ""):
                return self._send(400, {"error": "missing X-Arrgus header or JSON content type"})
            try:
                body = self._body()
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            routes = {
                "/api/check": app.do_check,
                "/api/versions": app.do_version_check,
                "/api/updates/approve": app.do_approve,
                "/api/updates/approve-os": app.do_approve_os,
                "/api/ai/ask": lambda b: app.do_ai("ask", b),
                "/api/ai/report": lambda b: app.do_ai("report", b),
                "/api/ai/followup": lambda b: app.do_ai("followup", b),
                "/api/reboot": app.do_reboot,
                "/api/reboot/cancel": app.do_reboot_cancel,
            }
            if path.startswith("/api/settings/"):
                return self._settings_post(path, body)
            fn = routes.get(path)
            if not fn:
                return self._send(404, {"error": "not found"})
            try:
                code, out = fn(body)
            except Exception as e:
                log.exception("POST %s failed", path)
                code, out = 500, {"error": str(e)}
            self._send(code, out)

        def _settings_get(self, path, q):
            one = lambda k: (q.get(k) or [""])[0]
            try:
                if path == "/api/settings":
                    return self._send(200, app.settings_view())
                if path == "/api/settings/github":
                    return self._send(200, app.github_view())
                if path == "/api/settings/logs":
                    return self._send(200, S.logs_view(app.eng.log.dir))
                if path == "/api/settings/logs/download":
                    box = app.cfg["box"]["name"]
                    if one("month"):
                        m, fmt = one("month"), one("fmt") or "md"
                        data = S.log_file(app.eng.log.dir, m, fmt)
                        return self._send(200, data, "text/markdown" if fmt == "md" else "application/x-ndjson",
                                          headers={"Content-Disposition": f'attachment; filename="arrgus-{box}-{m}.{fmt}"'})
                    data = S.logs_zip(app.eng.log.dir, box, include_box=one("box") == "1")
                    stamp = time.strftime("%Y%m%d-%H%M")
                    return self._send(200, data, "application/zip",
                                      headers={"Content-Disposition": f'attachment; filename="arrgus-{box}-log-{stamp}.zip"'})
            except FileNotFoundError:
                return self._send(404, {"error": "no log for that month"})
            except (ValueError, RuntimeError) as e:
                return self._send(400 if isinstance(e, ValueError) else 500, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def _settings_post(self, path, body):
            tok = self._token()
            try:
                if path == "/api/settings/login":
                    r = app.do_settings_login(body)
                elif path == "/api/settings/password":
                    r = app.do_settings_password(body, tok)
                elif path == "/api/settings/logout":
                    app.sessions.drop(tok)
                    r = (200, {"ok": True}, "")
                else:
                    if not app.sessions.valid(tok):
                        return self._send(401, {"error": "sign in to Settings first"})
                    fn = {"/api/settings/services": app.do_settings_services,
                          "/api/settings/probe": app.do_settings_probe,
                          "/api/settings/github": app.do_github_save,
                          "/api/settings/github/key": app.do_github_key,
                          "/api/settings/github/test": app.do_github_test,
                          "/api/settings/github/push": app.do_github_push}.get(path)
                    if not fn:
                        return self._send(404, {"error": "not found"})
                    r = fn(body)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                log.exception("POST %s failed", path)
                return self._send(500, {"error": str(e)})
            self._send(r[0], r[1], cookie=r[2] if len(r) > 2 else None)

    return Handler


def serve(cfg, engine):
    """Bind (retrying until the address exists, e.g. the docker bridge after boot) and serve forever."""
    w = cfg["web"]
    host, port = w.get("listen", "127.0.0.1"), int(w.get("port", 8765))
    app = App(cfg, engine)
    handler = make_handler(app)
    delay = 2
    while True:
        try:
            httpd = ThreadingHTTPServer((host, port), handler)
            break
        except OSError as e:
            log.warning("web: cannot bind %s:%s yet (%s); retrying in %ss", host, port, e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30)
    httpd.daemon_threads = True
    httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    log.info("web: dashboard listening on http://%s:%s", host, port)
    httpd.serve_forever()
