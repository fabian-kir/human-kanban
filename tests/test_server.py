import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server as app


class FakeGitHub:
    def __init__(self, raw=None):
        self.raw = raw
        self.puts = []

    def default_branch(self):
        return "main"

    def get(self, target):
        if self.raw is None:
            return None
        return self.raw, hashlib.sha1(self.raw).hexdigest()

    def put(self, target, raw, previous_sha):
        actual = hashlib.sha1(self.raw).hexdigest() if self.raw is not None else None
        if actual != previous_sha:
            raise app.GitHubError("Remote changed", 409)
        self.puts.append((target, previous_sha))
        self.raw = raw


class TestBoardLogic(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "board.js"
        self.path.write_bytes((ROOT / "board.js").read_bytes())
        self.marker = self.root / ".kanban-sync.json"
        self.sentinel = self.root / "do-not-touch.txt"
        self.sentinel.write_text("important files stay untouched\n")
        self.remote = FakeGitHub(self.path.read_bytes())
        self.store = app.BoardStore(
            board_path=self.path, marker_path=self.marker, repo="org/project", remote_path="kanban/board.js",
            branch="main", token="fake", client=self.remote,
        )

    def test_initial_sync_and_local_change_only_updates_board(self):
        self.store.sync_once()
        self.assertEqual(self.store.info()["mode"], "synced")
        old = self.store.read()
        updated = copy.deepcopy(old["board"])
        updated["title"] = "Human priorities"
        self.store.save(updated, old["revision"])
        self.assertEqual(self.store.info()["mode"], "pending")
        self.store.sync_once()
        self.assertEqual(self.store.info()["mode"], "synced")
        self.assertEqual(app.decode_board(self.remote.raw)["title"], "Human priorities")
        self.assertEqual(len(self.remote.puts), 1)
        self.assertEqual(self.remote.puts[0][0].path, "kanban/board.js")
        self.assertEqual(self.sentinel.read_text(), "important files stay untouched\n")
        self.assertEqual(self.marker.read_text().count("org/project@main:kanban/board.js"), 1)

    def test_remote_update_is_pulled_when_local_unchanged(self):
        self.store.sync_once()
        remote_board = app.decode_board(self.remote.raw)
        remote_board["title"] = "Edited elsewhere"
        self.remote.raw = app.encode_board(remote_board)
        self.store.sync_once()
        self.assertEqual(self.store.read()["board"]["title"], "Edited elsewhere")
        self.assertEqual(self.store.info()["mode"], "synced")
        self.assertEqual(len(self.remote.puts), 0)

    def test_two_changes_conflict_without_overwrite_and_can_keep_remote(self):
        self.store.sync_once()
        old = self.store.read()
        local = copy.deepcopy(old["board"])
        local["title"] = "My local work"
        self.store.save(local, old["revision"])
        remote = copy.deepcopy(old["board"])
        remote["title"] = "Remote edit"
        self.remote.raw = app.encode_board(remote)
        self.store.sync_once()
        self.assertEqual(self.store.info()["mode"], "conflict")
        self.assertEqual(self.store.read()["board"]["title"], "My local work")
        with self.assertRaises(app.KanbanError):
            self.store.save(local, self.store.read()["revision"])
        self.store.resolve("remote")
        self.assertEqual(self.store.read()["board"]["title"], "Remote edit")
        backups = list((self.root / ".kanban-backups").glob("*.js"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(app.decode_board(backups[0].read_bytes())["title"], "My local work")

    def test_conflict_can_explicitly_keep_local(self):
        self.store.sync_once()
        old = self.store.read()
        local = copy.deepcopy(old["board"])
        local["title"] = "Keep local"
        self.store.save(local, old["revision"])
        remote = copy.deepcopy(old["board"])
        remote["title"] = "Remote change"
        self.remote.raw = app.encode_board(remote)
        self.store.sync_once()
        self.store.resolve("local")
        self.assertEqual(self.store.info()["mode"], "synced")
        self.assertEqual(app.decode_board(self.remote.raw)["title"], "Keep local")

    def test_agent_edits_board_file_and_server_pushes_only_it(self):
        self.store.sync_once()
        board = app.decode_board(self.path.read_bytes())
        board["columns"][0]["cards"].append({
            "id": "agent-note-1", "title": "Review a proposal", "description": "Requested by a human"
        })
        # A coding agent edits the same plain-text board.js without using the UI.
        self.path.write_bytes(app.encode_board(board))
        self.store.sync_once()
        self.assertEqual(self.store.info()["mode"], "synced")
        self.assertEqual(len(app.decode_board(self.remote.raw)["columns"][0]["cards"]), 1)
        self.assertEqual(self.sentinel.read_text(), "important files stay untouched\n")

    def test_sync_baseline_survives_server_restart(self):
        self.store.sync_once()
        remote_board = app.decode_board(self.remote.raw)
        remote_board["title"] = "Advanced on another computer"
        self.remote.raw = app.encode_board(remote_board)
        restarted = app.BoardStore(
            board_path=self.path, marker_path=self.marker, repo="org/project",
            remote_path="kanban/board.js", branch="main", token="fake", client=self.remote,
        )
        restarted.sync_once()
        self.assertEqual(restarted.info()["mode"], "synced")
        self.assertEqual(restarted.read()["board"]["title"], "Advanced on another computer")

    def test_stale_editor_is_refused(self):
        self.store.sync_once()
        old = self.store.read()
        updated = copy.deepcopy(old["board"])
        updated["title"] = "First writer"
        self.store.save(updated, old["revision"])
        another = copy.deepcopy(old["board"])
        another["title"] = "Stale writer"
        with self.assertRaisesRegex(app.KanbanError, "changed"):
            self.store.save(another, old["revision"])
        self.assertEqual(self.store.read()["board"]["title"], "First writer")

    def test_starts_without_remote_or_token_as_local_editing(self):
        local = app.BoardStore(board_path=self.path, marker_path=self.marker)
        local.sync_once()
        self.assertTrue(local.info()["can_edit"])
        self.assertEqual(local.info()["mode"], "local")
        local.save(local.read()["board"], local.read()["revision"])
        self.assertEqual(self.sentinel.read_text(), "important files stay untouched\n")

    def test_invalid_ids_and_payload_are_rejected(self):
        board = app.decode_board(self.path.read_bytes())
        board["columns"][1]["id"] = board["columns"][0]["id"]
        with self.assertRaises(app.KanbanError):
            app.encode_board(board)
        with self.assertRaises(app.KanbanError):
            app.decode_board(b"alert('not board data');")

    def test_column_color_is_optional_and_checked(self):
        board = app.decode_board(self.path.read_bytes())
        board["columns"][0]["color"] = "#3A7F6E"
        self.assertEqual(app.decode_board(app.encode_board(board))["columns"][0]["color"], "#3a7f6e")
        self.assertNotIn("color", app.decode_board(app.encode_board(board))["columns"][1])
        board["columns"][0]["color"] = "red; background:url(x)"
        with self.assertRaises(app.KanbanError):
            app.encode_board(board)

    def test_removed_remote_file_is_preserved_as_conflict(self):
        self.store.sync_once()
        self.remote.raw = None
        self.store.sync_once()
        self.assertEqual(self.store.info()["mode"], "conflict")
        self.assertTrue(self.path.exists())

    def test_first_sync_can_publish_missing_board(self):
        self.remote.raw = None
        self.store.sync_once()
        self.assertEqual(self.store.info()["mode"], "synced")
        self.assertEqual(self.remote.raw, self.path.read_bytes())
        self.assertEqual(len(self.remote.puts), 1)


class TestHTTP(unittest.TestCase):
    def test_real_local_server_and_origin_protection(self):
        import socket
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ["server.py", "index.html", "board.js"]:
                (root / name).write_bytes((ROOT / name).read_bytes())
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = os.environ.copy()
            for name in ["GITHUB_TOKEN", "GH_TOKEN", "KANBAN_GITHUB_TOKEN", "KANBAN_REPO"]:
                env.pop(name, None)
            process = subprocess.Popen(
                [sys.executable, str(root / "server.py"), "--port", str(port), "--no-browser"],
                cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                url = f"http://127.0.0.1:{port}"
                for _ in range(80):
                    try:
                        with urllib.request.urlopen(url + "/api/board", timeout=0.3) as response:
                            loaded = json.load(response)
                        break
                    except (OSError, ValueError):
                        time.sleep(0.05)
                else:
                    self.fail("Server failed to boot")
                self.assertEqual(loaded["sync"]["mode"], "local")
                self.assertIn("window.KANBAN_BOARD", urllib.request.urlopen(url + "/board.js").read().decode())
                self.assertIn("<!doctype html>", urllib.request.urlopen(url + "/index.html").read().decode())
                payload = {"board": loaded["board"], "revision": loaded["revision"]}
                payload["board"]["title"] = "Changed by browser"
                body = json.dumps(payload).encode()
                wrong = urllib.request.Request(
                    url + "/api/board", data=body,
                    headers={"Content-Type": "application/json", "Origin": "https://malicious.example"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(wrong)
                self.assertEqual(caught.exception.code, 403)
                self.assertEqual(app.decode_board((root / "board.js").read_bytes())["title"], "My board")
                good = urllib.request.Request(
                    url + "/api/board", data=body,
                    headers={"Content-Type": "application/json", "Origin": url}, method="POST",
                )
                with urllib.request.urlopen(good) as result:
                    self.assertEqual(json.load(result)["board"]["title"], "Changed by browser")
                self.assertEqual(app.decode_board((root / "board.js").read_bytes())["title"], "Changed by browser")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    unittest.main()
