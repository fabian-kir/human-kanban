#!/usr/bin/env python3
"""Human Kanban: local editing server and single-file GitHub synchronizer.

Python standard library only. Git is queried read-only to discover the repository
and distinguish an untouched checkout from an independently edited board.
No git pull, add, commit, push, reset, checkout, or other repository mutation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
BOARD_FILE = BASE / "board.js"
MARKER_FILE = BASE / ".kanban-sync.json"
BACKUP_DIR = BASE / ".kanban-backups"
MAX_BODY = 512 * 1024
SYNC_INTERVAL = 40
USER_AGENT = "Human-Kanban/1.0"
PREFIX = "window.KANBAN_BOARD = "
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,70}$")


class KanbanError(Exception):
    """A safe, actionable error for the person running the board."""


class GitHubError(KanbanError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Target:
    repo: str
    path: str
    branch: str

    @property
    def key(self) -> str:
        return f"{self.repo}@{self.branch}:{self.path}"


def validate_board(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "title", "columns"}:
        raise KanbanError("Board requires version, title, and columns (no other fields).")
    if type(value["version"]) is not int or value["version"] != 1:
        raise KanbanError("Unsupported board format. Expected version 1.")
    title = value["title"]
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 90:
        raise KanbanError("The board title must be 1–90 characters.")
    columns = value["columns"]
    if not isinstance(columns, list) or not 1 <= len(columns) <= 20:
        raise KanbanError("The board needs 1–20 columns.")
    seen: set[str] = set()
    total = 0
    clean_columns = []
    for column in columns:
        if not isinstance(column, dict) or set(column) != {"id", "title", "cards"}:
            raise KanbanError("Every column requires id, title, and cards.")
        cid, name, cards = column["id"], column["title"], column["cards"]
        if not isinstance(cid, str) or not ID_PATTERN.fullmatch(cid) or cid in seen:
            raise KanbanError("Column identifiers must be unique and valid.")
        seen.add(cid)
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 70:
            raise KanbanError("Column titles must be 1–70 characters.")
        if not isinstance(cards, list):
            raise KanbanError("Column cards must be a list.")
        clean_cards = []
        for card in cards:
            if not isinstance(card, dict) or set(card) != {"id", "title", "description"}:
                raise KanbanError("Every card requires id, title, and description.")
            card_id, card_title, description = card["id"], card["title"], card["description"]
            if not isinstance(card_id, str) or not ID_PATTERN.fullmatch(card_id) or card_id in seen:
                raise KanbanError("Card identifiers must be unique and valid.")
            seen.add(card_id)
            if not isinstance(card_title, str) or not 1 <= len(card_title.strip()) <= 180:
                raise KanbanError("Card titles must be 1–180 characters.")
            if not isinstance(description, str) or len(description) > 10000:
                raise KanbanError("Card descriptions must be 10,000 characters or fewer.")
            clean_cards.append({"id": card_id, "title": card_title, "description": description})
            total += 1
            if total > 3000:
                raise KanbanError("This board supports up to 3,000 cards.")
        clean_columns.append({"id": cid, "title": name, "cards": clean_cards})
    return {"version": 1, "title": title, "columns": clean_columns}


def encode_board(board: dict[str, Any]) -> bytes:
    normalized = validate_board(board)
    encoded = (PREFIX + json.dumps(normalized, ensure_ascii=True, indent=2) + ";\n").encode("utf-8")
    if len(encoded) > MAX_BODY:
        raise KanbanError("Board exceeds the 512 KB safety limit.")
    return encoded


def decode_board(raw: bytes) -> dict[str, Any]:
    try:
        source = raw.decode("utf-8").strip()
        if not source.startswith(PREFIX) or not source.endswith(";"):
            raise KanbanError("board.js must assign JSON to window.KANBAN_BOARD.")
        return validate_board(json.loads(source[len(PREFIX):-1].strip()))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KanbanError(f"board.js is not valid UTF-8 JSON: {exc}") from exc


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def atomic_write(path: Path, data: bytes, private: bool = False) -> None:
    fd, filename = tempfile.mkstemp(prefix=".kanban-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if private:
            os.chmod(filename, 0o600)
        os.replace(filename, path)
    finally:
        try:
            os.unlink(filename)
        except FileNotFoundError:
            pass


def git_output(*args: str) -> str | None:
    if not shutil.which("git"):
        return None
    try:
        process = subprocess.run(
            ["git", "-C", str(BASE), *args], capture_output=True,
            text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return process.stdout.strip() if process.returncode == 0 else None


def locate_target() -> tuple[str | None, str, str | None]:
    configured = os.environ.get("KANBAN_REPO", "").strip()
    remote = git_output("remote", "get-url", "origin")
    detected = None
    if remote:
        match = re.match(r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)([^\s]+)$", remote)
        if match:
            detected = match.group(1).removesuffix(".git").strip("/")
    repo = configured or detected
    if repo and not REPO_PATTERN.fullmatch(repo):
        raise KanbanError("KANBAN_REPO must be owner/repository on github.com.")

    override = os.environ.get("KANBAN_REMOTE_PATH", "").strip()
    if override:
        remote_path = override.strip("/")
    else:
        root = git_output("rev-parse", "--show-toplevel")
        if root:
            try:
                remote_path = BOARD_FILE.relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                remote_path = BOARD_FILE.name
        else:
            remote_path = BOARD_FILE.name
    if (not remote_path or remote_path.startswith(".") or ".." in remote_path.split("/")
            or not all(re.fullmatch(r"[A-Za-z0-9_. -]+", part) for part in remote_path.split("/"))):
        raise KanbanError("KANBAN_REMOTE_PATH must be a safe repository-relative path.")
    branch = os.environ.get("KANBAN_BRANCH", "").strip() or None
    if branch and (not re.fullmatch(r"[A-Za-z0-9_./-]+", branch) or ".." in branch):
        raise KanbanError("KANBAN_BRANCH contains invalid characters.")
    return repo, remote_path, branch


def pristine_in_checkout(remote_path: str, local_bytes: bytes) -> bool:
    """An untouched tracked board may safely receive a newer copy from GitHub."""
    if not shutil.which("git"):
        return False
    try:
        result = subprocess.run(
            ["git", "-C", str(BASE), "show", f"HEAD:{remote_path}"],
            capture_output=True, timeout=6, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == local_bytes


def obtain_token() -> tuple[str | None, str]:
    for name in ("KANBAN_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(name, "").strip()
        if token:
            return token, f"${name}"
    if shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "auth", "token", "--hostname", "github.com"],
                capture_output=True, text=True, timeout=8, check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip(), "GitHub CLI"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return None, "not configured"


class GitHubClient:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo
        self.root_url = "https://api.github.com/repos/" + "/".join(urllib.parse.quote(p, safe="") for p in repo.split("/"))

    def request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.root_url + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise GitHubError("GitHub repository, branch, or board file not found.", 404) from exc
            if exc.code in (409, 422):
                raise GitHubError("GitHub rejected the update because the file changed. Retry after checking for conflicts.", exc.code) from exc
            if exc.code in (401, 403):
                raise GitHubError("GitHub refused access. Run 'gh auth login' or provide a token with repository Contents read/write permission.", exc.code) from exc
            raise GitHubError(f"GitHub returned HTTP {exc.code}. Check connectivity and permissions.", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubError("Cannot reach GitHub. Local changes remain on disk and synchronization will retry.") from exc
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubError("GitHub returned an unreadable response.") from exc

    def default_branch(self) -> str:
        response = self.request("GET", "")
        return str(response["default_branch"])

    def get(self, target: Target) -> tuple[bytes, str] | None:
        path = "/contents/" + "/".join(urllib.parse.quote(p, safe="") for p in target.path.split("/"))
        try:
            response = self.request("GET", path + "?ref=" + urllib.parse.quote(target.branch, safe=""))
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(response, dict) or response.get("type") != "file" or response.get("encoding") != "base64":
            raise GitHubError("GitHub did not return a supported board file (base64 content required).")
        try:
            raw = base64.b64decode(response["content"], validate=False)
            if len(raw) > MAX_BODY:
                raise GitHubError("Remote board.js exceeds the size limit.")
            decode_board(raw)
            return raw, str(response["sha"])
        except (ValueError, KeyError, TypeError, KanbanError) as exc:
            raise GitHubError(f"Remote board.js is invalid; local data was preserved. {exc}") from exc

    def put(self, target: Target, raw: bytes, previous_sha: str | None) -> None:
        path = "/contents/" + "/".join(urllib.parse.quote(p, safe="") for p in target.path.split("/"))
        data: dict[str, Any] = {
            "message": "kanban: update board state",
            "content": base64.b64encode(raw).decode("ascii"),
            "branch": target.branch,
        }
        if previous_sha is not None:
            data["sha"] = previous_sha
        self.request("PUT", path, data)


class BoardStore:
    def __init__(self, board_path: Path = BOARD_FILE, marker_path: Path = MARKER_FILE,
                 repo: str | None = None, remote_path: str | None = None,
                 branch: str | None = None, token: str | None = None,
                 client: GitHubClient | None = None):
        self.board_path = board_path
        self.marker_path = marker_path
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.last_success: str | None = None
        self.error: str | None = None
        self.mode = "checking" if repo and token else "local"
        self.auth_source = ""
        self.repo = repo
        self.remote_path = remote_path or board_path.name
        self.branch = branch
        self.client = client or (GitHubClient(token, repo) if repo and token else None)
        decode_board(self.board_path.read_bytes())
        self.target: Target | None = Target(repo, self.remote_path, branch) if repo and branch else None

    def _ensure_target(self) -> Target | None:
        if not self.client or not self.repo:
            return None
        if self.target is None:
            self.branch = self.branch or self.client.default_branch()
            self.target = Target(self.repo, self.remote_path, self.branch)
        return self.target

    def _marker(self, target: Target) -> str | None:
        try:
            value = json.loads(self.marker_path.read_text(encoding="utf-8"))
            if value.get("target") == target.key and re.fullmatch(r"[a-f0-9]{64}", value.get("digest", "")):
                return value["digest"]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def _write_marker(self, target: Target, raw: bytes) -> None:
        value = {"target": target.key, "digest": digest(raw)}
        atomic_write(self.marker_path, (json.dumps(value, indent=2) + "\n").encode("utf-8"), private=True)

    def _set_synced(self, target: Target, raw: bytes) -> None:
        self._write_marker(target, raw)
        self.mode = "synced"
        self.error = None
        self.last_success = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def info(self) -> dict[str, Any]:
        with self.lock:
            return {
                "mode": self.mode,
                "can_edit": self.mode not in ("conflict", "checking"),
                "error": self.error,
                "repo": self.repo,
                "path": self.remote_path if self.repo else None,
                "branch": self.branch,
                "last_success": self.last_success,
            }

    def read(self) -> dict[str, Any]:
        with self.lock:
            raw = self.board_path.read_bytes()
            return {"board": decode_board(raw), "revision": digest(raw), "sync": self.info()}

    def save(self, board: Any, expected: str) -> dict[str, Any]:
        encoded = encode_board(board)
        with self.lock:
            if self.mode in ("checking", "conflict"):
                raise KanbanError("Board is not editable until synchronization checking/conflict is resolved.")
            old = self.board_path.read_bytes()
            if digest(old) != expected:
                raise KanbanError("Board changed since it was opened. Reload before saving to avoid losing edits.")
            if encoded != old:
                atomic_write(self.board_path, encoded)
                self.mode = "pending" if self.client else "local"
                self.wake.set()
            return self.read()

    def sync_once(self) -> None:
        with self.lock:
            if not self.client:
                self.mode = "local"
                self.error = (
                    "No GitHub repository detected. Publish this folder as a GitHub repository "
                    "or set KANBAN_REPO=owner/name."
                    if not self.repo else
                    "No GitHub credentials. Run 'gh auth login' (recommended), "
                    "or set KANBAN_GITHUB_TOKEN with Contents read/write permission."
                )
                return
            try:
                target = self._ensure_target()
                assert target is not None
                local = self.board_path.read_bytes()
                decode_board(local)
                previous = self._marker(target)
                remote = self.client.get(target)
                if remote is None:
                    if previous is not None:
                        self.mode = "conflict"
                        self.error = "GitHub's board.js was deleted. Local data was preserved; choose which copy to keep."
                        return
                    self.client.put(target, local, None)
                    self._set_synced(target, local)
                    return
                remote_raw, remote_sha = remote
                if digest(local) == digest(remote_raw):
                    self._set_synced(target, local)
                elif previous is None:
                    if pristine_in_checkout(target.path, local):
                        atomic_write(self.board_path, remote_raw)
                        self._set_synced(target, remote_raw)
                    else:
                        self.mode = "conflict"
                        self.error = (
                            "The local and GitHub boards differ, with no previous sync record. "
                            "Nothing was overwritten. Choose which copy to keep."
                        )
                elif digest(local) == previous:
                    atomic_write(self.board_path, remote_raw)
                    self._set_synced(target, remote_raw)
                elif digest(remote_raw) == previous:
                    self.client.put(target, local, remote_sha)
                    self._set_synced(target, local)
                else:
                    self.mode = "conflict"
                    self.error = (
                        "Both the local board and GitHub changed. Nothing was overwritten. "
                        "Choose which copy to keep."
                    )
            except (OSError, KanbanError) as exc:
                if self.mode != "conflict":
                    self.mode = "pending"
                self.error = str(exc)

    def resolve(self, choice: str) -> dict[str, Any]:
        if choice not in ("local", "remote"):
            raise KanbanError("Resolution must be 'local' or 'remote'.")
        with self.lock:
            if self.mode != "conflict" or not self.client:
                raise KanbanError("There is no GitHub conflict to resolve.")
            target = self._ensure_target()
            assert target is not None
            remote = self.client.get(target)
            local = self.board_path.read_bytes()
            if choice == "remote":
                if remote is None:
                    raise KanbanError("No board.js exists on GitHub to keep.")
                remote_bytes, _ = remote
                self._backup(local, "local")
                atomic_write(self.board_path, remote_bytes)
                self._set_synced(target, remote_bytes)
            else:
                self.client.put(target, local, remote[1] if remote else None)
                self._set_synced(target, local)
            return self.read()

    def _backup(self, raw: bytes, kind: str) -> None:
        directory = self.marker_path.parent / ".kanban-backups"
        directory.mkdir(exist_ok=True)
        filename = f"board-{kind}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.js"
        atomic_write(directory / filename, raw, private=True)

    def loop(self) -> None:
        self.sync_once()
        while not self.stop.is_set():
            self.wake.wait(SYNC_INTERVAL)
            self.wake.clear()
            if self.stop.is_set():
                break
            self.sync_once()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: BoardStore):
        self.store = store
        super().__init__(address, BoardHandler)


class BoardHandler(BaseHTTPRequestHandler):
    server: Server

    def send_data(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def json_response(self, status: int, value: dict[str, Any]) -> None:
        self.send_data(status, json.dumps(value, ensure_ascii=True).encode("utf-8"), "application/json; charset=utf-8")

    def check_host(self) -> bool:
        host = self.headers.get("Host", "")
        port = self.server.server_port
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.json_response(HTTPStatus.FORBIDDEN, {"error": "Only the local board address is accepted."})
            return False
        return True

    def do_GET(self) -> None:
        if not self.check_host():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                self.send_data(200, (BASE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/board.js":
                self.send_data(200, self.server.store.board_path.read_bytes(), "application/javascript; charset=utf-8")
            elif path in ("/api/board", "/api/status"):
                result = self.server.store.read() if path == "/api/board" else self.server.store.info()
                self.json_response(200, result)
            elif path == "/favicon.ico":
                self.send_data(204, b"", "text/plain")
            else:
                self.json_response(404, {"error": "Not found."})
        except (OSError, KanbanError) as exc:
            self.json_response(500, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self.check_host():
            return
        port = self.server.server_port
        origin = self.headers.get("Origin", "")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self.json_response(403, {"error": "Only the board's own page may submit changes."})
            return
        if self.headers.get("Sec-Fetch-Site", "") not in ("", "same-origin", "none"):
            self.json_response(403, {"error": "Cross-site writes are forbidden."})
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self.json_response(415, {"error": "Send application/json."})
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
            if not 1 <= size <= MAX_BODY:
                self.json_response(413, {"error": "Request must be JSON and at most 512 KB."})
                return
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise KanbanError("Request must be a JSON object.")
            if self.path == "/api/board":
                if not isinstance(request.get("revision"), str):
                    raise KanbanError("Save requires the board revision last read.")
                self.json_response(200, self.server.store.save(request.get("board"), request["revision"]))
            elif self.path == "/api/resolve":
                self.json_response(200, self.server.store.resolve(request.get("choice")))
            else:
                self.json_response(404, {"error": "Not found."})
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.json_response(400, {"error": f"Invalid JSON: {exc}"})
        except GitHubError as exc:
            self.json_response(503, {"error": str(exc)})
        except KanbanError as exc:
            code = 409 if "changed" in str(exc) or "conflict" in str(exc) or "editable" in str(exc) else 400
            self.json_response(code, {"error": str(exc)})
        except OSError as exc:
            self.json_response(500, {"error": f"Could not save board: {exc}"})

    def log_message(self, format: str, *args: Any) -> None:
        # Avoid terminal output from every 15-second UI poll. Errors still show.
        if args and str(args[1]) in ("200", "204"):
            return
        super().log_message(format, *args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Human Kanban local server (standard library only).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("KANBAN_PORT", "8765")))
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        repo, path, branch = locate_target()
        token, token_source = obtain_token()
        store = BoardStore(repo=repo, remote_path=path, branch=branch, token=token)
        store.auth_source = token_source
        server = Server(("127.0.0.1", args.port), store)
    except (OSError, KanbanError, ValueError) as exc:
        print(f"ERROR: Cannot start Kanban: {exc}", file=sys.stderr)
        print(f"Read-only fallback: open {BASE / 'index.html'} directly in a browser.", file=sys.stderr)
        return 1
    worker = threading.Thread(target=store.loop, name="kanban-sync", daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{args.port}/"
    print("\nHuman Kanban", flush=True)
    print(f"  Board: {BASE}", flush=True)
    print(f"  Open:  {url}", flush=True)
    print(f"  GitHub: {repo or 'not detected (local editing only)'}", flush=True)
    print(f"  Auth: {token_source}", flush=True)
    if not repo:
        print("  Tip: publish this folder as a GitHub repository or set KANBAN_REPO=owner/name.", flush=True)
    elif not token:
        print("  Tip: run 'gh auth login' once for automatic GitHub synchronization.", flush=True)
    print("  Stop: Ctrl-C. The HTML file remains readable without this server.\n", flush=True)
    if not args.no_browser:
        threading.Timer(0.65, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nServer stopped. Read-only HTML view still works.")
    finally:
        store.stop.set()
        store.wake.set()
        server.server_close()
        worker.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
