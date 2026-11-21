# Arrgus

An AI monitor for a Docker host. It watches containers, a VPN namespace, drives and the network; fixes known
problems with scripted playbooks; hands anything new (or any fix that fails) to an AI agent (Claude Code, OpenAI Codex or Google Gemini) running headless;
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
| `core/ai.py` | Queued headless AI sessions (one at a time), transcripts, follow-ups, result parsing |
| `core/agents.py` | The AI agents: Claude Code, OpenAI Codex, Google Gemini (command lines, output parsing, Test) |
| `core/updates.py` | Registry digest checks, apt check, approved apply with health check and rollback |
| `core/settings.py` | Settings page: overlay file, live re-apply, password + sessions, Services view and validation |
| `core/catalog.py` | Known apps (Plex, Jellyfin, the *arrs, download clients…) used to suggest health checks, links and icons |
| `core/logbook.py` | JSONL + Markdown log in a git repo, scrubbed of secrets, emails and public IPs, pushed periodically |
| `web/` | Stdlib HTTP server + single-page dashboard, Settings page, shared `theme.css` |
| `web/app/` | The installable phone app: page, manifest, service worker, icons |
| `config/` | One TOML per box (`example.toml` documents every key), plus `<box>.settings.json` from the Settings page |
| `deploy/` | systemd user unit + installer, optional sudoers rule, Caddy snippet |
| `box-notes.md` | Durable knowledge the AI reads and appends to |
| `state/` | Runtime state (incidents, AI jobs, proxy secret). Not committed anywhere. |

## Install on a box
1. `cp config/example.toml config/<box>.toml` and edit it.
2. Optional: connect a private GitHub repo for the log, either later from **Settings → Logs & GitHub**, or by hand:
   `ssh-keygen -t ed25519 -N '' -f ~/.ssh/arrgus_log_github`, add `~/.ssh/arrgus_log_github.pub` to the repo as a
   deploy key with write access, and set `[github] remote`. Without a repo the log is kept on the box only.
3. `deploy/install.sh config/<box>.toml` installs and starts the systemd user service and enables lingering so it
   starts at boot.
4. Point the reverse proxy at `web.listen:web.port` and add the header from `deploy/Caddyfile.snippet`
   (`state/proxy_secret` is created on first start).
5. Optional: install `deploy/sudoers.example` so mount/systemctl/apt fixes can run.
6. Set up the AI (below), or set `ai.enabled = false` to run without it.

## Setting up the AI
Arrgus has no AI of its own. It runs an AI coding agent headless on the box, as the same Linux user the service
runs as, so that user's sign-in is what pays for it. Choose the agent on **Settings → AI**:

| Agent | Account | Install (as the Arrgus user) | Sign in |
|---|---|---|---|
| **Claude Code** (default) | Claude subscription or Anthropic API key | `curl -fsSL https://claude.ai/install.sh \| bash` | run `claude` once and sign in |
| **OpenAI Codex** | ChatGPT plan or OpenAI API key | `npm install -g @openai/codex` | `codex login` → Sign in with ChatGPT |
| **Google Gemini** | Google account or Gemini API key | `npm install -g @google/gemini-cli` (Node.js 20+) | run `gemini` once → Login with Google |

Then press **Test** on the AI tab: it sends a one-word, read-only prompt to confirm the agent is installed and
signed in. Each agent reads the box briefing from its own file in `workspace/` (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`).

**API keys instead of a subscription** go in Arrgus's secrets file, which the systemd unit loads:
```sh
mkdir -p ~/.config/arrgus
install -m 600 /dev/null ~/.config/arrgus/env
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.config/arrgus/env   # or OPENAI_API_KEY=... / GEMINI_API_KEY=...
systemctl --user restart arrgus
```
Every value in that file is scrubbed from the published log, as is anything that looks like an Anthropic, OpenAI
or Google API key.

**What the AI may do** (`ai.access`, also on the AI tab):
- `"full"` (*Fix things*, default): the agent can run any command as the service user, plus whatever
  `deploy/sudoers.example` allows, then verifies its fix. Claude runs with `--dangerously-skip-permissions`, Codex
  with `--dangerously-bypass-approvals-and-sandbox`, Gemini with `--yolo`.
- `"read_only"` (*Investigate only*): it investigates and explains, and puts the exact fix in `owner_action` for
  you to apply. Claude runs in plan mode, Codex in its read-only sandbox, Gemini without auto-approval.
- Claude only: leave `access` unset and set `permission_mode` / `allowed_tools` for a custom Claude permission setup.

Usage is capped by `cooldown`, `max_runs_per_incident` and `max_auto_runs_per_day`; turn off
`auto_escalate` to run the AI only when you ask. Follow-up questions resume Claude's own session; with Codex and
Gemini the earlier turns are sent with the question. If an agent is missing or signed out, its sessions fail with
the install or sign-in step to take; monitoring and playbook fixes keep working.

Without the AI (`ai.enabled = false`), checks, playbook fixes, update approvals, the log and the dashboard all
still work; Ask AI, Report issue and automatic escalation are turned off.

## Dashboard
Status · System check (full check with auto-fix) · Version check (forced) · Ask AI · Report issue (AI
troubleshoots and fixes) · pending updates with Approve buttons · live AI transcripts · activity log · Settings.

## Phone app
`/app` is a separate, installable phone app (a PWA); the desktop dashboard is unchanged. Open
`https://<your dashboard>/app` on the phone, then iPhone: Safari → Share → **Add to Home Screen**; Android: browser
menu → **Install app**. It opens full screen with its own icon and has five tabs: **Home** (status, open issues,
updates with Approve, actions incl. Reboot), **Apps** (your links), **AI** (Ask / Report issue, conversations with
live replies and follow-ups), **System** (CPU, memory, temperature, drives, checks) and **Activity** (the log).
Pull down to refresh. When the server can't be reached (e.g. Tailscale is off) it shows the last known status.
Settings opens the normal Settings page, with a link back to the app. Icons are drawn by `tools/make_icons.py`.

## Settings page
`/settings` on the dashboard, protected by its own password (set on first visit; reset on the server with
`python3 arrgus.py set-password`). Five wrong passwords lock sign-in for five minutes.

- **Services:** every container in the compose file. For each one, choose whether Arrgus monitors it, gives it an
  HTTP health check (with a Test button), and shows it as a dashboard link. Known apps get suggested health-check
  URLs, link addresses and icons, so swapping Plex for Jellyfin or dropping Audiobookshelf is a few clicks.
  Links to things that aren't containers (a router, another server) go under *Other links*.
- **Logs & GitHub:** download the whole log as a zip (optionally with the box backups), or one month as Markdown
  or JSON Lines. Connect a GitHub repo for off-box copies: create a deploy key, enter `owner/repo`, test read and
  write access, turn pushing on or off, or push now. A repo that fails the test is never saved.
- **AI:** choose Claude Code, OpenAI Codex (ChatGPT) or Google Gemini, its model, whether it may fix things or only
  investigate, and whether unsolved problems go to it automatically. **Test** checks it's installed and signed in.
- **Password:** change the Settings password.

Changes are saved to `config/<box>.settings.json`, which overrides the same keys in the TOML (the TOML itself is
never rewritten), apply immediately without a restart, are recorded in the activity log, and are backed up to the
log repo with the other box files.

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
