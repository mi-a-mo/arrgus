"""AI troubleshooting via a headless coding agent (Claude Code, OpenAI Codex or Google Gemini; see agents.py).
One session at a time, queued."""
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import agents
from .util import ROOT, STATE_DIR, human_ts, log, now_iso

RESULT_SPEC = """
When you are done, end your final message with a fenced ```json block exactly like:
{"status": "fixed" | "partially_fixed" | "not_fixed" | "answered" | "needs_owner",
 "summary": "<one or two sentences: root cause and outcome>",
 "actions": ["<each change you actually made, past tense>"],
 "owner_action": "<what the owner must do, or empty string>"}
"""


_RESULT_BLOCK = re.compile(r"\s*```json\s*\{.*?\}\s*```\s*$", re.S)


def strip_result(text):
    """Remove the trailing machine-readable result block; it is for Arrgus, not for display."""
    return _RESULT_BLOCK.sub("", text or "").rstrip()


class AIRunner:
    def __init__(self, cfg, engine):
        self.cfg = cfg
        self.ai = cfg["ai"]
        self.engine = engine
        self.q = queue.Queue()
        self.jobs = {}
        self.current = None
        self.jobs_dir = STATE_DIR / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.workspace = Path(self.ai.get("workspace") or ROOT / "workspace")
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.notes = Path(self.ai.get("notes_file") or ROOT / "box-notes.md")
        for f in sorted(self.jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)[-40:]:
            try:
                j = json.loads(f.read_text())
                if j["status"] in ("queued", "running"):
                    j["status"] = "interrupted"
                self.jobs[j["id"]] = j
            except Exception:
                pass

    # ---- public ----------------------------------------------------------
    @property
    def enabled(self):
        return self.ai.get("enabled", True)

    def busy(self):
        return self.current is not None or not self.q.empty()

    @property
    def agent(self):
        return agents.current(self.ai)

    def submit(self, kind, title, request, incidents=None, parent=None):
        """Queue a session. With `parent` (a finished job), continue that conversation: Claude resumes its own
        session; other agents (or a different agent than before) get the earlier turns in the prompt."""
        jid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        ag = self.agent
        job = {"id": jid, "kind": kind, "title": title[:200], "request": request, "status": "queued",
               "created": now_iso(), "started": None, "finished": None, "transcript": [],
               "result": None, "result_text": None, "cost_usd": None, "incidents": incidents or [],
               "thread": jid, "parent": None, "agent": ag.id, "model": ag.model or "",
               "session_id": str(uuid.uuid4()) if ag.resumable else None, "resume": False}
        if parent:
            same = ag.resumable and (parent.get("agent") or "claude") == ag.id and parent.get("session_id")
            job.update(thread=parent.get("thread") or parent["id"], parent=parent["id"],
                       session_id=parent["session_id"] if same else job["session_id"], resume=bool(same),
                       incidents=parent.get("incidents") or [])
        self.jobs[jid] = job
        self._save(job)
        self.q.put(jid)
        return job

    def list(self, n=25):
        """One row per conversation (thread): the root's title, the latest turn's status, totals."""
        threads = {}
        for j in sorted(self.jobs.values(), key=lambda j: j["created"]):
            threads.setdefault(j.get("thread") or j["id"], []).append(j)
        rows = []
        for tid, js in threads.items():
            root, last = js[0], js[-1]
            row = {k: last.get(k) for k in ("status", "finished", "result")}
            row.update(id=root["id"], latest=last["id"], kind=root["kind"], title=root["title"],
                       created=root["created"], updated=last["created"], turns=len(js),
                       cost_usd=sum(j.get("cost_usd") or 0 for j in js) or None)
            rows.append(row)
        return sorted(rows, key=lambda r: r["updated"], reverse=True)[:n]

    def thread(self, jid):
        """All turns of the conversation containing job `jid`, oldest first."""
        j = self.jobs.get(jid)
        if not j:
            return []
        tid = j.get("thread") or j["id"]
        return sorted((x for x in self.jobs.values() if (x.get("thread") or x["id"]) == tid),
                      key=lambda x: x["created"])

    def can_follow_up(self, jid):
        """Returns (latest job of the thread, error or None)."""
        t = self.thread(jid)
        if not t:
            return None, "no such conversation"
        last = t[-1]
        if last["status"] in ("queued", "running"):
            return last, "the AI is still working on the previous message"
        return last, None

    def get(self, jid):
        return self.jobs.get(jid)

    def worker(self):
        while True:
            jid = self.q.get()
            job = self.jobs.get(jid)
            if not job:
                continue
            self.current = jid
            try:
                self._run(job)
            except Exception as e:
                log.exception("AI job crashed")
                job["status"], job["result"] = "failed", {"status": "not_fixed", "summary": f"runner error: {e}",
                                                          "actions": [], "owner_action": ""}
                self._save(job)
            finally:
                self.current = None

    # ---- internals -------------------------------------------------------
    def _save(self, job):
        p = self.jobs_dir / f"{job['id']}.json"
        p.write_text(json.dumps(job, default=str))

    def _write_context(self, ag):
        box = self.cfg["box"]
        eng = self.engine
        checks = "\n".join(f"- [{r['status'].upper()}] {r['name']}: {r['detail']}" for r in eng.results) or "(none yet)"
        events = "\n".join(f"- {human_ts(e['ts'])} {e['kind']}: {e['title']}" for e in list(eng.log.recent)[-25:])
        notes = self.notes.read_text() if self.notes.exists() else "(empty)"
        user = os.environ.get("USER", "unknown")
        if ag.access == "read_only":
            role = (f"You are running headless as user `{user}` in READ-ONLY mode: investigate and explain, but do not "
                    "change anything. Put the exact fix (commands, files to edit) in `owner_action`.")
        else:
            role = (f"You are running headless with full shell access as user `{user}` (member of the docker group). "
                    "Nobody is watching live: investigate, act, verify, then report.")
        (self.workspace / ag.context_file).write_text(f"""# You are Arrgus, the autonomous administrator of `{box['name']}`

{box.get('description', '')}

{role}

## Key facts
- Docker Compose project: `{box['compose_dir']}/{box.get('compose_file', 'docker-compose.yml')}`
  (secrets in `{box.get('env_file', '')}`; never print their values).
- Arrgus's own code: `{ROOT}`; this box's config: `{self.cfg.get('_path', ROOT / 'config.toml')}`. Its event log repo: `{eng.log.dir}` — do not edit it;
  Arrgus records your session automatically.
- Durable knowledge about this box lives in `{self.notes}`. Read it. If you learn something a future
  session should know (root causes, quirks, fixes that worked), append a dated bullet to it.
- {self.ai.get('sudo_note', 'You have no passwordless sudo. If a fix needs root, say so in `owner_action`.')}

## Operating rules
- Find the root cause before acting (logs: `docker logs --tail 200 <c>`, `docker inspect`, `journalctl`).
- Prefer the smallest fix that works. Verify it actually worked before you report "fixed".
- Before editing any file, copy it to `<file>.bak-arrgus-<YYYYMMDD-HHMM>`.
- Never delete media, downloads, or app config/databases. Never pull/upgrade images unless the request asks
  (updates are approved by the owner). Don't reboot unless the request asks or nothing else can work.
- Keep torrent traffic behind the VPN: if in doubt, stop the torrent client rather than leak.

## Current check results
{checks}

## Recent Arrgus events
{events}

## Box notes
{notes}
""")

    def _history(self, job):
        """Earlier turns of this conversation, for agents that can't resume their own session."""
        turns = [j for j in self.thread(job["id"]) if j["id"] != job["id"] and j["created"] <= job["created"]]
        parts = []
        for j in turns:
            parts.append(f"### Owner\n{j['request']}\n\n### You answered\n{strip_result(j.get('result_text')) or '(no answer)'}")
        text = "\n\n".join(parts)
        return text[-40_000:]

    def _prompt(self, job):
        if job.get("parent") and not job.get("resume"):
            return ("This continues an earlier conversation with the owner. Here it is so far:\n\n" + self._history(job) +
                    "\n\n---\nFollow-up from the owner. Investigate or act as needed (only change things if they ask "
                    "you to, or if it is part of fixing the reported problem).\n\nFollow-up: " + job["request"] + "\n" + RESULT_SPEC)
        if job.get("resume"):
            return ("Follow-up from the owner in this same conversation. Investigate or act as needed "
                    "(only change things if they ask you to, or if it is part of fixing the reported problem)."
                    "\n\nFollow-up: " + job["request"] + "\n" + RESULT_SPEC)
        if job["kind"] == "incident":
            body = ("Arrgus's monitoring found the issue(s) below and its scripted fixes did not resolve them "
                    "(or no scripted fix exists). Diagnose the root cause, fix it, and verify.\n\n" + job["request"])
        elif job["kind"] == "ask":
            body = ("The owner asks the question below. Investigate the live system as needed and answer clearly "
                    "and concisely. Only change things if the question asks you to.\n\nQuestion: " + job["request"])
        else:
            body = ("The owner reports the problem below. Troubleshoot it on the live system, fix it, and verify "
                    "the fix.\n\nReport: " + job["request"])
        return body + "\n" + RESULT_SPEC

    def _add(self, job, kind, text):
        job["transcript"].append({"t": now_iso(), "type": kind, "text": text})
        if len(job["transcript"]) % 3 == 0:
            self._save(job)

    def _run(self, job):
        eng = self.engine
        job["status"], job["started"] = "running", now_iso()
        self._save(job)
        ag = agents.AGENTS.get(job.get("agent") or "claude", agents.Claude)(self.ai)
        self._write_context(ag)
        if not ag.installed():
            job["result_text"] = ""
            job["result"] = {"status": "needs_owner", "summary": f"{ag.name} isn't installed at {ag.bin}.",
                             "actions": [], "owner_action": f"Install it ({ag.install}), then: {ag.signin}"}
            job["status"], job["finished"] = "failed", now_iso()
            self._save(job)
            self._publish(job)
            return
        prompt = self._prompt(job)
        rc, err = self._exec(ag, job, prompt, structured=True)
        if rc != 0 and not job["transcript"] and not job.get("result_text") and _flag_rejected(err):
            log.info("%s rejected its structured-output flag; retrying in plain-text mode", ag.name)
            rc, err = self._exec(ag, job, prompt, structured=False)
        job.pop("_last_text", None)
        job.pop("_buf", None)
        job["result"] = self._parse(job.get("result_text") or "", err, rc)
        job["status"] = "done" if rc == 0 else "failed"
        job["finished"] = now_iso()
        self._save(job)
        self._publish(job)
        if job["kind"] in ("incident", "report"):
            for iid in job["incidents"]:
                inc = eng.state.data["incidents"].get(iid)
                if inc:
                    inc["last_fixer"] = f"AI session {job['id']}"
            eng.trigger_check("ai-verify")

    def _exec(self, ag, job, prompt, structured):
        """Run the agent once. Returns (exit code, stderr tail). Fills job transcript/result_text."""
        cmd, stdin = ag.command(prompt, self.workspace, job, structured=structured)
        p = subprocess.Popen(cmd, cwd=self.workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=ag.env(), bufsize=1)
        errbuf = []
        t_err = threading.Thread(target=lambda: errbuf.append(p.stderr.read()), daemon=True)
        t_err.start()
        if stdin is not None:
            p.stdin.write(stdin)
        p.stdin.close()
        timer = threading.Timer(self.ai.get("timeout", 1800), p.kill)
        timer.start()
        plain = []
        add = lambda kind, text: self._add(job, kind, text)
        try:
            for line in p.stdout:
                if not structured:  # plain-text mode: everything is the answer, even lines that look like JSON
                    plain.append(line)
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    plain.append(line)
                    continue
                if not isinstance(ev, dict):
                    plain.append(line)
                    continue
                final = ag.handle(ev, job, add)
                if final is not None:
                    job["result_text"] = final
            p.wait(timeout=30)
        finally:
            timer.cancel()
        t_err.join(5)
        if not job.get("result_text"):
            text = job.get("_last_text") or "".join(plain).strip()
            if text:
                if not structured or not job["transcript"]:
                    self._add(job, "text", text)
                job["result_text"] = text
        return p.returncode, ("".join(errbuf))[-2000:]

    def _parse(self, text, err, rc):
        blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
        for b in reversed(blocks):
            try:
                d = json.loads(b)
                d.setdefault("actions", [])
                d.setdefault("owner_action", "")
                return d
            except ValueError:
                pass
        if rc != 0:
            return {"status": "not_fixed", "summary": f"AI session failed (exit {rc}): {err[-300:]}",
                    "actions": [], "owner_action": ""}
        return {"status": "answered", "summary": text.strip()[-600:], "actions": [], "owner_action": ""}

    def _publish(self, job):
        r = job["result"] or {}
        eng = self.engine
        lines = [f"# AI session {job['id']} — {job['title']}", "",
                 f"- Kind: {job['kind']}" + (f" (follow-up to [{job['parent']}]({job['parent']}.md))" if job.get("parent") else ""),
                 f"- Started: {job['started']}", f"- Finished: {job['finished']}",
                 f"- Agent: {agents.AGENTS.get(job.get('agent') or 'claude', agents.Claude).name}"
                 + (f" ({job.get('model') or self.ai.get('model')})" if (job.get('model') or self.ai.get('model')) else ""),
                 f"- Outcome: **{r.get('status')}**", "",
                 "## Request", "", job["request"], "", "## Answer", "", strip_result(job.get("result_text")) or "(none)", "",
                 "## Transcript", ""]
        for s in job["transcript"]:
            if s["type"] == "text":
                if strip_result(s["text"]):
                    lines += [strip_result(s["text"]), ""]
            elif s["type"] == "tool":
                lines += ["```", "$ " + s["text"], "```"]
            else:
                lines += ["<details><summary>output</summary>", "", "```",
                          s["text"][:1500].replace("```", "'''"), "```", "</details>", ""]
        rel = f"ai/{job['id']}.md"
        eng.log.write_file(rel, "\n".join(lines))
        detail = r.get("summary", "")
        if r.get("actions"):
            detail += "\nActions:\n" + "\n".join(f"- {a}" for a in r["actions"])
        if r.get("owner_action"):
            detail += f"\nOwner action needed: {r['owner_action']}"
        detail += f"\nTranscript: {rel}"
        level = {"fixed": "ok", "answered": "info"}.get(r.get("status"), "error")
        eng.log.event("ai", f"[{r.get('status', '?')}] {job['title']}", detail, level=level, push=True,
                      data={"job": job["id"]})
        if r.get("status") in ("not_fixed", "needs_owner", "partially_fixed"):
            eng.notify(f"Arrgus AI needs you on {self.cfg['box']['name']}", detail[:800])


def _flag_rejected(err):
    """True if the CLI refused an option (older/newer version): we then retry without structured output."""
    e = (err or "").lower()
    return any(k in e for k in ("unknown argument", "unknown option", "unexpected argument", "unrecognized",
                                "unknown arguments", "invalid value"))

