from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .model import Position, Snapshot, Unit, VisibleEnemy

RANGER_MIN_RANGE = 1
RANGER_MAX_RANGE = 3


@dataclass(frozen=True)
class CombatDecision:
    action: dict[str, object]
    target_id: str | None = None
    target_position: Position | None = None
    target_mode: str | None = None
    reason: str = "WAIT"


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


def precision_shot_action(target_id: str, expected_cell: Position) -> dict[str, object]:
    return {"type": "SHOOT", "target_id": target_id, "expected_cell": list(expected_cell)}


def cell_shot_action(expected_cell: Position) -> dict[str, object]:
    return {"type": "SHOOT", "expected_cell": list(expected_cell)}


def guard_slots(core: Position, obstacles: frozenset[Position], *, minimum: int = 2,
                maximum: int = 4) -> tuple[Position, ...]:
    slots = [
        (core[0] + dx, core[1] + dy)
        for radius in range(minimum, maximum + 1)
        for dx in range(-radius, radius + 1)
        for dy in (-(radius - abs(dx)), radius - abs(dx))
        if (core[0] + dx, core[1] + dy) not in obstacles
    ]
    return tuple(dict.fromkeys(sorted(slots, key=lambda cell: (
        abs(cell[0] - core[0]) + abs(cell[1] - core[1]), cell[0], cell[1]
    ))))


def ranger_firing_cells(target: Position, obstacles: frozenset[Position]) -> tuple[Position, ...]:
    cells: list[Position] = []
    for distance in range(RANGER_MIN_RANGE, RANGER_MAX_RANGE + 1):
        for direction in ((0, -1), (1, 0), (0, 1), (-1, 0),
                          (1, 1), (1, -1), (-1, 1), (-1, -1)):
            cell = target[0] + direction[0] * distance, target[1] + direction[1] * distance
            if cell in obstacles or not _shot_is_clear(cell, target, obstacles):
                continue
            cells.append(cell)
    return tuple(dict.fromkeys(cells))


def _shot_is_clear(start: Position, target: Position, obstacles: frozenset[Position]) -> bool:
    return ranger_line_distance(start, target) is not None and not any(
        cell in obstacles for cell in intermediate_cells(start, target)
    )


def select_vanguard_decision(vanguard: Unit, state: Snapshot,
                              *, protected_cells: set[Position]) -> CombatDecision:
    direction_order = (("UP", (0, -1)), ("RIGHT", (1, 0)),
                       ("DOWN", (0, 1)), ("LEFT", (-1, 0)))
    candidates: list[tuple[int, int, int, str, Position]] = []
    for rank, (direction, delta) in enumerate(direction_order):
        cell = vanguard.position[0] + delta[0], vanguard.position[1] + delta[1]
        hostiles = [enemy for enemy in state.visible_enemies if enemy.position == cell]
        if not hostiles:
            continue
        protected = min((abs(cell[0] - p[0]) + abs(cell[1] - p[1]) for p in protected_cells), default=99)
        candidates.append((-len(hostiles), protected, rank, direction, cell))
    if not candidates:
        return CombatDecision({"type": "WAIT"}, reason="NO_ADJACENT_HOSTILE")
    _, _, _, direction, cell = min(candidates)
    return CombatDecision({"type": "SWEEP", "direction": direction},
                          target_position=cell, target_mode="SWEEP_CELL", reason="ADJACENT_HOSTILE")


def select_ranger_decision(ranger: Unit, state: Snapshot,
                            *, protected_cells: set[Position], allow_cell_intercept: bool = False) -> CombatDecision:
    legal = [enemy for enemy in state.visible_enemies
             if _shot_is_clear(ranger.position, enemy.position, state.obstacle_cells)]
    if legal:
        target = min(legal, key=lambda enemy: (
            min((abs(enemy.position[0] - p[0]) + abs(enemy.position[1] - p[1])
                 for p in protected_cells), default=99),
            enemy.hp if enemy.hp is not None else 1_000,
            enemy.kind != "UNIT", enemy.id,
        ))
        return CombatDecision(precision_shot_action(target.id, target.position), target.id,
                              target.position, "PRECISION_CURRENT", "CURRENT_HOSTILE")
    if allow_cell_intercept:
        candidates: list[tuple[int, Position]] = []
        for enemy in state.visible_enemies:
            for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                cell = enemy.position[0] + dx, enemy.position[1] + dy
                if cell in state.obstacle_cells or not _shot_is_clear(ranger.position, cell, state.obstacle_cells):
                    continue
                score = min((abs(cell[0] - p[0]) + abs(cell[1] - p[1])
                             for p in protected_cells), default=99)
                if score <= 2:
                    candidates.append((score, cell))
        if candidates:
            cell = min(candidates)[1]
            return CombatDecision(cell_shot_action(cell), target_position=cell,
                                  target_mode="CELL_INTERCEPT", reason="PROTECTED_CELL_INTERCEPT")
    return CombatDecision({"type": "WAIT"}, reason="NO_LEGAL_SHOT")


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


def ranger_actions(state, *, allow_fire: bool, allowed_ranger_ids: set[str] | None = None) -> dict[str, dict[str, object]]:
    actions: dict[str, dict[str, object]] = {}
    allowed_ranger_ids = allowed_ranger_ids or set()
    for ranger in state.rangers:
        target = (
            ranger_target(ranger.position, state.visible_enemies, state.obstacle_cells)
            if allow_fire and ranger.id in allowed_ranger_ids else None
        )
        actions[ranger.id] = (
            precision_shot_action(target.id, target.position)
            if target is not None else {"type": "WAIT"}
        )
    return actions
