from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Position = tuple[int, int]

@dataclass(frozen=True)
class Unit:
    id: str
    position: Position
    unit_type: str
    cargo: int = 0

@dataclass(frozen=True)
class Snapshot:
    tick: int
    status: str
    resources: int
    resource_capacity: int
    core_id: str | None
    core_position: Position | None
    units: tuple[Unit, ...] = ()
    resource_cells: frozenset[Position] = frozenset()
    obstacle_cells: frozenset[Position] = frozenset()
    events: tuple[dict[str, Any], ...] = ()

    @property
    def workers(self) -> tuple[Unit, ...]:
        return tuple(x for x in self.units if x.unit_type == "WORKER")


def position(value: Any) -> Position:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError(f"invalid position: {value!r}")
    return int(value[0]), int(value[1])


def snapshot_from_state(tick: int, data: dict[str, Any]) -> Snapshot:
    core_id = None
    core_position = None
    units: list[Unit] = []
    resources: set[Position] = set()
    obstacles: set[Position] = set()
    for obj in data.get("objects", []):
        kind = obj.get("kind")
        if kind == "CORE" and obj.get("controlled"):
            core_id = str(obj["id"])
            core_position = position(obj["position"])
        elif kind == "UNIT" and obj.get("controlled"):
            units.append(Unit(str(obj["id"]), position(obj["position"]), str(obj["unit_type"]), int(obj.get("cargo", 0))))
        elif kind == "RESOURCE":
            resources.update(position(x) for x in obj.get("positions", []))
        elif kind == "OBSTACLE":
            obstacles.update(position(x) for x in obj.get("positions", []))
    population = int(data.get("population", len(units)))
    return Snapshot(
        tick=int(tick), status=str(data.get("status", "UNKNOWN")),
        resources=int(data.get("resources", 0)),
        resource_capacity=max(10, population * 5),
        core_id=core_id, core_position=core_position,
        units=tuple(units), resource_cells=frozenset(resources),
        obstacle_cells=frozenset(obstacles),
        events=tuple(data.get("events", [])),
    )
