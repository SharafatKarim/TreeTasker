#!/usr/bin/env python3
"""
Tree Tasker - a lightweight, responsive desktop Task Manager.

Built with PySide6 (Qt) + psutil.

Features
--------
* "Process Tree" tab  : QTreeView + QStandardItemModel showing the running
                        processes hierarchically (Parent -> Child) with the
                        columns Name / PID / CPU % / Memory %.  Rows are
                        updated *in place* every 2 s so the user's
                        expanded / collapsed state and selection survive.
* "Resource Usage" tab: QtCharts pie chart visualising how a chosen metric
                        ("CPU Usage", "Memory Usage", "Disk Read/Write" or
                        "Network/Internet") is distributed across processes.
                        Only the top consumers are shown individually; the
                        remainder is collapsed into an "Others" slice.

Design notes
------------
* All the heavy psutil work happens inside a background ``QThread``
  (``SamplerThread``).  The GUI thread only ever receives a ready-made
  snapshot via a signal, so the window never blocks while sampling.
* A single sample feeds *both* tabs, so we only walk the process table once
  per refresh cycle.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import psutil
from PySide6.QtCharts import QChart, QChartView, QPieSeries
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

# How often (milliseconds) we refresh the data shown in both tabs.
REFRESH_MS = 2000

# Number of individual slices the pie chart shows before everything else is
# folded into a single "Others" slice.
TOP_N_SLICES = 6


# --------------------------------------------------------------------------- #
#  Data model                                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class ProcInfo:
    """A flat snapshot of a single process, computed off the GUI thread."""

    pid: int
    ppid: int
    name: str
    cpu: float          # CPU percent (may exceed 100 on multi-core machines)
    mem: float          # Memory percent of total physical RAM
    disk: float         # Disk read+write rate in bytes/second
    net: float          # Proxy for network activity: open connection count


@dataclass
class Snapshot:
    """Everything the GUI needs for one refresh cycle."""

    procs: dict[int, ProcInfo] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
#  Background sampler                                                          #
# --------------------------------------------------------------------------- #
class SamplerThread(QThread):
    """
    Collects a :class:`Snapshot` every ``REFRESH_MS`` milliseconds and emits it.

    psutil's ``Process.cpu_percent()`` and per-process IO counters are both
    *delta* based: the returned value covers the interval since the previous
    call on the **same** ``Process`` object.  ``psutil.process_iter()`` caches
    its ``Process`` instances internally, so simply iterating it each cycle is
    enough to get correct CPU figures.  For disk throughput we keep the
    previous IO counters keyed by PID and compute the rate ourselves.
    """

    sampled = Signal(object)  # emits a Snapshot

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True
        # pid -> (read_bytes, write_bytes) from the previous cycle.
        self._prev_io: dict[int, tuple[int, int]] = {}

    def stop(self) -> None:
        """Ask the loop to exit and wait for it to finish."""
        self._running = False
        self.wait()

    # -- the worker loop ---------------------------------------------------- #
    def run(self) -> None:
        # Prime cpu_percent() so the *first* emitted sample already carries
        # meaningful values instead of a column full of zeros.
        for proc in psutil.process_iter():
            try:
                proc.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Sleep in small slices so stop() stays responsive.
        self._sleep_ms(REFRESH_MS)

        while self._running:
            self.sampled.emit(self._collect())
            self._sleep_ms(REFRESH_MS)

    def _sleep_ms(self, total_ms: int) -> None:
        slept = 0
        step = 50
        while self._running and slept < total_ms:
            self.msleep(step)
            slept += step

    def _collect(self) -> Snapshot:
        """Walk the process table once and build a Snapshot."""
        snap = Snapshot()
        seen: set[int] = set()
        # Seconds elapsed since the previous sample, used to turn the IO byte
        # deltas into a per-second rate.
        interval = REFRESH_MS / 1000.0

        # oneshot() batches the underlying system calls for a big speed-up.
        for proc in psutil.process_iter(
            ["pid", "ppid", "name", "cpu_percent", "memory_percent"]
        ):
            try:
                with proc.oneshot():
                    pid = proc.info["pid"]
                    seen.add(pid)

                    disk_rate = self._disk_rate(proc, pid, interval)
                    net = self._net_count(proc)

                    snap.procs[pid] = ProcInfo(
                        pid=pid,
                        ppid=proc.info["ppid"] or 0,
                        name=proc.info["name"] or f"pid {pid}",
                        cpu=proc.info["cpu_percent"] or 0.0,
                        mem=proc.info["memory_percent"] or 0.0,
                        disk=disk_rate,
                        net=net,
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                # Process vanished or we lack permission - just skip it.
                continue

        # Forget IO history for processes that have exited so the dict can't
        # grow without bound.
        self._prev_io = {p: v for p, v in self._prev_io.items() if p in seen}
        return snap

    def _disk_rate(self, proc: psutil.Process, pid: int, interval: float) -> float:
        """Bytes/second of disk IO for *proc*, or 0.0 if unavailable."""
        try:
            io = proc.io_counters()
        except (psutil.NoSuchProcess, psutil.AccessDenied, NotImplementedError,
                AttributeError):
            # io_counters() is unavailable on some platforms / for some procs.
            return 0.0

        cur = (io.read_bytes, io.write_bytes)
        prev = self._prev_io.get(pid)
        self._prev_io[pid] = cur
        if prev is None:
            return 0.0
        delta = (cur[0] - prev[0]) + (cur[1] - prev[1])
        return max(0.0, delta / interval)

    @staticmethod
    def _net_count(proc: psutil.Process) -> float:
        """
        Number of open network connections for *proc*.

        psutil cannot report per-process network *throughput* portably, so we
        use the count of open TCP/UDP connections as a lightweight proxy for
        how "network active" a process is.
        """
        try:
            # net_connections() is the modern spelling; fall back for older
            # psutil releases.
            getter = getattr(proc, "net_connections", proc.connections)
            return float(len(getter(kind="inet")))
        except (psutil.NoSuchProcess, psutil.AccessDenied, NotImplementedError):
            return 0.0


# --------------------------------------------------------------------------- #
#  Tab 1: Process Tree                                                         #
# --------------------------------------------------------------------------- #
# Column indices for the tree model.
COL_NAME, COL_PID, COL_CPU, COL_MEM = range(4)
PID_ROLE = Qt.UserRole + 1


class ProcessTreeTab(QWidget):
    """Hierarchical, in-place-updated view of the running processes."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.model = QStandardItemModel(self)
        self.model.setHorizontalHeaderLabels(["Name", "PID", "CPU %", "Memory %"])

        self.tree = QTreeView(self)
        self.tree.setModel(self.model)
        self.tree.setUniformRowHeights(True)        # faster rendering
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(False)          # avoid re-sorting churn
        self.tree.setEditTriggers(QTreeView.NoEditTriggers)

        header = self.tree.header()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for col in (COL_PID, COL_CPU, COL_MEM):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)

        # pid -> [name_item, pid_item, cpu_item, mem_item]
        self._rows: dict[int, list[QStandardItem]] = {}

    # -- update entry point ------------------------------------------------- #
    def update_data(self, snap: Snapshot) -> None:
        """Reconcile the tree with *snap*, keeping expansion state intact."""
        procs = snap.procs

        self._remove_dead(procs)
        self._create_missing(procs)
        self._update_values(procs)
        self._fix_parents(procs)

    # -- reconciliation steps ---------------------------------------------- #
    def _remove_dead(self, procs: dict[int, ProcInfo]) -> None:
        """Drop rows for processes that no longer exist.

        A dead node's still-living children are re-homed to the root first so
        they are not destroyed along with their parent; ``_fix_parents`` will
        later place them under their real parent (or leave them at the root).
        """
        root = self.model.invisibleRootItem()
        for pid in list(self._rows):
            if pid in procs:
                continue
            name_item = self._rows.pop(pid)[COL_NAME]
            while name_item.rowCount():
                root.appendRow(name_item.takeRow(0))
            parent = name_item.parent() or root
            parent.removeRow(name_item.row())

    def _create_missing(self, procs: dict[int, ProcInfo]) -> None:
        """Create (detached) item rows for processes we have not seen yet."""
        for pid, info in procs.items():
            if pid in self._rows:
                continue
            name_item = QStandardItem(info.name)
            name_item.setData(pid, PID_ROLE)
            pid_item = QStandardItem(str(pid))
            cpu_item = QStandardItem()
            mem_item = QStandardItem()
            for item in (pid_item, cpu_item, mem_item):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._rows[pid] = [name_item, pid_item, cpu_item, mem_item]

    def _update_values(self, procs: dict[int, ProcInfo]) -> None:
        """Refresh the per-process numbers without touching the hierarchy."""
        for pid, info in procs.items():
            name_item, _, cpu_item, mem_item = self._rows[pid]
            if name_item.text() != info.name:
                name_item.setText(info.name)
            cpu_item.setText(f"{info.cpu:.1f}")
            mem_item.setText(f"{info.mem:.1f}")

    def _fix_parents(self, procs: dict[int, ProcInfo]) -> None:
        """Attach each row under its parent process (or the root)."""
        root = self.model.invisibleRootItem()
        for pid, info in procs.items():
            row = self._rows[pid]
            name_item = row[COL_NAME]

            # Resolve the desired parent item: the parent's row if that parent
            # is alive and isn't the process itself, otherwise the root.
            parent_row = self._rows.get(info.ppid)
            desired = (
                parent_row[COL_NAME]
                if parent_row is not None and info.ppid != pid
                else root
            )

            # An item is only "placed" once it actually lives in the model.
            # A freshly-created row has model() is None and parent() is None,
            # so we must not mistake "parent is root" for "already attached".
            attached = name_item.model() is not None
            current = name_item.parent() or root
            if attached and current is desired:
                continue  # already correctly placed - leave expansion intact

            # Detach (preserving any child subtree) and re-attach under desired.
            if attached:
                taken = current.takeRow(name_item.row())
            else:
                taken = row                          # never attached yet
            desired.appendRow(taken)


# --------------------------------------------------------------------------- #
#  Tab 2: Resource Usage pie chart                                            #
# --------------------------------------------------------------------------- #
# label shown in the combo box  ->  attribute name on ProcInfo
METRICS = {
    "CPU Usage": "cpu",
    "Memory Usage": "mem",
    "Disk Read/Write": "disk",
    "Network/Internet": "net",
}


class ResourceUsageTab(QWidget):
    """Pie chart of how a chosen metric is distributed across processes."""

    def __init__(self, parent=None):
        super().__init__(parent)

        self.selector = QComboBox(self)
        self.selector.addItems(METRICS.keys())

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Metric:"))
        controls.addWidget(self.selector)
        controls.addStretch(1)

        self.series = QPieSeries()
        self.chart = QChart()
        self.chart.addSeries(self.series)
        self.chart.legend().setAlignment(Qt.AlignRight)
        self.chart.setAnimationOptions(QChart.NoAnimation)

        self.chart_view = QChartView(self.chart, self)
        self.chart_view.setRenderHint(QPainter.Antialiasing)

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(self.chart_view)

        # Keep the last snapshot so changing the metric re-renders instantly,
        # without waiting for the next 2-second refresh.
        self._last: Snapshot | None = None
        self.selector.currentTextChanged.connect(self._rebuild)

    def update_data(self, snap: Snapshot) -> None:
        self._last = snap
        self._rebuild()

    def _rebuild(self) -> None:
        if self._last is None:
            return

        attr = METRICS[self.selector.currentText()]
        # (value, name) pairs for processes that actually consume the metric.
        values = [
            (getattr(info, attr), info.name)
            for info in self._last.procs.values()
            if getattr(info, attr) > 0
        ]
        values.sort(reverse=True, key=lambda pair: pair[0])

        self.series.clear()

        if not values:
            self.series.append("No data", 1)
            self.chart.setTitle(f"{self.selector.currentText()} - no activity")
            return

        top = values[:TOP_N_SLICES]
        others = sum(v for v, _ in values[TOP_N_SLICES:])
        total = sum(v for v, _ in values) or 1.0

        for value, name in top:
            slice_ = self.series.append(f"{name} ({value / total:.0%})", value)
            slice_.setLabelVisible(True)
        if others > 0:
            slice_ = self.series.append(f"Others ({others / total:.0%})", others)
            slice_.setLabelVisible(True)

        self.chart.setTitle(self._title(attr, total))

    @staticmethod
    def _title(attr: str, total: float) -> str:
        if attr == "disk":
            return f"Disk Read/Write - {ResourceUsageTab._human(total)}/s total"
        if attr == "net":
            return f"Open connections - {int(total)} total"
        if attr == "mem":
            return f"Memory Usage - {total:.1f}% of RAM in view"
        return f"CPU Usage - {total:.1f}% total"

    @staticmethod
    def _human(num: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if num < 1024 or unit == "GB":
                return f"{num:.1f} {unit}"
            num /= 1024
        return f"{num:.1f} GB"


# --------------------------------------------------------------------------- #
#  Main window                                                                #
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tree Tasker")
        self.resize(900, 600)

        self.tree_tab = ProcessTreeTab(self)
        self.usage_tab = ResourceUsageTab(self)

        tabs = QTabWidget(self)
        tabs.addTab(self.tree_tab, "Process Tree")
        tabs.addTab(self.usage_tab, "Resource Usage")
        self.setCentralWidget(tabs)

        self.statusBar().showMessage("Sampling...")

        # Start the background sampler and wire its signal to both tabs.
        self.sampler = SamplerThread(self)
        self.sampler.sampled.connect(self._on_sample)
        self.sampler.start()

    def _on_sample(self, snap: Snapshot) -> None:
        self.tree_tab.update_data(snap)
        self.usage_tab.update_data(snap)
        self.statusBar().showMessage(f"{len(snap.procs)} processes")

    def closeEvent(self, event) -> None:
        # Make sure the worker thread stops cleanly before the app exits.
        self.sampler.stop()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
