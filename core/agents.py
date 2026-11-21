"""The AI agents Arrgus can hand problems to. Each one is a coding-agent CLI run headless on this box.

- Claude Code (`claude -p`): Claude subscription or Anthropic API key.
- OpenAI Codex CLI (`codex exec`): ChatGPT plan or OpenAI API key.
- Google Gemini CLI (`gemini -p`): Google account or Gemini API key.

An adapter builds the command line and turns the CLI's output into transcript entries. Claude's stream-json
format is well known. Codex and Gemini output formats have changed between versions, so those parsers accept
several event shapes and, if a CLI rejects the machine-readable output flag, Arrgus re-runs it in plain-text
mode. Follow-up questions resume the session with Claude; for the others the earlier turns are included in
the prompt, which works with any version.
"""
import json
import os
import shutil
from pathlib import Path

MAX_ARG_PROMPT = 100_000  # prompts passed on the command line stay below Linux's 128 KiB per-argument limit


class Agent:
    id = name = vendor = ""
    default_bin = ""
    context_file = "AGENTS.md"
    default_model = ""
    resumable = False
    install = signin = ""

    def __init__(self, ai):
        self.ai = ai                      # the [ai] config section
        self.conf = ai.get(self.id) or {}  # the [ai.<id>] section

    # ---- configuration --------------------------------------------------
    @property
    def bin(self):
        b = self.conf.get("bin") or self.default_bin
        b = os.path.expanduser(b)
        if os.sep in b:
            return b
        return shutil.which(b, path=f"{Path.home()}/.local/bin:{os.environ.get('PATH', '/usr/bin:/bin')}") or b

    @property
    def model(self):
        return self.conf.get("model") if self.conf.get("model") is not None else self.default_model

    @property
    def access(self):
        return access_of(self.ai)

    def installed(self):
        return os.path.isfile(self.bin) and os.access(self.bin, os.X_OK)

    def env(self):
        env = os.environ.copy()
        env["PATH"] = f"{Path.home()}/.local/bin:" + env.get("PATH", "/usr/bin:/bin")
        return env

    # ---- running --------------------------------------------------------
    def command(self, prompt, workspace, job, structured=True):
        """(argv, stdin text or None)."""
        raise NotImplementedError

    def handle(self, ev, job, add):
        """Handle one parsed JSON event. Returns the final answer text if this event carries it."""
        return None


class Claude(Agent):
    id, name, vendor = "claude", "Claude Code", "Anthropic"
    default_bin = "~/.local/bin/claude"
    context_file = "CLAUDE.md"
    default_model = "claude-opus-5-5"
    resumable = True
    install = "curl -fsSL https://claude.ai/install.sh | bash"
    signin = ("Run `claude` once on the server as this user and sign in with your Claude account, "
              "or put ANTHROPIC_API_KEY=… in ~/.config/arrgus/env.")

    def __init__(self, ai):
        super().__init__(ai)
        # older configs keep these at the top of [ai]
        if "bin" not in self.conf and ai.get("claude_bin"):
            self.conf = dict(self.conf, bin=ai["claude_bin"])
        if "model" not in self.conf and ai.get("model"):
            self.conf = dict(self.conf, model=ai["model"])

    def command(self, prompt, workspace, job, structured=True):
        cmd = [self.bin, "-p", "--output-format", "stream-json", "--verbose"]
        if self.model:
            cmd += ["--model", self.model]
        if job.get("session_id"):
            cmd += ["--resume" if job.get("resume") else "--session-id", job["session_id"]]
        mode = self.ai.get("permission_mode", "bypassPermissions")
        if self.access == "read_only":
            cmd += ["--permission-mode", "plan"]
        elif self.access == "full":
            cmd.append("--dangerously-skip-permissions")
        else:  # a custom Claude permission mode set in the config file
            cmd += ["--permission-mode", mode]
            if self.ai.get("allowed_tools"):
                cmd += ["--allowedTools", *self.ai["allowed_tools"]]
        return cmd, prompt

    def handle(self, ev, job, add):
        t = ev.get("type")
        if ev.get("session_id") and ev["session_id"] != job.get("session_id"):
            job["session_id"] = ev["session_id"]  # trust what Claude reports
        if t == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "text" and b.get("text", "").strip():
                    add("text", b["text"])
                elif b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    s = inp.get("command") or inp.get("file_path") or inp.get("pattern") or json.dumps(inp)
                    add("tool", f"{b.get('name')}: {s}"[:2000])
        elif t == "user":
            content = (ev.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    add("output", str(c)[:3000])
        elif t == "result":
            job["cost_usd"] = ev.get("total_cost_usd")
            return ev.get("result") or ""
        return None


class Codex(Agent):
    id, name, vendor = "codex", "OpenAI Codex", "OpenAI"
    default_bin = "codex"
    context_file = "AGENTS.md"
    install = "npm install -g @openai/codex   (needs Node.js; or download a release binary from github.com/openai/codex)"
    signin = ("Run `codex login` on the server as this user and choose “Sign in with ChatGPT”, "
              "or put OPENAI_API_KEY=… in ~/.config/arrgus/env.")

    def command(self, prompt, workspace, job, structured=True):
        cmd = [self.bin, "exec", "--skip-git-repo-check", "-C", str(workspace)]
        if structured:
            cmd.append("--json")
        if self.model:
            cmd += ["-m", self.model]
        cmd += ["--dangerously-bypass-approvals-and-sandbox"] if self.access == "full" else ["--sandbox", "read-only"]
        return cmd + ["--", prompt[-MAX_ARG_PROMPT:]], None

    def handle(self, ev, job, add):
        t = ev.get("type")
        if t == "thread.started" and ev.get("thread_id"):
            job["agent_session"] = ev["thread_id"]
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        if item and t in ("item.completed", "item.started"):
            it = item.get("type") or item.get("item_type")
            if it in ("agent_message", "assistant_message") and t == "item.completed":
                text = item.get("text") or ""
                if text.strip():
                    add("text", text)
                    job["_last_text"] = text
            elif it == "command_execution":
                if t == "item.started":
                    add("tool", f"shell: {item.get('command')}"[:2000])
                else:
                    out = item.get("aggregated_output") or item.get("output") or ""
                    add("output", f"{out}\n(exit {item.get('exit_code')})"[:3000])
            elif it == "file_change" and t == "item.completed":
                add("tool", "edit: " + ", ".join(c.get("path", "?") for c in item.get("changes") or [])[:2000])
        msg = ev.get("msg") if isinstance(ev.get("msg"), dict) else None  # older event format
        if msg:
            mt = msg.get("type")
            if mt == "agent_message" and msg.get("message"):
                add("text", msg["message"])
                job["_last_text"] = msg["message"]
            elif mt == "exec_command_begin":
                c = msg.get("command")
                add("tool", "shell: " + (" ".join(c) if isinstance(c, list) else str(c))[:2000])
            elif mt == "exec_command_end":
                add("output", (msg.get("aggregated_output") or msg.get("stdout") or "")[:3000])
            elif mt == "task_complete":
                return msg.get("last_agent_message") or job.get("_last_text") or ""
        if t in ("turn.completed", "task_complete"):
            return job.get("_last_text") or ""
        if t in ("error", "turn.failed"):
            e = ev.get("message") or (ev.get("error") or {}).get("message")
            if e:
                add("output", f"error: {e}"[:3000])
        return None


class Gemini(Agent):
    id, name, vendor = "gemini", "Google Gemini", "Google"
    default_bin = "gemini"
    context_file = "GEMINI.md"
    install = "npm install -g @google/gemini-cli   (needs Node.js 20 or later)"
    signin = ("Run `gemini` once on the server as this user and choose “Login with Google”, "
              "or put GEMINI_API_KEY=… in ~/.config/arrgus/env.")

    def command(self, prompt, workspace, job, structured=True):
        cmd = [self.bin]
        if self.model:
            cmd += ["-m", self.model]
        if structured:
            cmd += ["--output-format", "stream-json"]
        if self.access == "full":
            cmd.append("--yolo")
        return cmd + ["-p", prompt[-MAX_ARG_PROMPT:]], None

    def handle(self, ev, job, add):
        t = ev.get("type")
        if t == "init" and ev.get("session_id"):
            job["agent_session"] = ev["session_id"]
        elif t == "message" and ev.get("role") == "assistant":
            c = ev.get("content") or ""
            if ev.get("delta"):
                job["_buf"] = job.get("_buf", "") + c
            elif c.strip():
                self._flush(job, add)
                add("text", c)
                job["_last_text"] = c
        elif t == "tool_use":
            self._flush(job, add)
            p = ev.get("parameters") or {}
            s = p.get("command") or p.get("file_path") or p.get("absolute_path") or json.dumps(p)
            add("tool", f"{ev.get('tool_name')}: {s}"[:2000])
        elif t == "tool_result":
            add("output", str(ev.get("output") or ev.get("error") or ev.get("status") or "")[:3000])
        elif t == "error":
            add("output", f"error: {ev.get('message') or ev}"[:3000])
        elif t == "result":
            self._flush(job, add)
            return job.get("_last_text") or ev.get("response") or ""
        elif "response" in ev and t is None:  # --output-format json (single object)
            return ev.get("response") or ""
        return None

    @staticmethod
    def _flush(job, add):
        buf = job.pop("_buf", "")
        if buf.strip():
            add("text", buf)
            job["_last_text"] = buf


AGENTS = {a.id: a for a in (Claude, Codex, Gemini)}


def access_of(ai):
    """'full' (fix things), 'read_only' (investigate only) or 'custom' (a Claude permission mode from the config)."""
    if ai.get("access") in ("full", "read_only"):
        return ai["access"]
    return "full" if ai.get("permission_mode", "bypassPermissions") == "bypassPermissions" else "custom"


def current(ai):
    return AGENTS.get(ai.get("agent") or "claude", Claude)(ai)


def version(ag):
    import subprocess
    if not ag.installed():
        return ""
    try:
        p = subprocess.run([ag.bin, "--version"], capture_output=True, text=True, timeout=15, env=ag.env())
        return (p.stdout or p.stderr).strip().splitlines()[0][:80] if (p.stdout or p.stderr).strip() else ""
    except Exception:
        return ""


_SIGNIN_HINTS = ("login", "log in", "sign in", "signin", "authenticat", "api key", "api_key", "unauthorized", "401",
                 "credentials", "not logged")


def probe(ag, workdir, timeout=120):
    """Ask the agent for a one-word reply, read-only, to show it is installed and signed in.
    Nothing is recorded as an AI session."""
    import subprocess
    if not ag.installed():
        return {"ok": False, "detail": f"{ag.name} isn't installed at {ag.bin}.", "fix": f"Install it: {ag.install}"}
    ver = version(ag)
    ro = dict(ag.ai, access="read_only")
    test = type(ag)(ro)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    job = {"session_id": None, "resume": False}
    cmd, stdin = test.command("Reply with exactly ARRGUS-OK and nothing else. Do not use any tools.", workdir, job)
    try:
        p = subprocess.run(cmd, input=stdin or "", capture_output=True, text=True, timeout=timeout, cwd=workdir, env=test.env())
    except subprocess.TimeoutExpired:
        return {"ok": False, "version": ver, "detail": f"No answer within {timeout}s.", "fix": test.signin}
    texts = []
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            texts.append(line)
            continue
        if isinstance(ev, dict):
            final = test.handle(ev, job, lambda kind, t: texts.append(t) if kind == "text" else None)
            if final:
                texts.append(final)
    answer = " ".join(t.strip() for t in texts if t.strip())[-300:]
    if p.returncode == 0 and "ARRGUS-OK" in answer.upper():
        return {"ok": True, "version": ver, "detail": f"{ag.name} answered" + (f" using {ag.model}" if ag.model else "") + "."}
    err = (p.stderr or "") + "\n" + answer
    signin = any(h in err.lower() for h in _SIGNIN_HINTS)
    return {"ok": False, "version": ver,
            "detail": (err.strip()[-400:] or f"exit code {p.returncode}"),
            "fix": test.signin if signin or p.returncode else ""}
