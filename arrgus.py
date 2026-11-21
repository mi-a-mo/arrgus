#!/usr/bin/env python3
"""Arrgus: AI monitor for a Docker host.

  arrgus.py serve            run the monitor + web dashboard (what the systemd unit runs)
  arrgus.py check [--fix]    run one check cycle and print results (no fixes unless --fix)
  arrgus.py boot-dryrun      run the post-reboot sequence now, without applying fixes
  arrgus.py versions         run an image/OS update check and print what is pending
  arrgus.py publish          commit and push the log repo now

The box-specific config comes from ARRGUS_CONFIG or --config (default: ./config.toml).
"""
import argparse
import logging
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.engine import Engine  # noqa: E402
from core.util import load_config, log  # noqa: E402


def print_results(results):
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    for r in sorted(results, key=lambda r: ({"fail": 0, "warn": 1}.get(r["status"], 2), r["group"], r["name"])):
        print(f"[{icon.get(r['status'], r['status'])}] {r['group']:<10} {r['name']}: {r['detail']}")
    n = {s: sum(r["status"] == s for r in results) for s in ("ok", "warn", "fail")}
    print(f"\n{n['ok']} ok, {n['warn']} warn, {n['fail']} fail")


def main():
    ap = argparse.ArgumentParser(description="Arrgus Docker-host monitor")
    ap.add_argument("--config", help="path to the box config (overrides ARRGUS_CONFIG)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    c = sub.add_parser("check")
    c.add_argument("--fix", action="store_true", help="apply playbook fixes (never escalates to AI)")
    sub.add_parser("boot-dryrun")
    sub.add_parser("versions")
    sub.add_parser("publish")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s" if sys.stderr.isatty() else "%(levelname)s %(message)s")
    cfg = load_config(a.config)

    if a.cmd == "serve":
        from web.server import serve
        eng = Engine(cfg)
        eng.start()
        serve(cfg, eng)  # blocks
        return

    # One-shot commands: never start the AI worker, so nothing here can launch an AI session.
    if a.cmd != "serve":
        cfg["ai"]["auto_escalate"] = False
    eng = Engine(cfg)
    if a.cmd == "check":
        print_results(eng.check_cycle("cli", allow_fix=a.fix))
    elif a.cmd == "boot-dryrun":
        eng.boot_sequence(force_boot=True, allow_fix=False)
        print_results(eng.results)
    elif a.cmd == "versions":
        s = eng.updater.check(reason="manual")
        for svc, e in sorted(s.get("images", {}).items()):
            extra = f" ({e['error']})" if e.get("error") else ""
            print(f"{e['status']:<13} {svc}: {e.get('current') or '?'} -> {e.get('available') or '-'}{extra}")
        print(f"OS packages upgradable: {s['os']['count']} ({s['os']['security']} security)")
    if a.cmd == "publish" or a.cmd != "serve":
        t = threading.Thread(target=eng.log.publish)
        t.start()
        t.join(180)
        print("push:", eng.log.last_push)


if __name__ == "__main__":
    main()
