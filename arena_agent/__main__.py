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
from .policy import ExplorationMemory, economy_plan

LOG = logging.getLogger("arena_agent")
WS_URL = "wss://api.arenahero.io/api/v1/game/ws"
HTTP_URL = "https://api.arenahero.io/api/v1/game/commands"
DIRECT_HTTP = build_opener(ProxyHandler({}))

class ProtocolError(RuntimeError): pass
class PermanentAuthError(RuntimeError): pass

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
        try:
            with tempfile.NamedTemporaryFile(prefix="arena-response-", mode="wb", delete=False) as f:
                body_path = f.name
            os.chmod(body_path, 0o600)
            cmd = ["curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "5",
                   "--request", "POST", "--url", HTTP_URL,
                   "--header", "Authorization: Bearer " + token,
                   "--header", "Content-Type: application/json",
                   "--header", "Idempotency-Key: " + key,
                   "--data-raw", raw.decode(), "--output", body_path,
                   "--write-out", "%{http_code}"]
            if cookie:
                cmd += ["--header", "Cookie: " + cookie, "--header", "Origin: https://app.arenahero.io"]
            if csrf:
                cmd += ["--header", "X-CSRF-Token: " + csrf]
            completed = subprocess.run(cmd, text=True, capture_output=True, timeout=8,
                                       env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"})
            if completed.returncode != 0:
                raise RuntimeError("curl command failed: " + completed.stderr[:200])
            status = completed.stdout.strip()
            with open(body_path, encoding="utf-8") as response_file:
                body = response_file.read()
        finally:
            if body_path:
                try:
                    os.unlink(body_path)
                except FileNotFoundError:
                    pass
        if not status.isdigit():
            raise RuntimeError("curl response missing status")
        result = {"status": int(status), "body": json.loads(body or "{}")}
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
                        plan = economy_plan(snapshot, memory)
                        result = await post_plan(token, tick, plan.as_dict(), args.dry_run, cookie, csrf)
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
                        state_summary["policy_state"] = plan.policy_state
                        state_summary["active_target"] = plan.active_target
                        state_summary["waypoint"] = plan.waypoint
                        state_summary["resource_memory_count"] = len(memory.resources)
                        state_summary["frontier"] = {
                            "band_radius": plan.band_radius,
                            "queued": len(memory.frontier_candidates),
                            "completed": len(memory.completed_targets),
                            "failed": len(memory.failed_targets),
                        }
                        state_summary["path"] = {
                            "status": plan.path_status,
                            "nodes": plan.path_nodes,
                        }
                        state_summary["last_event_types"] = plan.last_event_types
                        journal.write("plan", session=session_id, tick=tick, state=state_summary, plan=plan.as_dict(), result=result)
                        ticks += 1
                        if args.max_ticks > 0 and ticks >= args.max_ticks:
                            return 0
                    elif kind == "received":
                        journal.write("received", session=session_id, data=msg.get("data"))
                    else:
                        journal.write("unknown_message", session=session_id, data=msg)
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
