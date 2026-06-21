#!/usr/bin/env python3
"""
Tree Tasker (Web) - a beautiful, interactive process-ancestry explorer.

A small Flask backend reads the live process table with psutil and serves it
as a nested JSON tree (systemd -> bash -> python -> ...).  The browser front
end (templates/index.html) renders it as an animated, collapsible D3.js tree
so you can explore how every process descends from its ancestors.

Run:
    uv run webapp.py
then open http://127.0.0.1:5000
"""

from __future__ import annotations

import time

import psutil
from flask import Flask, jsonify, render_template

app = Flask(__name__)

# Fields we pull for every process in one batched walk.
_FIELDS = ["pid", "ppid", "name", "cpu_percent", "memory_percent",
           "create_time", "username"]


def _prime_cpu() -> None:
    """
    psutil's ``cpu_percent()`` is delta-based: the first call on a process
    always returns 0.0.  Calling it once at start-up "primes" psutil's internal
    Process cache so the very first request already carries real CPU figures.
    """
    for proc in psutil.process_iter():
        try:
            proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def build_tree() -> dict:
    """Walk the process table once and return a nested ancestry tree."""
    nodes: dict[int, dict] = {}

    for proc in psutil.process_iter(_FIELDS):
        try:
            info = proc.info
            pid = info["pid"]
            nodes[pid] = {
                "pid": pid,
                "ppid": info["ppid"] or 0,
                "name": info["name"] or f"pid {pid}",
                "cpu": round(info["cpu_percent"] or 0.0, 1),
                "mem": round(info["memory_percent"] or 0.0, 1),
                "user": info["username"] or "",
                # Epoch seconds; the browser turns this into a human "age".
                "started": info["create_time"] or 0,
                "children": [],
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # Process vanished or is inaccessible - just skip it.
            continue

    # Link every node under its parent; anything whose parent is missing
    # (or is itself) becomes a top-level root.
    roots: list[dict] = []
    for pid, node in nodes.items():
        parent = nodes.get(node["ppid"])
        if parent is not None and node["ppid"] != pid:
            parent["children"].append(node)
        else:
            roots.append(node)

    # A single synthetic root keeps D3 happy (one hierarchy, many real roots).
    return {
        "pid": 0,
        "ppid": -1,
        "name": "System",
        "cpu": 0.0,
        "mem": 0.0,
        "user": "",
        "started": psutil.boot_time(),
        "children": sorted(roots, key=lambda n: n["pid"]),
        "count": len(nodes),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/tree")
def api_tree():
    """Live process-ancestry tree as JSON, plus a server timestamp."""
    return jsonify({"tree": build_tree(), "now": time.time()})


if __name__ == "__main__":
    _prime_cpu()
    # threaded=True so a slow psutil walk never blocks a second request.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
