"""Measure a warm Orbit Worker directly and through the local orbitd HTTP API."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from verify_orbitd import daemon, fixture_env, model_fixture, wait_for, write_fixture_config

POLL_INTERVAL = 0.002
WARMUPS = 5
TASK = "plain"
REPLY = "orbitd fixture complete"


def working_set_mib(*pids: int) -> dict[int, float]:
    if sys.platform == "win32":
        ids = ",".join(str(pid) for pid in pids)
        output = subprocess.check_output(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"Get-Process -Id {ids} | Select-Object Id,WorkingSet64 | ConvertTo-Json -Compress",
            ],
            text=True,
            timeout=10,
        )
        rows = json.loads(output)
        if isinstance(rows, dict):
            rows = [rows]
        return {row["Id"]: row["WorkingSet64"] / 2**20 for row in rows}
    output = subprocess.check_output(["ps", "-o", "pid=,rss=", "-p", ",".join(map(str, pids))], text=True, timeout=10)
    return {int(pid): int(kib) / 1024 for pid, kib in map(str.split, output.splitlines())}


def direct(python: Path, workspace: Path, state: Path, config: Path, samples: int):
    command = [
        str(python),
        "-u",
        "-m",
        "orbit.worker",
        "--workspace",
        str(workspace),
        "--state",
        str(state),
        "--config",
        str(config),
    ]
    log_path = state / "worker.log"
    with log_path.open("wb") as log:
        started_at = time.perf_counter()
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, env=fixture_env())
        assert proc.stdin is not None and proc.stdout is not None
        frames = queue.Queue()

        def read_frames():
            for line in proc.stdout:
                frames.put((time.perf_counter(), line))
            frames.put((time.perf_counter(), b""))

        reader = threading.Thread(target=read_frames, daemon=True)
        reader.start()

        def receive():
            observed, line = frames.get(timeout=30)
            assert line, "Worker closed the pipe"
            return observed, json.loads(line)

        try:
            observed, ready = receive()
            assert ready["kind"] == "ready" and ready["version"] == 1
            startup_ms = 1000 * (observed - started_at)
            results = []
            for index in range(WARMUPS + samples):
                turn = f"sample-{index}"
                frame = json.dumps({"kind": "submit", "turn_id": turn, "conversation_id": turn, "text": TASK})
                began = time.perf_counter()
                proc.stdin.write(frame.encode() + b"\n")
                proc.stdin.flush()
                running, start = receive()
                _, reply = receive()
                terminal, end = receive()
                assert start["kind"] == "running" and start["turn_id"] == turn
                assert reply["kind"] == "reply" and reply["data"]["text"] == REPLY
                assert end["kind"] == "completed" and end["turn_id"] == turn and end["data"]["tool_calls"] == 0
                if index >= WARMUPS:
                    results.append(
                        {
                            "submit_to_running_ms": 1000 * (running - began),
                            "submit_to_completed_ms": 1000 * (terminal - began),
                        }
                    )
            rss = working_set_mib(ready["pid"])
            return startup_ms, results, {"worker": rss[ready["pid"]]}
        except BaseException:
            print(log_path.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
            raise
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
            reader.join(timeout=2)
            if not reader.is_alive():
                proc.stdout.close()


def managed(binary: Path, python: Path, workspace: Path, state: Path, samples: int):
    with daemon(binary, python, state, {"alpha": workspace}, 1) as client:
        workers = wait_for(client.workers, interval=POLL_INTERVAL)
        startup_ms = 1000 * (time.perf_counter() - client.started_at)
        results = []
        for index in range(WARMUPS + samples):
            began = time.perf_counter()
            code, turn = client.submit("alpha", f"sample-{index}", TASK)
            ack = time.perf_counter()
            assert code == 202, (code, turn)
            turn_id = turn["turn_id"]
            cursor = 0
            seen = {}

            def completed():
                nonlocal cursor
                events = client.events(turn_id, cursor)
                observed = time.perf_counter()
                for event in events:
                    cursor = event["seq"]
                    seen[event["kind"]] = observed, event
                return "completed" in seen

            wait_for(completed, timeout=30, interval=POLL_INTERVAL)
            assert seen["reply"][1]["data"]["text"] == REPLY
            assert seen["completed"][1]["data"]["tool_calls"] == 0
            if index >= WARMUPS:
                results.append(
                    {
                        "submit_ack_ms": 1000 * (ack - began),
                        "submit_to_running_ms": 1000 * (seen["running"][0] - began),
                        "submit_to_completed_ms": 1000 * (seen["completed"][0] - began),
                    }
                )
        assert client.workers() == workers, "Worker restarted during the benchmark"
        rss = working_set_mib(client.pid, workers["alpha"])
        return startup_ms, results, {"daemon": rss[client.pid], "worker": rss[workers["alpha"]]}


def summary(values):
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
    }


def benchmark(binary: Path, python: Path, root: Path, port: int, rounds: int, samples: int):
    runs = {"direct": [], "managed": []}
    for index in range(rounds):
        order = ("direct", "managed") if index % 2 == 0 else ("managed", "direct")
        for mode in order:
            trial = root / f"{mode}-{index}"
            workspace, state = trial / "workspace", trial / "state"
            workspace.mkdir(parents=True)
            state.mkdir()
            config = state / "config.json"
            write_fixture_config(config, port)
            if mode == "direct":
                startup, turns, rss = direct(python, workspace, state, config, samples)
            else:
                startup, turns, rss = managed(binary, python, workspace, state, samples)
            runs[mode].append({"startup_ms": startup, "turns": turns, "working_set_mib": rss})
    results = {}
    for mode, trials in runs.items():
        turns = [turn for trial in trials for turn in trial["turns"]]
        results[mode] = {
            "startup_ms": summary([trial["startup_ms"] for trial in trials]),
            "warm_turns": {key: summary([turn[key] for turn in turns]) for key in turns[0]},
            "idle_working_set_mib": {
                name: round(statistics.median(trial["working_set_mib"][name] for trial in trials), 3)
                for name in trials[0]["working_set_mib"]
            },
        }
    return {"summary": results, "samples": runs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orbitd", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--samples", type=int, default=20, help="warm turns per mode per round")
    args = parser.parse_args()
    if args.rounds < 1 or args.samples < 1:
        parser.error("--rounds and --samples must be positive")
    with args.orbitd.open("rb") as binary_file:
        binary_hash = hashlib.file_digest(binary_file, "sha256").hexdigest()
    with model_fixture() as port, tempfile.TemporaryDirectory(prefix="orbitd-bench-") as directory:
        report = benchmark(
            args.orbitd.resolve(), args.python.resolve(), Path(directory), port, args.rounds, args.samples
        )
    report["settings"] = {
        "platform": platform.platform(),
        "python": subprocess.check_output([str(args.python.resolve()), "--version"], text=True, timeout=10).strip(),
        "orbitd_sha256": binary_hash,
        "rounds": args.rounds,
        "warmups_per_round": WARMUPS,
        "samples_per_round": args.samples,
        "http_poll_interval_ms": 1000 * POLL_INTERVAL,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
