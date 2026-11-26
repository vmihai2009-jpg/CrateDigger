from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from ..core.service import TRACK_STATUSES
from . import theme
from .common import StatCard, open_path


class Dashboard(QWidget):
    navigate = Signal(str)

    def __init__(self, svc, parent=None):
        super().__init__(parent)
        self.svc = svc
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(14)
        title = QLabel("Dashboard")
        title.setObjectName("h1")
        lay.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.cards = {}
        for i, (key, label) in enumerate([("tracks", "Tracks imported"), ("searched", "Tracks searched"),
                                          ("matched", "Matches found"), ("review", "Need review"),
                                          ("missing", "Missing"), ("approved", "Approved files")]):
            c = StatCard(label)
            self.cards[key] = c
            grid.addWidget(c, 0, i)
        lay.addLayout(grid)

        # Status breakdown bar
        self.bar = QFrame()
        self.bar.setFixedHeight(10)
        self.bar_lay = QHBoxLayout(self.bar)
        self.bar_lay.setContentsMargins(0, 0, 0, 0)
        self.bar_lay.setSpacing(1)
        self.legend = QLabel()
        self.legend.setObjectName("muted")
        self.legend.setTextFormat(Qt.RichText)
        lay.addWidget(self.bar)
        lay.addWidget(self.legend)

        # Search progress
        prog = QFrame()
        prog.setObjectName("card")
        pl = QVBoxLayout(prog)
        pl.setContentsMargins(14, 10, 14, 12)
        self.search_label = QLabel("No searches running")
        self.search_bar = QProgressBar()
        self.search_bar.setFixedHeight(6)
        pl.addWidget(self.search_label)
        pl.addWidget(self.search_bar)
        lay.addWidget(prog)

        # Bridge setup / status
        bridge = QFrame()
        bridge.setObjectName("card")
        bl = QVBoxLayout(bridge)
        bl.setContentsMargins(14, 10, 14, 12)
        bl.setSpacing(6)
        h = QLabel("Soulseek connection (via Nicotine+)")
        h.setStyleSheet("font-weight:600;")
        self.bridge_state = QLabel()
        self.bridge_state.setTextFormat(Qt.RichText)
        self.bridge_help = QLabel()
        self.bridge_help.setObjectName("muted")
        self.bridge_help.setWordWrap(True)
        self.bridge_help.setTextFormat(Qt.RichText)
        self.install_btn = QPushButton("Install / update bridge plugin")
        self.install_btn.clicked.connect(self.install_plugin)
        open_btn = QPushButton("Open Nicotine+")
        open_btn.clicked.connect(lambda: open_path("/Applications/Nicotine+.app"))
        row = QHBoxLayout()
        row.addWidget(self.install_btn)
        row.addWidget(open_btn)
        row.addStretch(1)
        bl.addWidget(h)
        bl.addWidget(self.bridge_state)
        bl.addWidget(self.bridge_help)
        bl.addLayout(row)
        lay.addWidget(bridge)

        # Antivirus status
        av = QFrame()
        av.setObjectName("card")
        al = QVBoxLayout(av)
        al.setContentsMargins(14, 10, 14, 12)
        ah = QLabel("Virus & malware scanning")
        ah.setStyleSheet("font-weight:600;")
        self.av_state = QLabel()
        self.av_state.setTextFormat(Qt.RichText)
        self.av_help = QLabel("Every file is scanned before it can enter the library: built-in checks (real file type, "
                              "executable/script/archive signatures, disguised names) plus a ClamAV signature scan. "
                              "Files that are not scanned clean are never approved.")
        self.av_help.setObjectName("muted")
        self.av_help.setWordWrap(True)
        al.addWidget(ah)
        al.addWidget(self.av_state)
        al.addWidget(self.av_help)
        lay.addWidget(av)

        # Quick actions
        qa = QHBoxLayout()
        for text, page in (("Import / manage playlist →", "playlist"), ("Review results →", "results"),
                           ("Library && Rekordbox export →", "library")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, p=page: self.navigate.emit(p))
            qa.addWidget(b)
        qa.addStretch(1)
        lay.addLayout(qa)
        lay.addStretch(1)

    def install_plugin(self):
        try:
            p = self.svc.bridge.install_plugin()
        except Exception as e:
            QMessageBox.critical(self, "Install failed", str(e))
            return
        QMessageBox.information(
            self, "Plugin installed",
            f"Installed to:\n{p}\n\nIn Nicotine+: Preferences → Plugins → tick “CrateDigger Bridge”. "
            "If it was already enabled, untick and tick it again to load the new version.")
        self.refresh()

    def refresh(self):
        s = self.svc.stats()
        total = s["tracks"] or 0
        self.cards["tracks"].set(total)
        self.cards["searched"].set(s["searched"], f"{s['pending_searches']} in queue" if s["pending_searches"] else "")
        self.cards["matched"].set(s["matched"], f"score ≥ {self.svc.settings.min_match_score}")
        self.cards["review"].set(s["review"])
        self.cards["missing"].set(s["missing"])
        self.cards["approved"].set(s["approved"])

        while self.bar_lay.count():
            w = self.bar_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        legend = []
        for st in TRACK_STATUSES:
            n = s["by_status"].get(st, 0)
            if not n:
                continue
            seg = QFrame()
            seg.setStyleSheet(f"background:{theme.STATUS_COLORS.get(st, theme.MUTED)}; border-radius:2px;")
            self.bar_lay.addWidget(seg, n)
            legend.append(f"<span style='color:{theme.STATUS_COLORS.get(st)}'>■</span> {st.replace('_', ' ')} {n}")
        self.legend.setText("&nbsp;&nbsp;&nbsp;".join(legend) or "Import a Spotify CSV to begin.")

        done = self.svc.db.scalar("SELECT COUNT(*) FROM searches WHERE status IN ('done','failed','cancelled')") or 0
        allq = self.svc.db.scalar("SELECT COUNT(*) FROM searches") or 0
        pend = s["pending_searches"]
        if pend:
            run = self.svc.db.one("SELECT query FROM searches WHERE status='running' ORDER BY id DESC LIMIT 1")
            self.search_label.setText(f"Searching: {pend} pending" + (f" — now: “{run['query']}”" if run else ""))
            self.search_bar.setMaximum(max(allq, 1))
            self.search_bar.setValue(done)
        else:
            self.search_label.setText(f"No searches running ({done} completed)")
            self.search_bar.setMaximum(1)
            self.search_bar.setValue(1 if allq else 0)

        from ..core.security import SIG_DB_STALE_DAYS, engine_info
        ei = engine_info()
        stale = ei.db_age_days is not None and ei.db_age_days > SIG_DB_STALE_DAYS
        avc = theme.BAD if not ei.available else theme.WARN if stale else theme.OK
        self.av_state.setText(f"<span style='color:{avc}'>●</span> {ei.message}")

        st = self.svc.bridge.status()
        color = theme.OK if st.logged_in else theme.WARN if st.installed else theme.BAD
        self.bridge_state.setText(f"<span style='color:{color}'>●</span> {st.message}")
        outdated = st.installed and self.svc.bridge.plugin_outdated()
        self.install_btn.setText("Update bridge plugin" if outdated else
                                 "Reinstall bridge plugin" if st.installed else "Install bridge plugin")
        if not st.installed:
            help_ = ("1. Click <b>Install bridge plugin</b>.  2. Open Nicotine+ → Preferences → Plugins → enable "
                     "<b>CrateDigger Bridge</b>.  3. Make sure Nicotine+ is logged in. Searches then run through "
                     "your existing Nicotine+ session — CrateDigger never asks for your Soulseek password.")
        elif not st.plugin_running:
            help_ = ("Start Nicotine+ and enable <b>CrateDigger Bridge</b> under Preferences → Plugins. "
                     "Queued searches wait and are sent automatically once the plugin is running.")
        elif outdated:
            help_ = "A newer plugin version ships with this app: click Update, then re-enable it in Nicotine+."
        else:
            help_ = (f"Searches are paced at one every {self.svc.settings.search_interval_s:g}s to respect the Soulseek "
                     "network. Without the plugin you can still copy queries into Nicotine+ manually and import "
                     "the downloaded files with Library → Scan folders.")
        self.bridge_help.setText(help_)
