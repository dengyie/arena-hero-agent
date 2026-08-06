"""Official Arena Hero 0.2.9-compatible economy rules."""

UNIT_BASE_COSTS = {"WORKER": 5, "VANGUARD": 10, "RANGER": 12}
CORE_RESOURCE_MINIMUM_CAPACITY = 10
CORE_RESOURCE_CAPACITY_PER_UNIT = 5


def core_resource_capacity(population: int) -> int:
    if population < 0:
        raise ValueError("population must not be negative")
    return max(CORE_RESOURCE_MINIMUM_CAPACITY, population * CORE_RESOURCE_CAPACITY_PER_UNIT)


def unit_production_cost(unit_type: str, population: int) -> int:
    if population < 0:
        raise ValueError("population must not be negative")
    try:
        base = UNIT_BASE_COSTS[unit_type]
    except KeyError as exc:
        raise ValueError(f"unknown unit type: {unit_type}") from exc
    exponent = 0 if population < 20 else (population - 20) // 5 + 1
    numerator = base * 13**exponent
    denominator = 10**exponent
    return (2 * numerator + denominator) // (2 * denominator)
