from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def evaluate_combat_session(journal_path: Path | str, session: str) -> dict[str, Any]:
    plans: dict[int, dict[str, Any]] = {}
    received: set[int] = set()
    with Path(journal_path).open(encoding="utf-8", errors="replace") as journal:
        for line in journal:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if row.get("session") != session:
                continue
            if row.get("event") == "plan" and isinstance(row.get("tick"), int):
                plans[row["tick"]] = row
            elif row.get("event") == "received":
                tick = (row.get("data") or {}).get("tick")
                if isinstance(tick, int):
                    received.add(tick)

    resolved = sorted(
        tick for tick, row in plans.items()
        if tick in received and (row.get("result") or {}).get("status") == 202
    )
    eligible = 0
    event_types: dict[str, int] = {}
    episodes_by_key: dict[str, dict[str, Any]] = {}
    for tick in resolved:
        state = plans[tick].get("state") or {}
        if state.get("metric_window_eligible") is True:
            eligible += 1
        for event in state.get("events") or []:
            kind = event.get("event_type")
            if kind:
                event_types[kind] = event_types.get(kind, 0) + 1
        evaluation = state.get("phase_evaluation") or {}
        for episode in evaluation.get("clean_combat_episodes") or []:
            key = json.dumps(episode, sort_keys=True, separators=(",", ":"))
            episodes_by_key[key] = episode

    episodes = list(episodes_by_key.values())
    clean = [episode for episode in episodes if episode.get("outcome") == "CLEAN_COMPLETE"]
    friendly_deaths = sum(int(episode.get("friendly_deaths", 0) or 0) for episode in clean)
    cargo_lost = sum(int(episode.get("friendly_cargo_lost", 0) or 0) for episode in clean)
    sweep_episodes = sum(int(episode.get("sweeps", 0) or 0) for episode in clean)
    precision_episodes = sum(int(episode.get("precision_shots", 0) or 0) for episode in clean)
    cell_episodes = sum(int(episode.get("cell_intercept_shots", 0) or 0) for episode in clean)

    blocking: list[str] = []
    if eligible < 50:
        blocking.append("eligible_resolved_ticks<50")
    if len(clean) < 3:
        blocking.append("clean_complete_episodes<3")
    if friendly_deaths:
        blocking.append("confirmed_friendly_deaths>0")
    if cargo_lost:
        blocking.append("friendly_cargo_lost>0")
    if not (sweep_episodes and event_types.get("SWEEP_RESOLVED", 0)):
        blocking.append("sweep_event_coverage_missing")
    shot_events = event_types.get("SHOT_HIT", 0) + event_types.get("SHOT_MISSED", 0)
    if not (precision_episodes and shot_events):
        blocking.append("precision_event_coverage_missing")
    if not (cell_episodes and shot_events):
        blocking.append("cell_intercept_event_coverage_missing")

    return {
        "session": session,
        "resolved_ticks": len(resolved),
        "eligible_resolved_ticks": eligible,
        "clean_complete_episodes": len(clean),
        "friendly_deaths": friendly_deaths,
        "friendly_cargo_lost": cargo_lost,
        "event_types": dict(sorted(event_types.items())),
        "episode_attack_counts": {
            "sweep": sweep_episodes,
            "precision": precision_episodes,
            "cell_intercept": cell_episodes,
        },
        "blocking_reasons": blocking,
        "strategy_quality_ready": not blocking,
    }
