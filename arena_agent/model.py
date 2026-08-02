from __future__ import annotations

from dataclasses import dataclass
from typing import Any

Position = tuple[int, int]


@dataclass(frozen=True)
class Unit:
    id: str
    position: Position
    unit_type: str
    cargo: int = 0


@dataclass(frozen=True)
class VisibleEnemy:
    """A currently visible enemy only; never a historical targeting record."""

    id: str
    position: Position
    kind: str
    unit_type: str | None = None


@dataclass(frozen=True)
class Snapshot:
    tick: int
    status: str
    resources: int
    resource_capacity: int
    core_id: str | None
    core_position: Position | None
    units: tuple[Unit, ...] = ()
    visible_enemies: tuple[VisibleEnemy, ...] = ()
    resource_cells: frozenset[Position] = frozenset()
    obstacle_cells: frozenset[Position] = frozenset()
    events: tuple[dict[str, Any], ...] = ()

    @property
    def workers(self) -> tuple[Unit, ...]:
        return tuple(x for x in self.units if x.unit_type == "WORKER")

    @property
    def vanguards(self) -> tuple[Unit, ...]:
        return tuple(x for x in self.units if x.unit_type == "VANGUARD")

    @property
    def rangers(self) -> tuple[Unit, ...]:
        return tuple(x for x in self.units if x.unit_type == "RANGER")


def position(value: Any) -> Position:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise ValueError(f"invalid position: {value!r}")
    return int(value[0]), int(value[1])


def snapshot_from_state(tick: int, data: dict[str, Any]) -> Snapshot:
    core_id = None
    core_position = None
    units: list[Unit] = []
    enemies: list[VisibleEnemy] = []
    resources: set[Position] = set()
    obstacles: set[Position] = set()
    for obj in data.get("objects", []):
        kind = obj.get("kind")
        controlled = bool(obj.get("controlled"))
        if kind == "CORE":
            if controlled:
                core_id = str(obj["id"])
                core_position = position(obj["position"])
            elif "id" in obj and "position" in obj:
                enemies.append(VisibleEnemy(str(obj["id"]), position(obj["position"]), "CORE"))
        elif kind == "UNIT":
            if controlled:
                units.append(Unit(str(obj["id"]), position(obj["position"]),
                                  str(obj["unit_type"]), int(obj.get("cargo", 0))))
            elif "id" in obj and "position" in obj:
                enemies.append(VisibleEnemy(str(obj["id"]), position(obj["position"]), "UNIT",
                                            str(obj.get("unit_type", "UNKNOWN"))))
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
        units=tuple(units), visible_enemies=tuple(enemies),
        resource_cells=frozenset(resources), obstacle_cells=frozenset(obstacles),
        events=tuple(data.get("events", [])),
    )
