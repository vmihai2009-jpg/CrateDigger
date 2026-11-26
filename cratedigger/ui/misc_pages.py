from __future__ import annotations

import json

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget, QScrollArea)

from .. import __version__
from ..core.config import APP_DIR, DEFAULT_GENRE_RULES, LOG_DIR
from ..core.logging_setup import LOG_FILE
from .common import hbox, open_path


class ActivityPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 12)
        title = QLabel("Activity log")
        title.setObjectName("h1")
        openb = QPushButton("Open logs folder")
        openb.clicked.connect(lambda: open_path(str(LOG_DIR)))
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        lay.addWidget(hbox(title, None, refresh, openb))
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(QFont("Menlo", 11))
        lay.addWidget(self.text, 1)

    def refresh(self):
        try:
            with open(LOG_FILE, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 200_000))
                data = fh.read().decode("utf-8", "replace")
        except OSError:
            data = "(no log yet)"
        self.text.setPlainText(data)
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())


class SettingsPage(QWidget):
    check_updates_requested = Signal()

    def __init__(self, svc, on_saved, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.on_saved = on_saved
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 12)
        title = QLabel("Settings")
        title.setObjectName("h1")
        outer.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        lay = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        s = svc.settings

        g1 = QGroupBox("Folders")
        f1 = QFormLayout(g1)
        self.library_root = QLineEdit(s.library_root)
        pick = QPushButton("Choose…")
        pick.clicked.connect(lambda: self._pick(self.library_root))
        f1.addRow("Library root", hbox(self.library_root, pick))
        self.watch = QPlainTextEdit("\n".join(s.watch_folders))
        self.watch.setFixedHeight(60)
        f1.addRow("Watch folders\n(one per line)", self.watch)
        self.existing = QPlainTextEdit("\n".join(s.existing_collection))
        self.existing.setFixedHeight(60)
        f1.addRow("Existing collection\n(read-only dup check)", self.existing)
        self.pattern = QLineEdit(s.filename_pattern)
        f1.addRow("Filename pattern", self.pattern)
        lay.addWidget(g1)

        g2 = QGroupBox("Searching & matching")
        f2 = QFormLayout(g2)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(3, 120)
        self.interval.setValue(s.search_interval_s)
        self.interval.setSuffix(" s")
        self.window = QDoubleSpinBox()
        self.window.setRange(5, 300)
        self.window.setValue(s.search_window_s)
        self.window.setSuffix(" s")
        self.maxres = QSpinBox()
        self.maxres.setRange(10, 2000)
        self.maxres.setValue(s.max_results_per_track)
        self.tol = QDoubleSpinBox()
        self.tol.setRange(1, 60)
        self.tol.setValue(s.duration_tolerance_s)
        self.tol.setSuffix(" s")
        self.minscore = QSpinBox()
        self.minscore.setRange(0, 100)
        self.minscore.setValue(s.min_match_score)
        f2.addRow("Gap between searches", self.interval)
        f2.addRow("Collect results for", self.window)
        f2.addRow("Max results per search", self.maxres)
        f2.addRow("Duration tolerance", self.tol)
        f2.addRow("Hide candidates below score", self.minscore)
        lay.addWidget(g2)

        g4 = QGroupBox("Automatic approval of best matches")
        f4 = QFormLayout(g4)
        from ..core.service import AUTH_BASES
        self.auto_on = QCheckBox("Automatically approve the best match for every track")
        self.auto_on.setChecked(s.auto_approve_best)
        self.auto_basis = QComboBox()
        self.auto_basis.addItem("— choose —", "")
        for b in AUTH_BASES:
            self.auto_basis.addItem(b, b)
        self.auto_basis.setCurrentIndex(max(0, self.auto_basis.findData(s.auto_basis)))
        self.auto_note = QLineEdit(s.auto_note)
        self.auto_note.setPlaceholderText("Licence URL / permission reference (stored with every file)")
        self.auto_min = QSpinBox()
        self.auto_min.setRange(85, 100)
        self.auto_min.setValue(s.auto_min_score)
        self.auto_confirm = QCheckBox("I confirm I have permission to obtain every recording this downloads")
        self.auto_confirm.setChecked(s.auto_approve_best)
        expl = QLabel("When on: after each search the best match (score ≥ minimum, no security or size flags, best "
                      "quality) is selected and downloaded under this standing authorisation. Each file is "
                      "virus-scanned and quality-checked on arrival and approved into the library only if the scan is "
                      "clean, there are no quality warnings and the match is still ≥ minimum. Everything else waits "
                      "for you. The virus scan is never skipped.")
        expl.setWordWrap(True)
        expl.setObjectName("muted")
        f4.addRow(self.auto_on)
        f4.addRow("Authorisation basis", self.auto_basis)
        f4.addRow("Evidence / note", self.auto_note)
        f4.addRow("Minimum match score", self.auto_min)
        f4.addRow(self.auto_confirm)
        f4.addRow(expl)
        lay.addWidget(g4)

        g5 = QGroupBox("Updates")
        f5 = QFormLayout(g5)
        self.check_updates = QCheckBox("Check for updates when CrateDigger starts")
        self.check_updates.setChecked(s.check_updates)
        check_now = QPushButton("Check now")
        check_now.clicked.connect(self.check_updates_requested.emit)
        f5.addRow(self.check_updates)
        f5.addRow(f"Installed version {__version__}", hbox(check_now, None))
        lay.addWidget(g5)

        g3 = QGroupBox("Genre folder rules (JSON: [folder, [keywords…]], first match wins)")
        v3 = QVBoxLayout(g3)
        self.rules = QPlainTextEdit(json.dumps(s.genre_rules, indent=1))
        self.rules.setFont(QFont("Menlo", 11))
        self.rules.setMinimumHeight(180)
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(lambda: self.rules.setPlainText(json.dumps(DEFAULT_GENRE_RULES, indent=1)))
        v3.addWidget(self.rules)
        v3.addWidget(hbox(reset, None))
        lay.addWidget(g3)

        info = QLabel(f"Database, logs and the Nicotine+ bridge channel live in {APP_DIR}")
        info.setObjectName("muted")
        lay.addWidget(info)
        lay.addStretch(1)
        save = QPushButton("Save settings")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        outer.addWidget(hbox(None, save))

    def _pick(self, edit):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
        if d:
            edit.setText(d)

    def save(self):
        s = self.svc.settings
        try:
            rules = json.loads(self.rules.toPlainText())
            assert all(isinstance(r[0], str) and isinstance(r[1], list) for r in rules)
        except Exception:
            QMessageBox.critical(self, "Invalid rules", "Genre rules must be a JSON list of [\"Folder\", [\"keyword\", …]]")
            return
        try:
            self.pattern.text().format(artist="a", title="t")
        except (KeyError, IndexError, ValueError):
            QMessageBox.critical(self, "Invalid pattern", "Pattern may only use {artist} and {title}")
            return
        s.library_root = self.library_root.text().strip()
        s.watch_folders = [x.strip() for x in self.watch.toPlainText().splitlines() if x.strip()]
        s.existing_collection = [x.strip() for x in self.existing.toPlainText().splitlines() if x.strip()]
        s.filename_pattern = self.pattern.text().strip() or "{artist} - {title}"
        s.search_interval_s = self.interval.value()
        s.search_window_s = self.window.value()
        s.max_results_per_track = self.maxres.value()
        s.duration_tolerance_s = self.tol.value()
        s.min_match_score = self.minscore.value()
        s.genre_rules = rules
        turning_on = self.auto_on.isChecked() and not s.auto_approve_best
        if self.auto_on.isChecked():
            if not self.auto_basis.currentData():
                QMessageBox.critical(self, "Choose a basis", "Automatic approval needs an authorisation basis.")
                return
            if not self.auto_confirm.isChecked():
                QMessageBox.critical(self, "Confirmation needed", "Tick the confirmation to enable automatic approval.")
                return
        s.auto_approve_best = self.auto_on.isChecked()
        s.auto_basis = self.auto_basis.currentData() or ""
        s.auto_note = self.auto_note.text().strip()
        s.auto_min_score = self.auto_min.value()
        s.check_updates = self.check_updates.isChecked()
        s.save()
        if turning_on:
            from .common import run_bg
            self._sweep = run_bg(self.svc.run_auto_sweep, on_done=lambda n: QMessageBox.information(
                self, "Automatic approval on", f"{n} tracks waiting for review were queued for download now. "
                "New searches will be processed automatically."))
        self.on_saved()
        QMessageBox.information(self, "Saved", "Settings saved. Genre rules apply to newly imported tracks; "
                                "existing tracks can be moved via right-click → Set library folder.")
