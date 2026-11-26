from __future__ import annotations

import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QMainWindow, QProgressBar, QStackedWidget, QWidget)

from . import theme
from .common import run_bg
from .dashboard import Dashboard
from .library_view import LibraryPage
from .misc_pages import ActivityPage, SettingsPage
from .playlist import PlaylistPage
from .results import ResultsPage
from .updates import UpdateController

log = logging.getLogger(__name__)
PAGES = ["dashboard", "playlist", "results", "library", "activity", "settings"]
LABELS = ["Dashboard", "Playlist", "Search results", "Library", "Activity", "Settings"]


class MainWindow(QMainWindow):
    def __init__(self, svc):
        super().__init__()
        self.svc = svc
        self.setWindowTitle("CrateDigger")
        self.resize(1440, 860)
        central = QWidget()
        central.setObjectName("central")
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(170)
        brand = QLabel("  CRATE<span style='color:%s'>DIGGER</span>" % theme.ACCENT)
        brand.setStyleSheet("font-weight:800; font-size:14px; letter-spacing:1px; padding:14px 6px; background:%s;"
                            % theme.BG2)
        side = QWidget()
        side.setStyleSheet(f"background:{theme.BG2};")
        from PySide6.QtWidgets import QVBoxLayout
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(brand)
        sl.addWidget(self.nav, 1)
        side.setFixedWidth(170)
        for label in LABELS:
            self.nav.addItem(label)
        self.stack = QStackedWidget()
        lay.addWidget(side)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self.dashboard = Dashboard(svc)
        self.playlist = PlaylistPage(svc)
        self.results = ResultsPage(svc)
        self.library = LibraryPage(svc)
        self.activity = ActivityPage()
        self.settings_page = SettingsPage(svc, self.refresh_all)
        self.pages = {"dashboard": self.dashboard, "playlist": self.playlist, "results": self.results,
                      "library": self.library, "activity": self.activity, "settings": self.settings_page}
        for name in PAGES:
            self.stack.addWidget(self.pages[name])
        self.nav.currentRowChanged.connect(self._on_nav)
        self.dashboard.navigate.connect(self.goto)
        self.playlist.open_track.connect(self.open_track)
        for p in (self.playlist, self.results, self.library):
            p.changed.connect(self._mark_dirty)

        # status bar
        sb = self.statusBar()
        self.bridge_lbl = QLabel()
        self.queue_lbl = QLabel()
        self.queue_bar = QProgressBar()
        self.queue_bar.setFixedWidth(140)
        self.queue_bar.setFixedHeight(6)
        sb.addWidget(self.bridge_lbl)
        sb.addPermanentWidget(self.queue_lbl)
        sb.addPermanentWidget(self.queue_bar)

        self._dirty = True
        self._ticks = 0
        self._analyzing: set[int] = set()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.inbox_timer = QTimer(self)  # playlists sent from Minapp arrive as CSV files in the inbox folder
        self.inbox_timer.timeout.connect(self.check_inbox)
        self.inbox_timer.start(2000)
        QTimer.singleShot(500, self.check_inbox)
        self.nav.setCurrentRow(0)
        self.tick()

        self.updates = UpdateController(svc, self)
        self.settings_page.check_updates_requested.connect(lambda: self.updates.check(manual=True))
        if svc.settings.check_updates:
            QTimer.singleShot(4000, self.updates.check)

    # ------------------------------------------------------------ navigation
    def goto(self, name: str):
        self.nav.setCurrentRow(PAGES.index(name))

    def open_track(self, track_id: int):
        self.goto("results")
        self.results.show_track(track_id)

    def _on_nav(self, row):
        self.stack.setCurrentIndex(row)
        self.refresh_current()

    def refresh_current(self):
        page = self.stack.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()

    def refresh_all(self):
        self.playlist.reload_playlists()
        self.refresh_current()

    def _mark_dirty(self):
        self._dirty = True
        if self.stack.currentWidget() is self.dashboard:
            self.dashboard.refresh()

    def check_inbox(self):
        got = self.svc.import_inbox()
        if not got:
            return
        ok = [(n, r) for n, r in got if not isinstance(r, str)]
        bad = [(n, r) for n, r in got if isinstance(r, str)]
        added = sum(r.added for _, r in ok)
        msg = f"Received {len(ok)} playlist{'s' if len(ok) != 1 else ''} from Minapp ({added} new tracks)"
        if bad:
            msg += f"; {len(bad)} could not be read"
        self.statusBar().showMessage(msg, 15000)
        self.playlist.reload_playlists()
        self._mark_dirty()
        self.refresh_current()

    # ------------------------------------------------------------ polling loop
    def tick(self):
        self._ticks += 1
        try:
            changed = self.svc.process_bridge_events()
            if self._ticks % 10 == 0:
                self.svc.check_timeouts()
        except Exception:
            log.exception("bridge polling failed")
            changed = set()
        # Verify finished downloads automatically
        for cid in self.svc.downloads_to_analyze():
            if cid not in self._analyzing:
                self._analyzing.add(cid)
                run_bg(self.svc.analyze_candidate, cid,
                       on_done=lambda _, c=cid: (self._analyzing.discard(c), self._mark_dirty()),
                       on_fail=lambda msg, c=cid: log.error("analysis of candidate %s failed: %s", c, msg))
        st = self.svc.bridge.status()
        color = theme.OK if st.logged_in else theme.WARN if st.installed else theme.BAD
        self.bridge_lbl.setText(f"<span style='color:{color}'>●</span> Nicotine+: {st.message}")
        pend = self.svc.db.scalar("SELECT COUNT(*) FROM searches WHERE status IN ('queued','sent','running')") or 0
        total = self.svc.db.scalar("SELECT COUNT(*) FROM searches") or 0
        self.queue_lbl.setText(f"{pend} searches pending" if pend else "Search queue idle")
        self.queue_bar.setMaximum(max(total, 1))
        self.queue_bar.setValue(total - pend if total else 0)
        page = self.stack.currentWidget()
        if changed or (self._dirty and self._ticks % 2 == 0):
            self._dirty = False
            if page is self.dashboard:
                self.dashboard.refresh()
            elif page is self.playlist:
                self.playlist.refresh()
            elif page is self.results:
                if self.results.track_id in changed or not changed:
                    self.results.load_candidates()
                if changed and self._ticks % 3 == 0:
                    self.results.refresh()
        elif page is self.dashboard and self._ticks % 3 == 0:
            self.dashboard.refresh()
