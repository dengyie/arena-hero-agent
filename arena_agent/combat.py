from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .model import Position, VisibleEnemy

RANGER_MIN_RANGE = 1
RANGER_MAX_RANGE = 3


@dataclass(frozen=True)
class CombatDecision:
    action: dict[str, object]
    target_id: str | None = None
    target_position: Position | None = None


def ranger_line_distance(start: Position, target: Position) -> int | None:
    """Return legal Ranger distance for cardinal or exact 45-degree shots."""
    dx = abs(target[0] - start[0])
    dy = abs(target[1] - start[1])
    if dx == 0 and dy == 0:
        return None
    if dx == 0 or dy == 0 or dx == dy:
        distance = max(dx, dy)
        if RANGER_MIN_RANGE <= distance <= RANGER_MAX_RANGE:
            return distance
    return None


def intermediate_cells(start: Position, target: Position) -> tuple[Position, ...]:
    distance = ranger_line_distance(start, target)
    if distance is None:
        return ()
    dx = (target[0] - start[0]) // distance
    dy = (target[1] - start[1]) // distance
    return tuple((start[0] + dx * step, start[1] + dy * step) for step in range(1, distance))


def ranger_target(ranger_position: Position, enemies: Iterable[VisibleEnemy],
                  obstacles: frozenset[Position]) -> VisibleEnemy | None:
    """Select a visible, geometrically legal target without retaining fog facts."""
    candidates: list[tuple[int, int, str, VisibleEnemy]] = []
    for enemy in enemies:
        distance = ranger_line_distance(ranger_position, enemy.position)
        if distance is None or any(cell in obstacles for cell in intermediate_cells(ranger_position, enemy.position)):
            continue
        # Units are less durable and protect economic routes less than a nearby Core.
        candidates.append((0 if enemy.kind == "UNIT" else 1, distance, enemy.id, enemy))
    return min(candidates)[-1] if candidates else None


def ranger_actions(state) -> dict[str, dict[str, object]]:
    actions: dict[str, dict[str, object]] = {}
    for ranger in state.rangers:
        target = ranger_target(ranger.position, state.visible_enemies, state.obstacle_cells)
        actions[ranger.id] = (
            {"type": "SHOOT", "target_id": target.id, "expected_cell": list(target.position)}
            if target is not None else {"type": "WAIT"}
        )
    return actions
