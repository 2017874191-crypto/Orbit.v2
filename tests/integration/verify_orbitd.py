"""Run the orbitd acceptance suite with real workers, files, and a local model fixture."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def wait_for(check, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.05)
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
        if task.startswith("edit-") and not has_result:
            with self.lock:
                self.edits[task] += 1
            result.update(
                content=None,
                tool_calls=[
                    {
                        "id": "call_edit",
                        "type": "function",
                        "function": {
                            "name": "edit_file",
                            "arguments": json.dumps(
                                {
                                    "path": "target.txt",
                                    "old_text": "anchor",
                                    "new_text": "written\nanchor",
                                }
                            ),
                        },
                    }
                ],
            )
            finish_reason = "tool_calls"
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


def acceptance(binary: Path, python: Path, home: Path, model_port: int):
    root = home / "state"
    root.mkdir()
    alpha, beta = home / "alpha", home / "beta"
    alpha.mkdir()
    beta.mkdir()
    marker = alpha / "target.txt"
    marker.write_text("anchor\n", encoding="utf-8")
    config = {
        "agents": {"defaults": {"model": "openai/gpt-4o-mini", "provider": "openai"}},
        "providers": {"openai": {"apiKey": "fixture-key", "apiBase": f"http://127.0.0.1:{model_port}/v1"}},
        "memory": {"backend": None},
        "callEfficiency": {"mode": "off"},
        "tracing": {"enabled": False},
    }
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with socket.socket() as free_port:
        free_port.bind(("127.0.0.1", 0))
        port = free_port.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    token = secrets.token_hex(24)
    env = {**os.environ, "ORBITD_TOKEN": token, "ORBIT_TRACING": "0", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}

    def request(path, body=None, method=None, auth=True):
        headers = {"Authorization": f"Bearer {token}"} if auth else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url + path, json.dumps(body).encode() if body is not None else None, headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def workers():
        try:
            _, report = request("/health")
        except urllib.error.URLError:
            return None
        return report["workers"] if all(report["workers"].values()) else None

    @contextlib.contextmanager
    def daemon(limit):
        log_path = home / f"daemon-{limit}.log"
        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                [
                    str(binary),
                    "--python",
                    str(python),
                    "--listen",
                    f"127.0.0.1:{port}",
                    "--state",
                    str(root),
                    "--workspace",
                    f"alpha={alpha}",
                    "--workspace",
                    f"beta={beta}",
                    "--concurrency",
                    str(limit),
                ],
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                yield wait_for(workers)
            except BaseException:
                print(log_path.read_text(encoding="utf-8", errors="replace")[-12000:], file=sys.stderr)
                raise
            finally:
                proc.terminate()
                proc.wait(timeout=12)

    def submit(workspace, key, text):
        return request("/turns", {"workspace": workspace, "conversation_id": key, "request_key": key, "text": text})

    def status(turn_id, expected):
        return wait_for(lambda: request(f"/turns/{turn_id}")[1]["status"] == expected)

    def events(turn_id):
        code, value = request(f"/turns/{turn_id}/events")
        assert code == 200
        return value

    with daemon(1) as pids:
        assert request("/health", auth=False)[0] == 401
        code, first = submit("alpha", "duplicate", "edit-once")
        assert code == 202
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
        assert [e["kind"] for e in request(f"/turns/{turn_id}/events?after={cut}")[1]] == ["tool", "reply", "completed"]

        code, cancelled = submit("alpha", "cancel", "hold-cancel")
        assert code == 202
        status(cancelled["turn_id"], "running")
        assert submit("beta", "blocked", "plain")[0] == 429
        assert request(f"/turns/{cancelled['turn_id']}/cancel", method="POST")[1]["cancel_requested"]
        status(cancelled["turn_id"], "cancelled")
        assert workers() == pids

        marker.write_text("anchor\n", encoding="utf-8")
        code, crashed = submit("alpha", "crash", "edit-crash")
        assert code == 202
        crash_id = crashed["turn_id"]
        wait_for(lambda: any(e.get("data", {}).get("phase") == "complete" for e in events(crash_id)))
        assert marker.read_text(encoding="utf-8") == "written\nanchor\n"
        os.kill(pids["alpha"], signal.SIGTERM)
        status(crash_id, "unknown")
        wait_for(lambda: (now := workers()) and now["alpha"] != pids["alpha"] and now["beta"] == pids["beta"])
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

    with daemon(2):
        assert submit("alpha", "duplicate", "edit-once")[1]["turn_id"] == turn_id
        status(turn_id, "completed")
        status(crash_id, "unknown")
        assert len(events(turn_id)) == 7
        a = submit("alpha", "parallel-a", "hold-parallel-a")[1]["turn_id"]
        b = submit("beta", "parallel-b", "hold-parallel-b")[1]["turn_id"]
        status(a, "running")
        status(b, "running")
        for name in ("hold-parallel-a", "hold-parallel-b"):
            Model.holds[name].set()
        status(a, "completed")
        status(b, "completed")

    print("PASS: 1 edit for replay; limit 1 returned 429, limit 2 ran two workspaces;")
    print("PASS: cancel, cursor replay, persisted restart, and recovery (1 edit vs. 2 with forced replay).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orbitd", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    Model.holds = {
        name: threading.Event() for name in ("hold-cancel", "edit-crash", "hold-parallel-a", "hold-parallel-b")
    }
    Model.edits = Counter()
    Model.lock = threading.Lock()
    with ThreadingHTTPServer(("127.0.0.1", 0), Model) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="orbitd-check-") as directory:
                acceptance(args.orbitd.resolve(), args.python.resolve(), Path(directory), server.server_port)
        finally:
            for hold in Model.holds.values():
                hold.set()
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
