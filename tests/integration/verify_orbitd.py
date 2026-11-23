"""Run the orbitd acceptance suite with real workers, files, and a local model fixture."""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

READ_TASK = "Use read_file on README.md, summarize it in one sentence, and include its verification code exactly."


def wait_for(check, timeout=40, *, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out at line {sys._getframe(1).f_lineno}")


class Model(BaseHTTPRequestHandler):
    holds: dict[str, threading.Event]
    edits: Counter
    lock: threading.Lock

    def log_message(self, *_args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = request["messages"]
        last_user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        task = last_user.rsplit("\n", 1)[-1].strip()
        has_result = messages[-1]["role"] == "tool"
        result = {"role": "assistant", "content": "orbitd fixture complete"}
        finish_reason = "stop"
        if (task.startswith("edit-") or task == READ_TASK) and not has_result:
            name, arguments = "read_file", {"path": "README.md"}
            if task.startswith("edit-"):
                with self.lock:
                    self.edits[task] += 1
                name = "edit_file"
                arguments = {"path": "target.txt", "old_text": "anchor", "new_text": "written\nanchor"}
            result.update(
                content=None,
                tool_calls=[
                    {
                        "id": "call_edit",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            )
            finish_reason = "tool_calls"
        elif task == READ_TASK:
            code = re.search(r"ORBIT-[0-9a-f]{16}", messages[-1]["content"])
            assert code is not None
            result["content"] = f"The README describes the Orbit tool smoke test and lists {code[0]}."
        elif hold := self.holds.get(task):
            hold.wait(35)
        body = json.dumps(
            {
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": result, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
            }
        ).encode()
        with contextlib.suppress(OSError):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)


def write_fixture_config(path: Path, model_port: int) -> None:
    config = {
        "agents": {"defaults": {"model": "openai/gpt-4o-mini", "provider": "openai"}},
        "providers": {"openai": {"apiKey": "fixture-key", "apiBase": f"http://127.0.0.1:{model_port}/v1"}},
        "memory": {"backend": None},
        "callEfficiency": {"mode": "off"},
        "tracing": {"enabled": False},
    }
    path.write_text(json.dumps(config), encoding="utf-8")


def fixture_env():
    return {**os.environ, "ORBIT_TRACING": "0", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}


@dataclass(frozen=True)
class Daemon:
    port: int
    token: str
    pid: int
    started_at: float

    def request(self, path, body=None, method=None, auth=True):
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            json.dumps(body).encode() if body is not None else None,
            headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def workers(self):
        try:
            code, report = self.request("/health")
        except urllib.error.URLError:
            return None
        return report["workers"] if code == 200 and all(report["workers"].values()) else None

    def submit(self, workspace, key, text, *, conversation=None):
        return self.request(
            "/turns", {"workspace": workspace, "conversation_id": conversation or key, "request_key": key, "text": text}
        )

    def status(self, turn_id, expected):
        return wait_for(lambda: self.request(f"/turns/{turn_id}")[1]["status"] == expected)

    def events(self, turn_id, after=0):
        code, value = self.request(f"/turns/{turn_id}/events?after={after}")
        assert code == 200
        return value


@contextlib.contextmanager
def daemon(
    binary: Path, python: Path, root: Path, workspaces: dict[str, Path], limit: int, *, config: Path | None = None
):
    with socket.socket() as free_port:
        free_port.bind(("127.0.0.1", 0))
        port = free_port.getsockname()[1]
    token = secrets.token_hex(24)
    command = [str(binary), "--python", str(python), "--listen", f"127.0.0.1:{port}", "--state", str(root)]
    if config is not None:
        command.extend(("--config", str(config)))
    for name, workspace in workspaces.items():
        command.extend(("--workspace", f"{name}={workspace}"))
    command.extend(("--concurrency", str(limit)))
    log_path = root / "daemon.log"
    with log_path.open("wb") as log:
        started_at = time.perf_counter()
        proc = subprocess.Popen(command, env={**fixture_env(), "ORBITD_TOKEN": token}, stdout=log, stderr=log)
        try:
            yield Daemon(port, token, proc.pid, started_at)
        except BaseException:
            print(log_path.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
            raise
        finally:
            proc.terminate()
            proc.wait(timeout=12)


def readme_turn(client: Daemon, workspace: Path):
    marker = f"ORBIT-{secrets.token_hex(8)}"
    (workspace / "README.md").write_text(
        f"# Orbit smoke test\n\nThis workspace checks tool access. Verification code: {marker}\n", encoding="utf-8"
    )
    code, turn = client.submit("alpha", "readme-smoke", READ_TASK)
    assert code == 202, (code, turn)
    turn_id = turn["turn_id"]

    def terminal_events():
        items = client.events(turn_id)
        if items and items[-1]["kind"] in {"completed", "failed", "cancelled", "unknown"}:
            return items
        return None

    events = wait_for(terminal_events, timeout=60, interval=0.2)
    assert events[-1]["kind"] == "completed", events[-1]
    assert any(
        e["kind"] == "tool"
        and e["data"]["phase"] == "complete"
        and e["data"]["name"] == "read_file"
        and not e["data"]["failed"]
        for e in events
    ), "No successful read_file event"
    assert any(e["kind"] == "reply" and marker in e["data"]["text"] for e in events), (
        "Reply omitted the verification code"
    )
    print(f"README smoke: {turn_id} -> read_file succeeded, verification code present in final reply.")


def acceptance(binary: Path, python: Path, home: Path, model_port: int):
    root = home / "state"
    root.mkdir()
    workspaces = {name: home / name for name in ("alpha", "beta")}
    for workspace in workspaces.values():
        workspace.mkdir()
    marker = workspaces["alpha"] / "target.txt"
    marker.write_text("anchor\n", encoding="utf-8")
    write_fixture_config(root / "config.json", model_port)

    with daemon(binary, python, root, workspaces, 1) as client:
        pids = wait_for(client.workers)
        request, submit, status, events = client.request, client.submit, client.status, client.events
        assert request("/health", auth=False)[0] == 401
        # Simulate a client that sends the whole request and loses the response.
        with contextlib.closing(http.client.HTTPConnection("127.0.0.1", client.port, timeout=3)) as connection:
            connection.request(
                "POST",
                "/turns",
                body=json.dumps(
                    {
                        "workspace": "alpha",
                        "conversation_id": "duplicate",
                        "request_key": "duplicate",
                        "text": "edit-once",
                    }
                ),
                headers={"Authorization": f"Bearer {client.token}", "Content-Type": "application/json"},
            )
        wait_for(lambda: Model.edits["edit-once"] == 1)
        code, first = submit("alpha", "duplicate", "edit-once")
        assert code == 200
        turn_id = first["turn_id"]
        status(turn_id, "completed")
        assert submit("alpha", "duplicate", "edit-once")[1]["turn_id"] == turn_id
        assert submit("alpha", "duplicate", "different")[0] == 409
        assert marker.read_text(encoding="utf-8").splitlines().count("written") == Model.edits["edit-once"] == 1
        assert [e["kind"] for e in events(turn_id)] == [
            "accepted",
            "dispatching",
            "running",
            "tool",
            "tool",
            "reply",
            "completed",
        ]
        cut = events(turn_id)[3]["seq"]
        assert [e["kind"] for e in events(turn_id, cut)] == ["tool", "reply", "completed"]
        persisted_tail = events(turn_id, cut)

        marker.write_text("anchor\n", encoding="utf-8")
        no_dedup_ids = set()
        for key in ("no-dedup-1", "no-dedup-2"):
            code, control = submit("alpha", key, "edit-no-dedup", conversation="no-dedup")
            assert code == 202
            no_dedup_ids.add(control["turn_id"])
            status(control["turn_id"], "completed")
        assert len(no_dedup_ids) == 2
        assert marker.read_text(encoding="utf-8").splitlines().count("written") == Model.edits["edit-no-dedup"] == 2
        print("Dedup: same key -> 1 turn, 1 execution, 1 write; fresh keys -> 2 turns, 2 executions, 2 writes.")

        _, a = submit("alpha", "serial-a", "hold-parallel-a")
        status(a["turn_id"], "running")
        assert submit("beta", "serial-b", "hold-parallel-b")[0] == 429
        Model.holds["hold-parallel-a"].set()
        status(a["turn_id"], "completed")
        code, b = submit("beta", "serial-b", "hold-parallel-b")
        assert code == 202
        status(b["turn_id"], "running")
        Model.holds["hold-parallel-b"].set()
        status(b["turn_id"], "completed")
        assert events(a["turn_id"])[-1]["seq"] < next(e["seq"] for e in events(b["turn_id"]) if e["kind"] == "running")
        for name in ("hold-parallel-a", "hold-parallel-b"):
            Model.holds[name].clear()

        code, cancelled = submit("alpha", "cancel", "hold-cancel")
        assert code == 202
        status(cancelled["turn_id"], "running")
        assert submit("beta", "blocked", "plain")[0] == 429
        assert request(f"/turns/{cancelled['turn_id']}/cancel", method="POST")[1]["cancel_requested"]
        status(cancelled["turn_id"], "cancelled")
        assert client.workers() == pids

        marker.write_text("anchor\n", encoding="utf-8")
        code, crashed = submit("alpha", "crash", "edit-crash")
        assert code == 202
        crash_id = crashed["turn_id"]
        wait_for(lambda: any(e.get("data", {}).get("phase") == "complete" for e in events(crash_id)))
        assert marker.read_text(encoding="utf-8") == "written\nanchor\n"
        os.kill(pids["alpha"], signal.SIGTERM)
        status(crash_id, "unknown")
        wait_for(lambda: (now := client.workers()) and now["alpha"] != pids["alpha"] and now["beta"] == pids["beta"])
        assert submit("alpha", "crash", "edit-crash")[1]["turn_id"] == crash_id
        assert marker.read_text(encoding="utf-8") == "written\nanchor\n"
        assert Model.edits["edit-crash"] == 1
        status(submit("alpha", "after-crash", "plain")[1]["turn_id"], "completed")

        # A new key deliberately simulates re-dispatch after the same crash. The
        # already modified file is kept so the second side effect is observable.
        Model.holds["edit-crash"].set()
        _, replay = submit("alpha", "forced-replay", "edit-crash")
        status(replay["turn_id"], "completed")
        assert marker.read_text(encoding="utf-8") == "written\nwritten\nanchor\n"
        assert Model.edits["edit-crash"] == 2
        print(
            f"Crash: {crash_id} -> unknown, 1 write; replacement completed a new turn; deliberate replay -> 2 writes."
        )

    with daemon(binary, python, root, workspaces, 2) as client:
        wait_for(client.workers)
        submit, status, events = client.submit, client.status, client.events
        assert submit("alpha", "duplicate", "edit-once")[1]["turn_id"] == turn_id
        status(turn_id, "completed")
        status(crash_id, "unknown")
        assert len(events(turn_id)) == 7
        assert events(turn_id, cut) == persisted_tail
        a = submit("alpha", "parallel-a", "hold-parallel-a")[1]["turn_id"]
        b = submit("beta", "parallel-b", "hold-parallel-b")[1]["turn_id"]
        status(a, "running")
        status(b, "running")
        for name in ("hold-parallel-a", "hold-parallel-b"):
            Model.holds[name].set()
        status(a, "completed")
        status(b, "completed")
        histories = [events(turn_id) for turn_id in (a, b)]
        starts = [next(e["seq"] for e in history if e["kind"] == "running") for history in histories]
        assert max(starts) < min(history[-1]["seq"] for history in histories)
        print("Quota: limit 1 -> peak 1 running, second submission HTTP 429; limit 2 -> peak 2 running.")
        print("Restart: same turn_id, 7 retained events, identical 3-event cursor suffix.")
        readme_turn(client, workspaces["alpha"])

    fresh_state = home / "fresh-state"
    fresh_state.mkdir()
    write_fixture_config(fresh_state / "config.json", model_port)
    with daemon(binary, python, fresh_state, workspaces, 1) as client:
        wait_for(client.workers)
        assert client.request(f"/turns/{turn_id}")[0] == 404
    print("Fresh-state control: original turn_id returned HTTP 404.")

    print("PASS: authentication, dedup, quota, cancellation, crash recovery and persistence.")


@contextlib.contextmanager
def model_fixture():
    Model.holds = {
        name: threading.Event() for name in ("hold-cancel", "edit-crash", "hold-parallel-a", "hold-parallel-b")
    }
    Model.edits = Counter()
    Model.lock = threading.Lock()
    with ThreadingHTTPServer(("127.0.0.1", 0), Model) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            for hold in Model.holds.values():
                hold.set()
            server.shutdown()
            thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orbitd", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--provider-config", type=Path, help="run only the README smoke with this Orbit config")
    args = parser.parse_args()
    binary, python = args.orbitd.resolve(strict=True), args.python.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="orbitd-check-") as directory:
        home = Path(directory)
        if args.provider_config is None:
            with model_fixture() as port:
                acceptance(binary, python, home, port)
            return
        root, alpha = home / "state", home / "alpha"
        root.mkdir()
        alpha.mkdir()
        with daemon(
            binary, python, root, {"alpha": alpha}, 1, config=args.provider_config.resolve(strict=True)
        ) as client:
            wait_for(client.workers)
            readme_turn(client, alpha)


if __name__ == "__main__":
    main()
