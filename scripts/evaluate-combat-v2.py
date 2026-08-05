#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arena_agent.evaluation import evaluate_combat_session


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate an Arena Combat V2 journal session")
    parser.add_argument("journal")
    parser.add_argument("session")
    args = parser.parse_args()
    result = evaluate_combat_session(args.journal, args.session)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["strategy_quality_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
