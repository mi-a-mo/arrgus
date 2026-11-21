# Arrgus

An AI monitor for a Docker host. It watches containers, a VPN namespace, drives and the network; fixes known
problems with scripted playbooks; hands anything new (or any fix that fails) to Claude Opus running headless;
reports image updates and applies them only after the owner approves; and logs every issue, fix, update and AI
session to a private GitHub repo. The web dashboard sits behind your existing reverse proxy.

The code is generic. Everything about a particular box lives in `config/<box>.toml`.

## Layout
| Path | What |
|---|---|
| `arrgus.py` | CLI entry point: `serve`, `check [--fix]`, `boot-dryrun`, `versions`, `publish` |
| `core/checks.py` | Health checks: containers, VPN namespace + egress leak, HTTP probes, mounts, bind visibility, disk, DNS/internet/Tailscale, system |
| `core/fixes.py` | Playbook fixes: compose up, restart, VPN restart and re-attach, mount and restart, systemctl |
| `core/engine.py` | Check → confirm (15s) → fix → verify → escalate loop; post-reboot sequence; Docker event watcher |
| `core/ai.py` | Queued headless `claude -p` sessions (one at a time), transcripts, result parsing |
| `core/updates.py` | Registry digest checks, apt check, approved apply with health check and rollback |
| `core/logbook.py` | JSONL + Markdown log in a git repo, scrubbed of secrets, emails and public IPs, pushed periodically |
| `web/` | Stdlib HTTP server + single-page dashboard |
| `config/` | One TOML per box (`example.toml` documents every key) |
| `deploy/` | systemd user unit + installer, optional sudoers rule, Caddy snippet |
| `box-notes.md` | Durable knowledge the AI reads and appends to |
| `state/` | Runtime state (incidents, AI jobs, proxy secret). Not committed anywhere. |

## Install on a box
1. `cp config/example.toml config/<box>.toml` and edit it.
2. Create the log repo on GitHub (private) and a deploy key with write access:
   `ssh-keygen -t ed25519 -N '' -f ~/.ssh/arrgus_log_github`, then add `~/.ssh/arrgus_log_github.pub` as a deploy key
   with write access to the repo. Set `[github] remote`.
3. `deploy/install.sh config/<box>.toml` installs and starts the systemd user service and enables lingering so it
   starts at boot.
4. Point the reverse proxy at `web.listen:web.port` and add the header from `deploy/Caddyfile.snippet`
   (`state/proxy_secret` is created on first start).
5. Optional: install `deploy/sudoers.example` so mount/systemctl/apt fixes can run.
6. Set up the AI (below), or set `ai.enabled = false` to run without it.

## Setting up the AI
Arrgus has no AI of its own. It runs [Claude Code](https://docs.claude.com/en/docs/claude-code) headless
(`claude -p`) as the same Linux user the service runs as, so that user's Claude Code sign-in is what pays for it.

1. Install Claude Code for that user. The config expects it at `~/.local/bin/claude` (`ai.claude_bin`).
2. Sign it in, one of two ways:
   - **Claude subscription:** run `claude` once as that user and sign in. Every AI session counts against that
     plan's usage, and the plan must include the model in `ai.model` (default: Opus).
   - **API key (pay per use):** put the key in Arrgus's secrets file, which the systemd unit loads:
     ```sh
     mkdir -p ~/.config/arrgus
     install -m 600 /dev/null ~/.config/arrgus/env
     echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.config/arrgus/env
     systemctl --user restart arrgus
     ```
     Every value in that file is scrubbed from the published log, as is anything that looks like an Anthropic key.
3. Test it with **Ask AI** on the dashboard. If Claude Code is missing or signed out, AI sessions fail and show the
   error on the dashboard; monitoring and playbook fixes keep working.

**What the AI is allowed to do.** By default (`ai.permission_mode = "bypassPermissions"`) the AI can run any
command as the service user, plus whatever `deploy/sudoers.example` allows. That is what lets it actually fix
things, but it is full control of the account. For a stricter setup, set `permission_mode` to another Claude Code
permission mode and list what it may use in `ai.allowed_tools`. Usage is capped by `cooldown`,
`max_runs_per_incident` and `max_auto_runs_per_day`.

Without the AI (`ai.enabled = false`), checks, playbook fixes, update approvals, the log and the dashboard all
still work; Ask AI, Report issue and automatic escalation are turned off.

## Dashboard
Status · System check (full check with auto-fix) · Version check (forced) · Ask AI · Report issue (AI
troubleshoots and fixes) · pending updates with Approve buttons · live AI transcripts · activity log.

Access control: the server only listens on an address the proxy can reach, refuses any request without the
proxy-injected `X-Arrgus-Proxy` secret, and requires `X-Arrgus: 1` + JSON on every POST (CSRF).

## AI sessions
Started only by the running service: automatically, for incidents the playbooks can't fix
(`ai.auto_escalate`, rate-limited by `cooldown`, `max_runs_per_incident`, `max_auto_runs_per_day`), or by the owner
from the dashboard. One-shot CLI commands never start an AI session. Transcripts are written to `ai/` in the log repo.

## Testing without touching production
Use a separate state dir and config so tests don't clobber the live service:
`ARRGUS_STATE_DIR=/tmp/x/state python3 arrgus.py --config /tmp/x/test.toml check`.
Point `ai.claude_bin` at a stub that prints stream-json to exercise the AI path without calling a model.

## License
Free for noncommercial use under the [PolyForm Noncommercial License 1.0.0](LICENSE): personal use, hobby
projects, research, education and charities. Commercial use is not permitted; contact
[mi-a-mo](https://github.com/mi-a-mo) to ask about it.
