"""Kill orbitd at two durable Turn boundaries and verify restart with real Workers."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from verify_orbitd import Model, daemon, model_fixture, wait_for, write_fixture_config

from orbit.utils.portable_lock import file_lock


def wait_for_worker_exit(pid: int) -> None:
    if sys.platform == "win32":
        # On Windows os.kill(pid, 0) calls TerminateProcess. Wait on the old process instead.
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"$worker = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                "if ($null -eq $worker -or $worker.WaitForExit(12000)) { exit 0 }; exit 1",
            ],
            check=True,
            timeout=18,
        )
        return

    def exited():
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=3)
        if result.returncode == 1 and not (result.stdout.strip() or result.stderr.strip()):
            return True
        result.check_returncode()
        return result.stdout.lstrip().startswith("Z")

    wait_for(exited, timeout=12)


def verify(binary: Path, python: Path, home: Path, port: int) -> None:
    state, workspace = home / "state", home / "alpha"
    state.mkdir()
    workspace.mkdir()
    workspaces = {"alpha": workspace}
    marker = workspace / "target.txt"
    marker.write_text("anchor\n", encoding="utf-8")
    write_fixture_config(state / "config.json", port)

    # Holding the real Worker lock keeps alpha unready while orbitd commits the HTTP submission.
    lock = state / "workers" / "workspace-alpha" / "worker.lock"
    with file_lock(lock), daemon(binary, python, state, workspaces, 1) as client:
        health = wait_for(client.health)
        assert health["workers"] == {"alpha": 0}
        code, accepted = client.submit("alpha", "accepted-crash", "edit-accepted")
        assert code == 202 and accepted["status"] == "accepted", (code, accepted)
        accepted_id = accepted["turn_id"]
        before_dispatch = client.events(accepted_id)
        assert [event["kind"] for event in before_dispatch] == ["accepted"], before_dispatch
        assert marker.read_text(encoding="utf-8") == "anchor\n"
        assert Model.edits["edit-accepted"] == 0
        client.crash()

    with daemon(binary, python, state, workspaces, 1) as client:
        old_pid = wait_for(client.workers)["alpha"]
        client.status(accepted_id, "completed", timeout=90)
        recovered = client.events(accepted_id)
        assert recovered[:1] == before_dispatch
        assert [event["kind"] for event in recovered] == [
            "accepted",
            "dispatching",
            "running",
            "tool",
            "tool",
            "reply",
            "completed",
        ], recovered
        assert marker.read_text(encoding="utf-8") == "written\nanchor\n"
        assert Model.edits["edit-accepted"] == 1
        print(f"Manager crash before dispatch: {accepted_id} resumed -> completed, 1 write.")

        marker.write_text("anchor\n", encoding="utf-8")
        code, running = client.submit("alpha", "running-crash", "edit-crash")
        assert code == 202, (code, running)
        running_id = running["turn_id"]

        def written_before_terminal():
            events = client.events(running_id)
            if any(
                e["kind"] == "tool"
                and e["data"]["phase"] == "complete"
                and e["data"]["name"] == "edit_file"
                and not e["data"]["failed"]
                for e in events
            ):
                return events
            return None

        # The fixture holds the model's response after the edit so the Turn cannot complete first.
        before_crash = wait_for(written_before_terminal)
        assert [event["kind"] for event in before_crash] == ["accepted", "dispatching", "running", "tool", "tool"]
        assert client.request(f"/turns/{running_id}")[1]["status"] == "running"
        assert marker.read_text(encoding="utf-8") == "written\nanchor\n"
        client.crash()

    wait_for_worker_exit(old_pid)
    Model.holds["edit-crash"].set()
    with daemon(binary, python, state, workspaces, 1) as client:
        new_pid = wait_for(client.workers)["alpha"]
        assert new_pid != old_pid
        code, replay = client.submit("alpha", "running-crash", "edit-crash")
        assert code == 200 and replay["turn_id"] == running_id and replay["status"] == "unknown", (code, replay)
        history = client.events(running_id)
        assert history[:-1] == before_crash
        assert history[-1]["kind"] == "unknown" and history[-1]["data"]["reason"] == "manager_restarted"

        inspection = subprocess.check_output(
            [str(binary), "inspect", "--listen", f"127.0.0.1:{client.port}", running_id],
            env={**os.environ, "ORBITD_TOKEN": client.token},
            text=True,
            timeout=12,
        )
        assert f"Turn: {running_id}" in inspection, inspection
        assert "Status: unknown" in inspection and "Reason: manager_restarted" in inspection, inspection
        assert "edit_file complete (succeeded)" in inspection, inspection
        assert client.events(running_id) == history

        code, fresh = client.submit("alpha", "after-manager-crash", "plain")
        assert code == 202 and fresh["turn_id"] != running_id, (code, fresh)
        client.status(fresh["turn_id"], "completed", timeout=90)
        assert marker.read_text(encoding="utf-8") == "written\nanchor\n"
        assert Model.edits["edit-crash"] == 1
        print(f"Manager crash after tool write: {running_id} -> unknown (manager_restarted), 1 write.")
        print(f"Preserved {len(before_crash)} prior events and original Turn ID; Worker {old_pid} exited.")
        print(f"Replacement Worker {new_pid} completed a new Turn.")
        print("Read-only inspection of the interrupted Turn:")
        print(inspection, end="")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orbitd", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    binary, python = args.orbitd.resolve(strict=True), args.python.resolve(strict=True)
    with model_fixture() as port, tempfile.TemporaryDirectory(prefix="orbitd-manager-crash-") as directory:
        verify(binary, python, Path(directory), port)
    print("PASS: orbitd forced-exit recovery before dispatch and after a tool side effect.")


if __name__ == "__main__":
    main()
