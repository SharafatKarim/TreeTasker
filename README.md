# 🌳 Tree Tasker

A lightweight process explorer that shows how every process on your machine
descends from its ancestors — `systemd → bash → python → …`. It ships in two
flavours that share the same idea (and the same [psutil](https://github.com/giampaolo/psutil)
data source):

| App | Stack | What it is |
| --- | --- | --- |
| **Desktop** (`main.py`) | PySide6 (Qt) + QtCharts | A native task-manager window with a live process tree and a resource-usage pie chart. |
| **Web** (`webapp.py`) | Flask + D3.js | A browser-based, animated, collapsible process-ancestry tree you can pan, zoom and search. |

---

## Features

### Desktop app (`main.py`)
- **Process Tree tab** — a hierarchical `QTreeView` (Name / PID / CPU % / Memory %).
  Rows are reconciled **in place** every 2 seconds, so your expanded/collapsed
  state and selection survive each refresh.
- **Resource Usage tab** — a pie chart of how a chosen metric is distributed
  across processes: **CPU**, **Memory**, **Disk Read/Write**, or
  **Network/Internet** (open-connection count). Top consumers are shown
  individually; the rest collapse into an *“Others”* slice.
- **Non-blocking sampling** — all the heavy psutil work runs on a background
  `QThread`; the GUI only ever receives a ready-made snapshot, so the window
  never freezes while sampling.

### Web app (`webapp.py`)
- **Animated D3 tree** of the full process ancestry, served as nested JSON.
- **Interactive** — click a node to fold/unfold its descendants, hover for
  CPU/memory/age details, scroll to zoom, drag to pan.
- **Search** by process name or PID.
- **Lineage highlight** — select a node to trace its path back to the root.
- **Auto-refresh** every 3 seconds that keeps your zoom/pan and folded nodes
  intact (only the first load auto-fits the view).
- CPU-hungry processes (≥ 15 %) are tinted warm so they stand out.

---

## Requirements

- **Python ≥ 3.12**
- Dependencies (managed via `pyproject.toml`):
  - [`flask`](https://flask.palletsprojects.com/) ≥ 3.1.3
  - [`psutil`](https://github.com/giampaolo/psutil) ≥ 7.2.2
  - [`PySide6`](https://doc.qt.io/qtforpython/) ≥ 6.11.1

This project uses [**uv**](https://docs.astral.sh/uv/) for dependency and
environment management.

---

## Installation

```bash
# Clone the repository, then enter it
git clone <repository-url>
cd tree_tasker

# Install dependencies into a managed virtual environment
uv sync
```

> Don't have `uv`? Install it with `pipx install uv` (or see the
> [uv install guide](https://docs.astral.sh/uv/getting-started/installation/)),
> or fall back to a plain virtualenv:
> ```bash
> python -m venv .venv && source .venv/bin/activate
> pip install "flask>=3.1.3" "psutil>=7.2.2" "pyside6>=6.11.1"
> ```

---

## Usage

### Desktop app

```bash
uv run main.py
```

A native window opens with the **Process Tree** and **Resource Usage** tabs.

### Web app

```bash
uv run webapp.py
```

Then open <http://127.0.0.1:5000> in your browser.

| Action | How |
| --- | --- |
| Fold / unfold a subtree | Click a node |
| Inspect a process | Hover over a node |
| Zoom | Scroll |
| Pan | Drag |
| Search | Type a name or PID in the search box |
| Re-center | Click **Fit** |
| Toggle live updates | **Auto-refresh** checkbox |

---

## How it works

Both apps walk the live process table **once per refresh cycle** with psutil and
build a parent → child hierarchy keyed by PID. A few psutil details worth knowing:

- `cpu_percent()` and per-process IO counters are **delta-based** — the first
  call returns `0.0`. Both apps *prime* psutil at start-up so the very first
  sample already carries real numbers.
- Disk throughput is computed as a **bytes/second rate** from successive IO
  counter readings; network activity uses the **open-connection count** as a
  portable proxy (psutil can't report per-process throughput everywhere).
- Processes that vanish mid-walk or that you lack permission to read are simply
  skipped.

The web backend exposes a single JSON endpoint:

| Route | Description |
| --- | --- |
| `GET /` | Serves the D3.js front end (`templates/index.html`). |
| `GET /api/tree` | Returns the live ancestry tree plus a server timestamp. |

---

## Project structure

```
tree_tasker/
├── main.py              # PySide6 desktop task manager
├── webapp.py            # Flask backend for the web explorer
├── templates/
│   └── index.html       # D3.js front end
├── pyproject.toml       # Project metadata & dependencies
└── README.md
```

---

## Notes & limitations

- Some metrics (per-process disk IO, connection counts) require elevated
  privileges; without them those values fall back to `0`.
- The web app binds to `127.0.0.1` only — it is intended for local use, not as a
  public service.

---

## License

No license has been specified yet. Add one (e.g. MIT) before distributing.
