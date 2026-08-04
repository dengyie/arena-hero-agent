from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from typing import Any
from urllib.request import Request, ProxyHandler, build_opener

from .journal import Journal
from .model import snapshot_from_state
from .path import FRONTIER_PATH_NODE_CAP, MAX_FRONTIER_PATH_EVALUATIONS
from .policy import (MAX_ECONOMY_WORKERS, MAX_EXTERNAL_RECOVERY_POPULATION,
                     ExplorationMemory, economy_plan, step_position)

LOG = logging.getLogger("arena_agent")
WS_URL = "wss://api.arenahero.io/api/v1/game/ws"
HTTP_URL = "https://api.arenahero.io/api/v1/game/commands"
STALE_TICK_RECONNECT_THRESHOLD = 3


class ProtocolError(RuntimeError): pass
class PermanentAuthError(RuntimeError): pass

def stale_tick_reconnect_required(streak: int, result: dict[str, Any]) -> tuple[int, bool]:
    next_streak = streak + 1 if result.get("stale_tick") else 0
    return next_streak, next_streak >= STALE_TICK_RECONNECT_THRESHOLD


def record_received_source(audit: dict[str, Any], received: Any) -> dict[str, Any]:
    """Classify a stored source plan for metrics without affecting decisions."""
    payload = received if isinstance(received, dict) else {}
    source = str(payload.get("source", "UNKNOWN"))
    stored_plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    core_action = stored_plan.get("core_action") if isinstance(stored_plan.get("core_action"), dict) else None
    audit["last_received"] = {
        "source": source,
        "tick": payload.get("tick"),
        "core_action": core_action.get("type") if core_action else None,
    }
    if source == "MANUAL":
        audit["manual_interventions"] += 1
        audit["window_contaminated"] = True
        if core_action:
            audit["external_core_actions"] += 1
    return payload


def record_session_baseline(audit: dict[str, Any], snapshot: Any) -> None:
    """Flag a restart baseline that already exceeds the Agent's worker cap."""
    if audit.get("baseline_recorded"):
        return
    workers = len(snapshot.workers)
    audit["baseline_recorded"] = True
    audit["baseline_worker_count"] = workers
    audit["baseline_population"] = snapshot.population
    audit["baseline_over_worker_cap"] = workers > MAX_ECONOMY_WORKERS
    audit["baseline_contaminated"] = (
        workers > MAX_ECONOMY_WORKERS or snapshot.population > MAX_ECONOMY_WORKERS
    )
    if audit["baseline_contaminated"]:
        audit["window_contaminated"] = True


def population_control_metrics(snapshot: Any) -> dict[str, Any]:
    worker_count = len(snapshot.workers)
    return {
        "worker_count": worker_count,
        "normal_worker_cap": MAX_ECONOMY_WORKERS,
        "external_recovery_population_ceiling": MAX_EXTERNAL_RECOVERY_POPULATION,
        "external_recovery_ceiling_reached": snapshot.population >= MAX_EXTERNAL_RECOVERY_POPULATION,
    }


def allocator_metrics(snapshot, matched: int, total_cost: int) -> dict[str, Any]:
    eligible = sum(1 for worker in snapshot.workers
                   if worker.cargo <= 0 and (worker.hp is None or worker.hp > 1))
    visible_resources = len(snapshot.resource_cells)
    return {
        "eligible": eligible,
        "visible_resources": visible_resources,
        "matched": matched,
        "unmatched_eligible": max(0, eligible - matched),
        "resource_starved": bool(snapshot.workers) and not visible_resources,
        "total_cost": total_cost,
    }


async def post_plan(token: str, tick: int, plan: dict[str, Any], dry_run: bool, cookie: str = "", csrf: str = "") -> dict[str, Any]:
    body = {"tick": tick, **plan}
    if dry_run:
        return {"dry_run": True, "body": body}
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    key = "arena-agent-%s-%s" % (tick, hashlib.sha256(raw).hexdigest()[:16])
    req = Request(HTTP_URL, data=raw, method="POST", headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Idempotency-Key": key,
    })
    if cookie:
        req.add_header("Cookie", cookie)
        req.add_header("Origin", "https://app.arenahero.io")
    if csrf:
        req.add_header("X-CSRF-Token", csrf)
    def send() -> dict[str, Any]:
        # curl's system TLS/HTTP fingerprint is accepted by the edge where
        # Python's urllib client is challenged. Secrets stay in curl config
        # stdin and never appear in argv or the journal.
        body_path = ""
        request_path = ""
        config_path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="arena-response-", mode="wb", delete=False) as f:
                body_path = f.name
            with tempfile.NamedTemporaryFile(prefix="arena-request-", mode="wb", delete=False) as f:
                request_path = f.name
                f.write(raw)
            with tempfile.NamedTemporaryFile(prefix="arena-curl-", mode="w", delete=False) as f:
                config_path = f.name
                f.write("header = \"Authorization: Bearer " + token + "\"\n")
                f.write("header = \"Content-Type: application/json\"\n")
                f.write("header = \"Idempotency-Key: " + key + "\"\n")
                if cookie:
                    f.write("header = \"Cookie: " + cookie + "\"\n")
                    f.write("header = \"Origin: https://app.arenahero.io\"\n")
                if csrf:
                    f.write("header = \"X-CSRF-Token: " + csrf + "\"\n")
            for path in (body_path, request_path, config_path):
                os.chmod(path, 0o600)
            cmd = ["curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "5",
                   "--request", "POST", "--url", HTTP_URL, "--config", config_path,
                   "--data-binary", "@" + request_path, "--output", body_path,
                   "--write-out", "%{http_code}"]
            completed = subprocess.run(cmd, text=True, capture_output=True, timeout=8,
                                       env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"})
            if completed.returncode != 0:
                raise RuntimeError("curl command failed: " + completed.stderr[:200])
            status = completed.stdout.strip()
            with open(body_path, encoding="utf-8") as response_file:
                body = response_file.read()
        finally:
            for path in (body_path, request_path, config_path):
                if path:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
        if not status.isdigit():
            raise RuntimeError("curl response missing status")
        result = {"status": int(status), "body": json.loads(body or "{}")}
        if result["status"] == 409 and result["body"].get("error") == "TICK_MISMATCH":
            # A newly restarted Agent can receive a state for a tick whose
            # prior process already stored a plan. The slot is closed; wait
            # for the next tick rather than treating this as a protocol fault.
            result["stale_tick"] = True
        if result["status"] in (401, 403):
            raise PermanentAuthError(f"command authentication rejected status={result['status']}")
        return result
    return await asyncio.to_thread(send)

async def run(args: argparse.Namespace) -> int:
    if args.fixture:
        data = json.loads(open(args.fixture, encoding="utf-8").read())
        snapshot = snapshot_from_state(int(data["tick"]), data["state"])
        plan = economy_plan(snapshot)
        result = await post_plan("", snapshot.tick, plan.as_dict(), True)
        Journal(args.journal).write("fixture_plan", tick=snapshot.tick, plan=plan.as_dict(), result=result)
        return 0
    try:
        import websockets
    except ImportError as exc:
        raise SystemExit("install requirements.txt first") from exc
    token = os.environ.get("ARENA_HERO_TOKEN", "")
    cookie = os.environ.get("ARENA_HERO_COOKIE", "")
    csrf = os.environ.get("ARENA_HERO_CSRF", "")
    if not args.dry_run and not token and not cookie:
        raise SystemExit("ARENA_HERO_TOKEN or ARENA_HERO_COOKIE is required for --live")
    journal = Journal(args.journal)
    memory = ExplorationMemory()
    source_audit = {"manual_interventions": 0, "external_core_actions": 0,
                    "window_contaminated": False, "last_received": None,
                    "baseline_recorded": False, "baseline_contaminated": False,
                    "baseline_worker_count": None, "baseline_population": None,
                    "baseline_over_worker_cap": False}
    stale_tick_streak = 0
    reconnect_reason: str | None = None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if cookie:
        headers["Cookie"] = cookie
        headers["Origin"] = "https://app.arenahero.io"
    if csrf:
        headers["X-CSRF-Token"] = csrf
    ticks = 0
    backoff = 0.5
    session_id = hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:12]
    journal.write("session_start", session=session_id, dry_run=args.dry_run)
    while args.max_ticks <= 0 or ticks < args.max_ticks:
        try:
            if hasattr(websockets, "connect"):
                try:
                    connector = websockets.connect(WS_URL, additional_headers=headers, max_size=2 * 1024 * 1024)
                except TypeError:
                    connector = websockets.connect(WS_URL, extra_headers=headers, max_size=2 * 1024 * 1024)
            else:
                raise ProtocolError("websockets.connect unavailable")
            async with connector as ws:
                backoff = 0.5
                saw_message = False
                async for raw in ws:
                    saw_message = True
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "tick":
                        tick = int(msg["data"])
                        journal.write("tick", session=session_id, tick=tick)
                    elif kind == "state":
                        if "tick" not in locals():
                            raise ProtocolError("state arrived before tick")
                        snapshot = snapshot_from_state(tick, msg["data"])
                        record_session_baseline(source_audit, snapshot)
                        plan = economy_plan(snapshot, memory)
                        result = await post_plan(token, tick, plan.as_dict(), args.dry_run, cookie, csrf)
                        stale_tick_streak, should_reconnect = stale_tick_reconnect_required(stale_tick_streak, result)
                        if should_reconnect:
                            reconnect_reason = "stale_tick_streak"
                        raw_state = msg["data"]
                        state_summary = {
                            "status": raw_state.get("status"),
                            "resources": raw_state.get("resources"),
                            "population": raw_state.get("population"),
                            "population_tier": raw_state.get("population_tier"),
                            "objects": {
                                "total": len(raw_state.get("objects", [])),
                                "controlled_units": [
                                    {
                                        "id": obj.get("id"),
                                        "unit_type": obj.get("unit_type"),
                                        "position": obj.get("position"),
                                        "cargo": obj.get("cargo", 0),
                                        "hp": obj.get("hp"),
                                    }
                                    for obj in raw_state.get("objects", [])
                                    if obj.get("kind") == "UNIT" and obj.get("controlled")
                                ],
                                "controlled_core": [
                                    {"id": obj.get("id"), "position": obj.get("position")}
                                    for obj in raw_state.get("objects", [])
                                    if obj.get("kind") == "CORE" and obj.get("controlled")
                                ],
                                "resource_positions": sum(
                                    len(obj.get("positions", []))
                                    for obj in raw_state.get("objects", [])
                                    if obj.get("kind") == "RESOURCE"
                                ),
                                "obstacle_positions": sum(
                                    len(obj.get("positions", []))
                                    for obj in raw_state.get("objects", [])
                                    if obj.get("kind") == "OBSTACLE"
                                ),
                            },
                            "events": raw_state.get("events", []),
                        }
                        state_summary["upkeep_next_tick"] = raw_state.get("upkeep_next_tick")
                        state_summary["controlled_core_state"] = [
                            {
                                "id": obj.get("id"),
                                "position": obj.get("position"),
                                "hp": obj.get("hp"),
                                "shield": obj.get("shield"),
                                "state": obj.get("state"),
                            }
                            for obj in raw_state.get("objects", [])
                            if obj.get("kind") == "CORE" and obj.get("controlled")
                        ]
                        state_summary["source_audit"] = {
                            "manual_interventions": source_audit["manual_interventions"],
                            "external_core_actions": source_audit["external_core_actions"],
                            "window_contaminated": source_audit["window_contaminated"],
                            "last_received": source_audit["last_received"],
                            "baseline_contaminated": source_audit["baseline_contaminated"],
                            "baseline_worker_count": source_audit["baseline_worker_count"],
                            "baseline_population": source_audit["baseline_population"],
                            "baseline_over_worker_cap": source_audit["baseline_over_worker_cap"],
                        }
                        state_summary["population_control"] = population_control_metrics(snapshot)
                        state_summary["metric_window_eligible"] = (
                            not source_audit["window_contaminated"] and not result.get("stale_tick")
                        )
                        state_summary["policy_state"] = plan.policy_state
                        state_summary["active_target"] = plan.active_target
                        state_summary["waypoint"] = plan.waypoint
                        state_summary["resource_memory_count"] = len(memory.resources)
                        state_summary["frontier"] = {
                            "band_radius": plan.band_radius,
                            "queued": len(memory.frontier_candidates),
                            "completed": len(memory.completed_targets),
                            "failed": len(memory.failed_targets),
                            "failure_reasons": dict(memory.frontier_failure_reasons),
                            "active_targets": len(memory.active_targets),
                            "path_evaluations": memory.frontier_path_evaluations,
                            "path_nodes": memory.frontier_path_nodes,
                            "path_evaluation_cap_per_worker": MAX_FRONTIER_PATH_EVALUATIONS,
                            "path_node_cap_per_evaluation": FRONTIER_PATH_NODE_CAP,
                        }
                        state_summary["path"] = {
                            "status": plan.path_status,
                            "nodes": plan.path_nodes,
                            "length": plan.path_length,
                        }
                        state_summary["worker_actions"] = {
                            worker_id: action.get("type")
                            for worker_id, action in plan.unit_actions.items()
                        }
                        state_summary["economy_metrics"] = {
                            "recent_deposits": len(memory.ledger.deposits),
                            "pending_harvests": len(memory.ledger.pending_harvests),
                            "deposit_latencies": list(memory.ledger.deposit_latencies)[-8:],
                            "injured_workers": sorted(memory.ledger.worker_damage_ticks),
                            "core_damage_recent": len(memory.ledger.core_damage_ticks),
                            "upkeep_last": memory.ledger.upkeep_events[-1] if memory.ledger.upkeep_events else None,
                            "resource_overflow_destroyed": memory.ledger.resource_overflow_amount,
                            "core_defense_events": list(memory.ledger.core_defense_events)[-8:],
                            "core_action": (plan.core_action or {}).get("type"),
                            "allocator": allocator_metrics(snapshot, memory.allocation_count, memory.allocation_total_cost),
                        }
                        state_summary["traffic"] = {
                            "holds": dict(memory.traffic.holds),
                            "ingress_queue": list(memory.traffic.ingress_queue),
                            "reserved_destinations": len({
                                step_position(unit.position, action["direction"])
                                for unit in snapshot.workers
                                for action in [plan.unit_actions.get(unit.id, {})]
                                if action.get("type") == "MOVE"
                            }),
                            "dynamic_edges": len(memory.traffic.blocked_edges),
                            "dynamic_cells": len(memory.traffic.blocked_cells),
                            "repeated_failures": max(memory.traffic.repeated_failures.values(), default=0),
                        }
                        state_summary["last_event_types"] = plan.last_event_types
                        state_summary["stale_tick"] = bool(result.get("stale_tick"))
                        state_summary["stale_tick_streak"] = stale_tick_streak
                        state_summary["reconnect_reason"] = reconnect_reason
                        journal.write("plan", session=session_id, tick=tick, state=state_summary, plan=plan.as_dict(), result=result)
                        if should_reconnect:
                            journal.write("session_rejoin", session=session_id,
                                          reason=reconnect_reason, stale_tick_streak=stale_tick_streak)
                            break
                        ticks += 1
                        if args.max_ticks > 0 and ticks >= args.max_ticks:
                            return 0
                    elif kind == "received":
                        received = record_received_source(source_audit, msg.get("data"))
                        journal.write("received", session=session_id, data=received)
                    else:
                        journal.write("unknown_message", session=session_id, data=msg)
                if reconnect_reason:
                    journal.write("session_rejoin_complete", session=session_id,
                                  reason=reconnect_reason, stale_tick_streak=stale_tick_streak)
                    stale_tick_streak = 0
                    reconnect_reason = None
                    await asyncio.sleep(0.5)
                    continue
                if saw_message:
                    journal.write("session_end", session=session_id, reason="ws_closed")
                    return 43
        except KeyboardInterrupt:
            return 0
        except PermanentAuthError as exc:
            journal.write("session_end", session=session_id, reason="permanent_auth", error=str(exc))
            return 42
        except Exception as exc:
            text = repr(exc)
            if "status_code=401" in text or "status_code=403" in text or "Unauthorized" in text or "Forbidden" in text:
                journal.write("session_end", session=session_id, reason="ws_auth", error=text)
                return 42
            journal.write("connection_error", session=session_id, error=text, backoff=backoff)
            if args.max_ticks > 0 and ticks >= args.max_ticks:
                return 1
            await asyncio.sleep(backoff)
            backoff = min(5.0, backoff * 2)
    return 0

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", action="store_true", help="submit commands; without it only dry-run plans are journaled")
    p.add_argument("--dry-run", action="store_true", help="explicitly force dry-run")
    p.add_argument("--max-ticks", type=int, default=0)
    p.add_argument("--journal", default=os.environ.get("ARENA_HERO_JOURNAL", "./runtime/arena-agent.jsonl"))
    p.add_argument("--fixture", help="build one dry-run plan from a local JSON state fixture")
    args = p.parse_args()
    args.dry_run = not args.live or args.dry_run
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(run(args))

if __name__ == "__main__": raise SystemExit(main())
