"""Reusable UI pieces: a fast list-of-dicts table model, workers, small widgets."""
from __future__ import annotations

import logging
import subprocess
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QObject, QRunnable, QSortFilterProxyModel, Qt,
                            QThreadPool, Signal)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QLabel, QTableView, QVBoxLayout,
                               QWidget)

from . import theme

log = logging.getLogger(__name__)
SORT_ROLE = Qt.UserRole + 1


@dataclass
class Col:
    header: str
    key: str
    fmt: Callable[[dict], Any] | None = None     # display text
    sort: Callable[[dict], Any] | None = None    # sort value
    color: Callable[[dict], str | None] | None = None
    tip: Callable[[dict], str | None] | None = None
    width: int | None = None
    align: str = "left"
    editable: bool = False


class DictTableModel(QAbstractTableModel):
    edited = Signal(dict, str, object)  # row, key, new value

    def __init__(self, cols: list[Col], parent=None):
        super().__init__(parent)
        self.cols = cols
        self.rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(self.cols)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.cols[section].header
        return None

    def flags(self, index):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if self.cols[index.column()].editable:
            f |= Qt.ItemIsEditable
        return f

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        c = self.cols[index.column()]
        if role in (Qt.DisplayRole, Qt.EditRole):
            if role == Qt.EditRole and c.editable:
                v = row.get(c.key)
                return "" if v is None else str(v)
            v = c.fmt(row) if c.fmt else row.get(c.key)
            return "" if v is None else str(v)
        if role == SORT_ROLE:
            v = c.sort(row) if c.sort else (c.fmt(row) if c.fmt else row.get(c.key))
            if v is None:
                return -1e18 if c.align == "right" else ""
            return v
        if role == Qt.ForegroundRole and c.color:
            col = c.color(row)
            return QColor(col) if col else None
        if role == Qt.ToolTipRole and c.tip:
            return c.tip(row)
        if role == Qt.TextAlignmentRole:
            return int((Qt.AlignRight if c.align == "right" else Qt.AlignCenter if c.align == "center" else Qt.AlignLeft)
                       | Qt.AlignVCenter)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        row = self.rows[index.row()]
        c = self.cols[index.column()]
        if str(row.get(c.key) or "") == str(value):
            return False
        self.edited.emit(row, c.key, value)
        return True


class SortProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.predicate: Callable[[dict], bool] | None = None

    def lessThan(self, a, b):
        va, vb = a.data(SORT_ROLE), b.data(SORT_ROLE)
        try:
            return va < vb
        except TypeError:
            return str(va) < str(vb)

    def filterAcceptsRow(self, source_row, source_parent):
        if self.predicate is None:
            return True
        return self.predicate(self.sourceModel().rows[source_row])


def make_table(model: DictTableModel, multi=True) -> tuple[QTableView, SortProxy]:
    proxy = SortProxy()
    proxy.setSourceModel(model)
    view = QTableView()
    view.setModel(proxy)
    view.setSortingEnabled(True)
    view.setAlternatingRowColors(True)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.ExtendedSelection if multi else QAbstractItemView.SingleSelection)
    view.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(22)
    view.setShowGrid(False)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.ElideRight)
    h = view.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.Interactive)
    h.setStretchLastSection(True)
    h.setHighlightSections(False)
    for i, c in enumerate(model.cols):
        if c.width:
            view.setColumnWidth(i, c.width)
    return view, proxy


def selected_rows(view: QTableView, proxy: SortProxy) -> list[dict]:
    rows = sorted({proxy.mapToSource(i).row() for i in view.selectionModel().selectedRows()})
    return [proxy.sourceModel().rows[r] for r in rows]


# ------------------------------------------------------------------ background work
class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)


class Worker(QRunnable):
    """Run fn(*args, progress=cb) in the global thread pool; signals arrive on the UI thread."""

    def __init__(self, fn, *args, with_progress=False, **kw):
        super().__init__()
        self.fn, self.args, self.kw = fn, args, kw
        self.signals = _Signals()
        self.with_progress = with_progress
        self.cancelled = False

    def run(self):
        try:
            if self.with_progress:
                self.kw["progress"] = lambda i, n, msg="": self.signals.progress.emit(i, n, msg)
            self.signals.done.emit(self.fn(*self.args, **self.kw))
        except Exception as e:
            log.error("background task failed: %s\n%s", e, traceback.format_exc())
            self.signals.failed.emit(str(e))


# Workers (and their signal objects) must stay alive until their result has been delivered on the UI thread;
# if Python frees them while a queued signal is pending, Qt crashes (segfault in shiboken).
_ACTIVE: set = set()


def run_bg(fn, *args, on_done=None, on_fail=None, on_progress=None, **kw) -> Worker:
    from PySide6.QtCore import QTimer
    w = Worker(fn, *args, with_progress=on_progress is not None, **kw)
    w.setAutoDelete(False)  # Python owns the runnable
    _ACTIVE.add(w)

    def release(*_):
        QTimer.singleShot(0, lambda: _ACTIVE.discard(w))  # after all slots for this emission have run

    if on_done:
        w.signals.done.connect(on_done)
    if on_fail:
        w.signals.failed.connect(on_fail)
    if on_progress:
        w.signals.progress.connect(on_progress)
    w.signals.done.connect(release)
    w.signals.failed.connect(release)
    QThreadPool.globalInstance().start(w)
    return w


# ------------------------------------------------------------------ widgets
class StatCard(QFrame):
    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(2)
        self.value = QLabel("0")
        self.value.setObjectName("statValue")
        self.sub = QLabel("")
        self.sub.setObjectName("muted")
        l2 = QLabel(label)
        l2.setObjectName("statLabel")
        lay.addWidget(l2)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)

    def set(self, v, sub: str = ""):
        self.value.setText(str(v))
        self.sub.setText(sub)


def hbox(*widgets, spacing=6, margins=(0, 0, 0, 0), stretch_at: int | None = None) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for i, x in enumerate(widgets):
        if x is None:
            lay.addStretch(1)
        else:
            lay.addWidget(x)
    return w


def reveal_in_finder(path: str) -> None:
    subprocess.Popen(["open", "-R", path])


def open_path(path: str) -> None:
    subprocess.Popen(["open", path])


def fmt_dur(s: float | None) -> str:
    if not s:
        return ""
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def fmt_size(n: int | None) -> str:
    if not n:
        return ""
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    return f"{n / (1 << 20):.1f} MB"


def score_color(score) -> str | None:
    if score is None:
        return None
    return theme.OK if score >= 85 else theme.WARN if score >= 60 else theme.BAD


# ------------------------------------------------------------------ daily virus-definition update gate
def needs_fresh_av(method):
    """Decorator for UI actions that fetch tracks (search, download, scan).

    On the first such action of a calendar day, freshclam runs first (in the background, with a progress
    dialog); the action continues when it finishes. Later actions that day run immediately.
    """
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        from PySide6.QtWidgets import QMessageBox, QProgressDialog

        from ..core.security import signatures_updated_today, update_signatures_if_new_day
        if signatures_updated_today():
            return method(self, *args, **kwargs)
        dlg = QProgressDialog("Updating virus definitions before fetching tracks…\n(first time today)", None, 0, 0, self)
        dlg.setWindowTitle("CrateDigger")
        dlg.setMinimumDuration(0)
        dlg.setCancelButton(None)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.show()

        def done(res):
            dlg.close()
            if not res.ok:
                QMessageBox.warning(self, "Virus definitions not updated", res.message)
            method(self, *args, **kwargs)

        def fail(msg):
            dlg.close()
            QMessageBox.warning(self, "Virus definitions not updated", msg)
            method(self, *args, **kwargs)

        self._av_worker = run_bg(update_signatures_if_new_day, on_done=done, on_fail=fail)

    return wrapper
