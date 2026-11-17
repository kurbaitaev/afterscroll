## Checks

Before committing or saying a change is done, use the `verify` skill (`.claude/skills/verify/`). Its quick check is `bash .claude/skills/verify/quick.sh`: the same compile, ruff, test and shellcheck steps as CI (`.github/workflows/ci.yml`), about 5 s. It needs `.venv` once: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt 'ruff==0.9.10'`. On 2026-11-16 it passed on the laptop and CI was green. After a deploy, run the skill's live check against Netcup.
