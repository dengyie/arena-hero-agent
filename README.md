# Arena Hero Agent

A conservative, tick-driven Arena Hero agent.

Game: https://app.arenahero.io/arena
Docs: https://doc.arenahero.io/

The production agent implements a conservative tick-driven protocol client:

- receive authoritative `tick`/`state` messages over WebSocket;
- replace state snapshots instead of merging them;
- move Workers to visible resource cells;
- harvest, return to Core, and deposit cargo;
- v0.13 two-entity cell capacity and serialized Core ingress;
- guarded Vanguard/Ranger production, Core-local defense, and current-state
  precision/SWEEP gates;
- resource journal, plan/receipt settlement, resolution events, rotated history,
  and domain-separated economy/Combat evaluation;
- support dry-run by default and live mode only with `--live`.

The production implementation speaks the documented protocol directly so it runs on
pxed's Python 3.10. The official typed `arena-hero==0.2.9` SDK is tracked separately
in `requirements-sdk.txt` and requires Python 3.11+; it is not installed into the
current production runtime until an Arena-only 3.11 staging environment passes the
same protocol and live-resolution gates. Neither path requires browser DOM injection.

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

## SDK 0.2.9 compatibility

For development on Python 3.11+:

```bash
python3.13 -m venv .venv-sdk
. .venv-sdk/bin/activate
pip install -r requirements-sdk.txt
python - <<'PY'
from arena_hero import UnitType, core_resource_capacity, unit_cost
assert core_resource_capacity(3) == 15
assert unit_cost(UnitType.RANGER, 20) == 16
PY
```

The local compatibility helpers in `arena_agent.policy` intentionally match SDK
0.2.9's exact integer dynamic-pricing formula. Do not use floating-point price
multipliers or install the SDK into pxed's Python 3.10 interpreter.

## Combat status

The verified implementation includes guarded Vanguard/Ranger production,
Core-local and current-threat escort positioning, SWEEP/Ranger precision gates,
combat episode attribution, and friendly-loss cooldowns. Clean live SWEEP,
precision, and cell-intercept episode acceptance remains evidence-gated in
`docs/DEV-COMBAT-SYSTEM-V2.md`.
