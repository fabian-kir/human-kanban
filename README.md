# Human Kanban

A small, human-first Kanban board. It works in Safari, Chrome, and Edge, on macOS and Windows. No framework, build step, database, Docker, or paid hosting is required.

**The entire board is in this directory.** `index.html` contains the complete interface and `board.js` contains all board state. `server.py` enables edits and synchronizes **only `board.js`** with a GitHub repository through the GitHub Contents API. It never invokes `git pull`, `git add`, `git commit`, `git push`, or changes other repository files.

The board is for people. Agents can read and edit `board.js` like any other source file, but the board does not execute agents, infer priorities, or grant permission to work on a card.

## Start here

| Platform | Edit in a browser | View without server |
| --- | --- | --- |
| **macOS** | Double-click **`Start Kanban.command`** | Open **`index.html`** in Safari |
| **Windows** | Double-click **`Start Kanban.cmd`** | Open **`index.html`** in Edge or Chrome |
| **Terminal** | `bash start-kanban.bash` or `python3 server.py` | Open **`index.html`** directly |

The launcher checks Python and required files, prints actionable errors, and opens `http://127.0.0.1:8765/` in your default browser. **Python 3.10+ is the only runtime requirement.** No `pip install` is needed. The launcher finds a suitable Python even when the first `python3` on `PATH` is too old (macOS ships 3.9 at `/usr/bin/python3`): it tries `python3.14` down to `python3.10`, Homebrew's and `~/.local/bin`'s `python3`, then any Python [uv](https://docs.astral.sh/uv/) manages (`uv python find`). The terminal window must remain running while you edit; Ctrl-C stops the server. A different port can be selected with `--port 8766`.

Without Python, or if the server is stopped, `index.html` loads the existing board from `board.js` in **read-only mode**. Its editing controls are hidden. You still can search, open cards, and read descriptions. The browser never needs direct filesystem-write permissions.

To synchronize between devices, set up GitHub once as described below. Until then, the server permits **local editing** and clearly labels it *not synced*; changes remain safely in `board.js`.

## Put the whole project in a private GitHub repository

1. Install [Git](https://git-scm.com/), [GitHub CLI](https://cli.github.com/), and [Python 3.10+](https://www.python.org/downloads/). On macOS, Git may already be present. Only Python is required for local editing; Git and GitHub CLI make initial private publishing and repeated authentication straightforward.
2. Extract the project ZIP. It already contains an initialized local Git repository with an initial commit.
3. In Terminal / PowerShell, change into the extracted project directory and run:

   ```bash
   gh auth login
   gh repo create human-kanban --private --source=. --remote=origin --push
   ```

   GitHub CLI creates the **private** remote repository and pushes the existing project. It is the only routine setup step that pushes source files. Afterwards, the local board server synchronizes `board.js` directly with GitHub using HTTPS. GitHub CLI stores your login in its normal credential storage; no token goes into the board files.

4. Start Kanban again. The terminal should print your GitHub repository and `Auth: GitHub CLI`. The board's top-right status changes to **Saved · GitHub synchronized** after its first successful check.

On a second device, install Python and GitHub CLI, authenticate with `gh auth login`, clone **your private repo**, and double-click that device's launcher. There is no paid server to keep running. Each computer runs its own local server; GitHub stores the shared board state.

**Alternative to GitHub CLI:** Set `KANBAN_GITHUB_TOKEN` to a fine-grained GitHub personal access token restricted to the desired repository with **Contents: read and write** permission. Use your operating system's credential store or a temporary environment variable; **never write a token into the repo, `board.js`, HTML, or launcher.** If GitHub CLI is not installed and no token is configured, edits stay local until synchronization is configured.

## Everyday use

- Create, rename, recolour, and remove columns (an occupied column must be emptied first).
- Add cards with a title and optional description. Open a card to view or edit it.
- Drag cards across columns or within a column (desktop browsers); use the **Column** selector in the card editor as a keyboard-friendly alternative.
- Search titles and descriptions, even in read-only mode.
- Rename the board. Every edit is written atomically to `board.js`.

The server checks GitHub every ~40 seconds and also wakes immediately after local edits; browser tabs check the local server every 12 seconds. This is **periodic, not real-time collaboration**. There are no WebSockets or paid services.

### State and conflict behavior

`board.js` is the **only remotely synchronized state file**. The server also keeps a tiny ignored local `.kanban-sync.json` marker recording the last synced file hash; this is not board content and is never uploaded. GitHub file updates use GitHub's blob SHA to detect concurrent updates.

- When only GitHub changed, the server downloads the newer `board.js`.
- When only the local `board.js` changed (including an agent's valid file edit), the server uploads that file.
- When **both** changed, neither is overwritten. The UI shows a conflict with explicit **Keep GitHub** and **Keep local** choices. *Keep GitHub* backs up the local version under ignored `.kanban-backups/` first. *Keep local* replaces the remote file; its previous version remains available in GitHub history.
- If GitHub is offline or credentials are missing, local edits remain on disk and are retried when the server can sync. The status never claims unsynced work is synced.
- If two browser tabs edit stale copies, the later save is rejected instead of silently replacing the first one.

On a fresh clone, local `board.js` normally equals the remote file. If GitHub has moved ahead of the checkout and the checked-out file is still unmodified, the server can safely download it. If both copies differ without a known baseline, it asks you to choose.

**GitHub history:** Every Contents API update creates a commit on the sync branch, `kanban-state` unless `KANBAN_BRANCH` names another. The code branch never receives board-state commits, so pulling, pushing and merging development work needs no reconciliation with them. When the sync branch does not exist yet, the server creates it once from the head of the default branch, through the GitHub API; that copy carries the default branch's `board.js` as the starting state. This **does not run Git commands or update your local Git HEAD**, and the Kanban server never resets, merges, or modifies unrelated files.

Consequence for a checkout of the code branch: the local `board.js` follows `kanban-state`, so `git status` shows it modified. Leave it out of code commits. A fresh clone still starts correctly: an unmodified checked-out `board.js` is replaced by the newer copy from `kanban-state` on the first sync.

## Adding Human Kanban to an existing repository (later, optional)

**This is a future installation procedure, not an action performed by this project.** Keep your existing working tree out of a cloud-sync folder. Move/copy the complete board **as one folder** into the target repo:

```text
YourRepository/
  AGENTS.md                 # if you already use agents
  kanban/
    .gitignore
    index.html
    board.js                # the only state file synced to GitHub
    server.py
    start-kanban.bash
    Start Kanban.command
    Start Kanban.cmd
    README.md               # optional but recommended
    tests/                  # optional tests
```

Do **not** copy this project's nested `.git/`, `.kanban-sync.json`, or `.kanban-backups/`. When copied, `kanban/.gitignore` also excludes the two local helper files from the containing repository. Commit the added `kanban/` files and push them **once, as an explicit ordinary source change** using your usual Git workflow. Do not start remote sync until the board exists in the target repository and you know which branch contains it.

Nothing needs to be installed inside the target application's kernel or existing web server. The launcher runs its **own loopback-only local server**, and its HTML is still viewable without that server.

The board locates its new GitHub destination automatically from the **containing repository's `origin` remote** and calculates the path of `kanban/board.js` relative to the Git root. For a normal GitHub checkout, nothing else needs configuring. If auto-detection is not possible, see the overrides below.

If agents should consult the board during their normal work, add a single *human-owned* instruction to the existing repository's agent-entrypoint (for example `AGENTS.md`):

> Read `kanban/board.js` for the current human-maintained work board at session start. Treat its priorities as context, not permission to implement tasks. Update cards only when specifically authorized. The board is operated by people; no agent workflow is implied.

The state is intentionally a JavaScript assignment to JSON data (`window.KANBAN_BOARD = { ... };`), so it can be read by people and agents as plain text and by browsers opened from `file://`. Retain the exact assignment wrapper and the `version: 1` schema when editing it directly. A column may carry an optional `"color": "#rrggbb"`. The local server validates it and detects external changes. A standalone `fetch('board.json')` would not reliably work from `file://` in Safari.

## Overrides (only if needed)

| Variable or flag | Purpose | Default |
| --- | --- | --- |
| `KANBAN_REPO=owner/name` | GitHub repository when `origin` is absent or not on github.com | Detect `origin` |
| `KANBAN_BRANCH=name` | Remote branch to synchronize; created from the default branch if missing | `kanban-state` |
| `KANBAN_REMOTE_PATH=kanban/board.js` | Path of board state **within the remote repo** | Detect relative to Git root |
| `KANBAN_GITHUB_TOKEN=...` | GitHub token (do not commit) | `GH_TOKEN`, `GITHUB_TOKEN`, then `gh auth token` |
| `KANBAN_PORT=8766` or `--port 8766` | Local HTTP port | `8765` |
| `--no-browser` | Do not open the browser automatically | Open browser |

These are optional overrides, not required configuration files. Nothing needs to be hardcoded into a script or HTML. Port binding is always `127.0.0.1`, not your LAN/IP/public internet interface.

## Startup and error messages

| Situation | What happens | What to do |
| --- | --- | --- |
| Python missing / too old | Launcher prints the requirement and stops | Install Python 3.10+ (python.org, `brew install python`, or `uv python install 3.12`); meanwhile open `index.html` |
| One of the three core files missing | Launcher refuses to start | Keep `index.html`, `board.js`, `server.py` together |
| Port already in use | Server reports a bind error | Stop the other server or use `--port 8766` |
| No Git remote | Local editing works; sync says not connected | Publish privately, or use `KANBAN_REPO` |
| GitHub CLI missing / not logged in | Local editing works; sync says not connected | Run `gh auth login`, or use a fine-grained token |
| Missing GitHub permission | Local editing works, GitHub error shown, state queued locally | Give the token Contents read/write permission |
| Network unavailable | Local edits remain on disk; sync retries | Reconnect and leave the server running |
| Both copies changed | Editing pauses to avoid data loss | Pick **Keep GitHub** or **Keep local** explicitly |
| Server stopped | HTML still opens with cards and descriptions | Open `index.html`, or restart launcher to edit |

**Security:** The server listens only on loopback, checks Host and Origin on writes, requires JSON POSTs, and does not implement cross-origin access. The token is handled by the local Python process, never sent to browser code. Do not expose port 8765 via a public tunnel or bind this server to `0.0.0.0`; it is deliberately **not** a multi-user, remotely authenticated service. The repository is private only after you create it as private on GitHub; the local folder and ZIP do not provide access control.

## Run the tests

No extra packages needed:

```bash
python3 -m unittest discover -s tests -v
```

The tests cover local saves, single-file remote sync, remote pulls, agent-style file changes, concurrent-edit rejection, conflict resolution and backup, missing credentials, real HTTP endpoints, and origin protection. Browser behavior was also manually exercised in a headless Chromium test; Safari and Windows need an on-device check.

## Files

| File | Role |
| --- | --- |
| `index.html` | Complete HTML/CSS/JavaScript Kanban interface and read-only fallback |
| `board.js` | The **sole authoritative board state**, human/agent readable, remotely synchronized |
| `server.py` | Local standard-library HTTP server plus GitHub Contents API sync |
| `Start Kanban.command` | macOS Finder double-click launcher |
| `start-kanban.bash` | Shared Bash launcher with startup checks and clear errors |
| `Start Kanban.cmd` | Windows double-click launcher with dependency checks |
| `.gitignore` | Excludes local sync metadata, backups, and Python caches |
| `tests/` | Standard-library tests |

No source code from Ecotonomous is included and no Ecotonomous files are changed.
