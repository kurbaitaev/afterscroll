---
name: verify
description: Prove a saved-to-notes change works before committing or deploying it. Use before every commit in this repo, before saying a change is done, and after a change goes live on Netcup. Runs the same checks as CI, then a read-only health check of the live bot.
---

# verify (saved-to-notes)

## 1. Quick check, every change

```
bash .claude/skills/verify/quick.sh
```

Same checks as CI: compile, ruff (pinned 0.9.10), `tests/test_pipeline.py`, `tests/test_cards.py`, shellcheck. Ends with `VERIFY PASSED` or `VERIFY FAILED`. About 5 seconds. Needs `.venv` once (the script's header says how; the laptop and Netcup both have one). Add a regression test to `tests/` for any bug you fix.

## 2. Live check, after the change is on Netcup

The bot runs on Netcup as user services (`afterscroll.service` plus timers). Push and restart only when the task is to deploy it. Then read, without changing anything:

```
ssh netcup-work 'cd ~/afterscroll && git log --oneline -1 && systemctl --user is-active afterscroll && journalctl --user -u afterscroll --since "-10 min" -p warning --no-pager | tail -20'
```

Done when Netcup is on the commit you pushed, the service is `active`, and there are no new errors. If the change is to card handling, the real proof is one card going through end to end. Ask Magomed to save one post only if you can't produce one yourself.

## If a check fails

Fix the cause. Never skip a test, add sleeps, or loosen an assertion to get green. After three failed fix rounds, stop and tell him what fails and what was tried. A red CI nobody fixes stops being a check.
