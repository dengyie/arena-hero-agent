# Arena Hero Agent

A conservative, tick-driven Arena Hero agent.

Game: https://app.arenahero.io/arena
Docs: https://doc.arenahero.io/

This first version implements a safe Worker economy loop:

- receive authoritative `tick`/`state` messages over WebSocket;
- replace state snapshots instead of merging them;
- move Workers to visible resource cells;
- harvest, return to Core, and deposit cargo;
- current combat remains defensive-only: existing Vanguard/Ranger actions are
  gated by current private state, with no Beacon or pursuit policy;
- journal state, plan, receipts, and resolution events;
- support dry-run by default and live mode only with `--live`.

The implementation speaks the documented protocol directly so it runs on pxed's Python 3.10. It does not require a browser token or DOM injection.

## Local

```bash
cd /Users/mango/project/arena-hero-agent
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'
python -m arena_agent --dry-run --max-ticks 1
```

Live mode requires an Agent token supplied out-of-band:

```bash
export ARENA_HERO_TOKEN='[REDACTED]'
python -m arena_agent --live
```

Never put the token in source, command history, journal, or Git.

## pxed

The deployment script clones/pulls the repository, creates a Python 3.10 venv, installs dependencies, runs tests, writes a redacted environment file, and installs/restarts only the Arena Hero Supervisor program.

```bash
./deploy/pxed-deploy.sh --repo https://github.com/arena-hero/arena-hero-agent.git
```

It refuses live startup unless `ARENA_HERO_TOKEN` is already present in the protected environment file. Default deployed mode remains dry-run.

## Policy

The agent submits a complete plan once per state, with an idempotency key per tick. HTTP 202 means stored, not successful; actual results are read from the next state's events. A manual plan remains higher priority on the server.

## Combat status

The current verified implementation contains conservative Core-local Vanguard
SWEEP/Ranger precision-fire gates, combat episode accounting, and friendly-loss
cooldowns. It does not yet produce combat units or run a complete defensive
squad. The design for that next milestone is in
`docs/DEV-COMBAT-SYSTEM-V2.md`; implementation and live behavior remain behind
an explicit approval and staged event-level acceptance.
