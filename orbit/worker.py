"""Run one managed Orbit workspace over a private, newline-delimited pipe."""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from orbit.spine import ChatType, Origin, OriginPools, Scheduler, Source, TurnHandle, TurnRequest
from orbit.spine.events import Text, ToolEvent, TurnEnded, TurnEvent, TurnFailed, TurnStarted

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 1 << 20


@dataclass
class ActiveTurn:
    turn_id: str
    handle: TurnHandle
    failure: TurnFailed | None = None
    ended: TurnEnded | None = None
    cancel_requested: bool = False


async def serve(runner, output: TextIO) -> None:
    active: ActiveTurn | None = None
    completion: asyncio.Task[None] | None = None

    def send(**event) -> None:
        output.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
        output.flush()

    async def observe(event: TurnEvent) -> None:
        if active is None:
            raise RuntimeError("runtime emitted without an active turn")
        match event:
            case TurnStarted():
                send(kind="running", turn_id=active.turn_id)
            case ToolEvent():
                send(
                    kind="tool",
                    turn_id=active.turn_id,
                    data={
                        "phase": event.phase,
                        "call_id": event.tool_call_id,
                        "name": event.name,
                        "failed": event.failed,
                        "duration_ms": event.duration_ms,
                    },
                )
            case Text():
                send(kind="reply", turn_id=active.turn_id, data={"text": event.content})
            case TurnFailed():
                active.failure = event
            case TurnEnded():
                active.ended = event

    async def finish(turn: ActiveTurn) -> None:
        nonlocal active
        result = await turn.handle.result()
        active = None
        if result is not None and turn.ended is not None:
            send(
                kind="completed",
                turn_id=turn.turn_id,
                data={
                    "usage": asdict(result.usage),
                    "tool_calls": result.tool_calls,
                    "tool_failures": result.tool_failures,
                    "explicit_reply": result.explicit_reply,
                },
            )
        elif turn.failure and not turn.failure.cancelled:
            send(kind="failed", turn_id=turn.turn_id, data={"reason": "runtime_failed"})
        elif turn.failure or turn.cancel_requested:
            send(kind="cancelled", turn_id=turn.turn_id)
        else:
            send(kind="unknown", turn_id=turn.turn_id, data={"reason": "no_runtime_terminal_event"})

    scheduler = Scheduler(runner, OriginPools(1, 1), observe)
    send(kind="ready", version=PROTOCOL_VERSION, pid=os.getpid())
    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline, MAX_FRAME_BYTES):
            if not line.endswith(b"\n"):
                raise ValueError("incomplete or oversized input frame")
            if completion is not None and completion.done():
                completion.result()
            command = json.loads(line)
            turn_id = command["turn_id"]
            if command["kind"] == "cancel":
                if active is not None and active.turn_id == turn_id:
                    active.cancel_requested = True
                    active.handle.cancel()
                continue
            if command["kind"] != "submit" or active is not None:
                raise ValueError("invalid worker command or overlapping turns")
            conversation = command["conversation_id"]
            request = TurnRequest(
                origin=Origin.USER,
                source=Source(channel="worker", chat_id=conversation, sender_id="local", chat_type=ChatType.DM),
                text=command["text"],
                conversation=f"worker:{conversation}",
            )
            active = ActiveTurn(turn_id, scheduler.submit(request))
            completion = asyncio.create_task(finish(active))
    finally:
        await scheduler.shutdown(grace=1)
        if completion is not None:
            await completion


async def run(workspace: Path, state: Path, config_path: Path, output: TextIO) -> None:
    from orbit.agent.spine_runner import AgentTurnRunner
    from orbit.cli._helpers import make_lazy_provider
    from orbit.cli._runtime_assembly import assemble_runtime
    from orbit.config.loader import read_raw_or_raise
    from orbit.config.orbit import load_orbit_config
    from orbit.config.paths import RuntimePaths
    from orbit.utils.helpers import sync_workspace_templates
    from orbit.utils.portable_lock import file_lock

    with file_lock(state / "worker.lock", blocking=False):
        if not config_path.is_file():
            raise FileNotFoundError(config_path)
        raw = read_raw_or_raise(config_path)
        tracing = raw.get("tracing")
        if isinstance(tracing, dict) and isinstance(tracing.get("enabled"), bool):
            os.environ.setdefault("ORBIT_TRACING", "1" if tracing["enabled"] else "0")
        orbit_config = load_orbit_config(config_path)
        config = orbit_config.base
        config.agents.defaults.workspace = str(workspace)
        config.tools.restrict_to_workspace = True
        config.tools.disabled_tools = [*config.tools.disabled_tools, "ask_user", "spawn", "message"]
        sync_workspace_templates(state, silent=True)
        runtime = assemble_runtime(
            config,
            orbit_config,
            provider=make_lazy_provider(config),
            cron_service=None,
            interactive=False,
            paths=RuntimePaths(workspace=workspace, state=state),
        )
        try:
            await runtime.start_memory_backend()
            await serve(AgentTurnRunner(runtime.agent_loop, stream=False), output)
        finally:
            await runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    workspace, state, config = (path.resolve() for path in (args.workspace, args.state, args.config))
    # Reserve the original pipe for protocol frames, including when libraries write to fd 1.
    with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n") as output:
        sys.stdout.flush()
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        os.environ["ORBIT_HOME"] = str(state)
        os.environ["ORBIT_TRACING_DIR"] = str(state / "traces")
        asyncio.run(run(workspace, state, config, output))


if __name__ == "__main__":
    main()
