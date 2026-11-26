from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from ..core import library as lib
from ..core import rekordbox
from . import theme
from .common import (Col, DictTableModel, fmt_dur, fmt_size, hbox, make_table, needs_fresh_av, open_path,
                     reveal_in_finder, run_bg, selected_rows)

LIB_COLS = [
    Col("Dup", "dup", fmt=lambda r: f"#{r['dup']}" if r.get("dup") else "", color=lambda r: theme.WARN, width=40,
        sort=lambda r: r.get("dup") or 0),
    Col("Folder", "genre_folder", width=100, editable=True),
    Col("Artist", "artist", width=170, editable=True),
    Col("Title", "title", width=220, editable=True),
    Col("Album", "album", width=150, editable=True),
    Col("Label", "label", width=110, editable=True),
    Col("Year", "year", width=44, editable=True),
    Col("BPM", "bpm", fmt=lambda r: f"{r['bpm']:.0f}" if r["bpm"] else "", width=44, align="right", editable=True,
        sort=lambda r: r["bpm"] or 0),
    Col("Key", "musical_key", width=40, editable=True),
    Col("Codec", "codec", width=56),
    Col("kbps", "bitrate", align="right", width=48),
    Col("Rate", "sample_rate", fmt=lambda r: f"{r['sample_rate'] / 1000:g}k" if r["sample_rate"] else "", width=44),
    Col("Length", "duration_s", fmt=lambda r: fmt_dur(r["duration_s"]), sort=lambda r: r["duration_s"] or 0, width=50,
        align="right"),
    Col("Size", "size", fmt=lambda r: fmt_size(r["size"]), sort=lambda r: r["size"] or 0, width=66, align="right"),
    Col("Tags", "tags_written", fmt=lambda r: "✓" if r["tags_written"] else "✗",
        color=lambda r: theme.OK if r["tags_written"] else theme.BAD, width=36),
    Col("File", "library_path", fmt=lambda r: Path(r["library_path"]).name, tip=lambda r: r["library_path"],
        color=lambda r: None if r.get("exists") else theme.BAD, width=300),
]


class LibraryPage(QWidget):
    changed = Signal()

    def __init__(self, svc, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.workers = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 12)
        lay.setSpacing(8)
        title = QLabel("Library")
        title.setObjectName("h1")
        self.root_label = QLabel()
        self.root_label.setObjectName("muted")
        lay.addWidget(hbox(title, self.root_label, None))

        self.scan_btn = QPushButton("Scan folders for matches")
        self.scan_btn.setToolTip("Read-only scan of your watch folders (e.g. Nicotine+ downloads) for files that match "
                                 "playlist tracks. Matches appear in Search results as LOCAL candidates.")
        self.scan_btn.clicked.connect(lambda: self.scan())
        export = QPushButton("Export for Rekordbox")
        export.setObjectName("primary")
        export.clicked.connect(self.export)
        openf = QPushButton("Open library folder")
        openf.clicked.connect(lambda: self._open_root())
        self.only_dups = QCheckBox("Duplicates only")
        self.only_dups.toggled.connect(self.apply_filter)
        self.text = QLineEdit()
        self.text.setPlaceholderText("Filter…")
        self.text.textChanged.connect(self.apply_filter)
        self.count = QLabel()
        self.count.setObjectName("muted")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(160)
        self.progress.setVisible(False)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("muted")
        lay.addWidget(hbox(export, self.scan_btn, openf, self.progress, self.progress_label, None, self.only_dups,
                           self.text, self.count))
        hint = QLabel("Double-click a cell to edit metadata — changes are written to the library copy only "
                      "(and the file is moved if artist/title/folder change). Originals are never modified.")
        hint.setObjectName("muted")
        lay.addWidget(hint)

        self.model = DictTableModel(LIB_COLS)
        self.model.edited.connect(self.on_edit)
        self.view, self.proxy = make_table(self.model)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.menu)
        lay.addWidget(self.view, 1)

    def _open_root(self):
        root = Path(self.svc.settings.library_root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        open_path(str(root))

    def refresh(self):
        self.root_label.setText(f"  {self.svc.settings.library_root}")
        groups = lib.duplicate_groups(self.svc.db)
        rows = []
        for r in self.svc.db.q("SELECT * FROM library ORDER BY genre_folder, artist, title"):
            d = dict(r)
            d["dup"] = groups.get(r["id"])
            d["exists"] = Path(r["library_path"]).exists()
            rows.append(d)
        self.model.set_rows(rows)
        self.apply_filter()

    def apply_filter(self):
        t = self.text.text().lower()
        dups = self.only_dups.isChecked()
        self.proxy.predicate = lambda r: (not dups or r.get("dup")) and (
            not t or t in f"{r['artist']} {r['title']} {r['album']} {r['label']} {r['genre_folder']}".lower())
        self.proxy.invalidateFilter()
        n_d = len({r["dup"] for r in self.model.rows if r.get("dup")})
        self.count.setText(f"{self.proxy.rowCount()} files" + (f" · {n_d} duplicate groups" if n_d else ""))

    def on_edit(self, row, key, value):
        v = value.strip()
        if key == "bpm":
            try:
                v = float(v) if v else None
            except ValueError:
                QMessageBox.warning(self, "Invalid BPM", "BPM must be a number")
                return
        try:
            lib.update_entry(self.svc.db, self.svc.settings, row["id"], {key: v})
        except Exception as e:
            QMessageBox.critical(self, "Update failed", str(e))
        self.refresh()

    def menu(self, pos):
        rows = selected_rows(self.view, self.proxy)
        if not rows:
            return
        m = QMenu(self)
        m.addAction("Reveal in Finder", lambda: reveal_in_finder(rows[0]["library_path"]))
        if rows[0]["source_path"]:
            m.addAction("Reveal original source", lambda: reveal_in_finder(rows[0]["source_path"]))
        fm = m.addMenu("Move to folder")
        for f in [r[0] for r in self.svc.settings.genre_rules] + ["Unsorted"]:
            fm.addAction(f, lambda f=f: self._move(rows, f))
        m.addAction("Re-write tags", lambda: self._retag(rows))
        m.addSeparator()
        m.addAction("Remove from library (keep file)", lambda: self._remove(rows, False))
        m.addAction("Remove and delete library copy…", lambda: self._remove(rows, True))
        m.exec(self.view.viewport().mapToGlobal(pos))

    def _move(self, rows, folder):
        for r in rows:
            try:
                lib.update_entry(self.svc.db, self.svc.settings, r["id"], {"genre_folder": folder})
            except Exception as e:
                QMessageBox.critical(self, "Move failed", str(e))
        self.refresh()

    def _retag(self, rows):
        for r in rows:
            try:
                lib.update_entry(self.svc.db, self.svc.settings, r["id"], {"artist": r["artist"]})
                lib.write_tags(Path(r["library_path"]), {"artist": r["artist"], "title": r["title"], "album": r["album"],
                                                         "genre": r["genre"], "bpm": r["bpm"], "key": r["musical_key"],
                                                         "isrc": r["isrc"], "label": r["label"], "year": r["year"]})
            except Exception as e:
                QMessageBox.critical(self, "Tagging failed", str(e))
        self.refresh()

    def _remove(self, rows, delete):
        if delete and QMessageBox.question(
                self, "Delete library copies",
                f"Delete {len(rows)} file(s) from the CrateDigger library folder?\n\nOnly the library copies are "
                "deleted; the original source files are not touched. This cannot be undone.") != QMessageBox.Yes:
            return
        for r in rows:
            lib.remove_entry(self.svc.db, self.svc.settings, r["id"], delete_copy=delete)
        self.refresh()
        self.changed.emit()

    @needs_fresh_av
    def scan(self):
        folders = list(self.svc.settings.watch_folders) + list(self.svc.settings.existing_collection)
        folders = [f for f in folders if f and Path(f).expanduser().is_dir()]
        if not folders:
            QMessageBox.information(self, "No folders", "Add watch folders in Settings (e.g. your Nicotine+ downloads "
                                    "folder).")
            return
        self.scan_btn.setEnabled(False)
        self.progress.setVisible(True)

        def prog(i, n, msg):
            self.progress.setMaximum(max(n, 1))
            self.progress.setValue(i)
            self.progress_label.setText(f"{i}/{n}")

        def done(n):
            self.scan_btn.setEnabled(True)
            self.progress.setVisible(False)
            self.progress_label.setText(f"{n} new local matches")
            self.changed.emit()

        def fail(msg):
            self.scan_btn.setEnabled(True)
            self.progress.setVisible(False)
            QMessageBox.critical(self, "Scan failed", msg)

        self.workers.append(run_bg(self.svc.scan_folders, folders, on_done=done, on_fail=fail, on_progress=prog))

    def export(self):
        try:
            res = rekordbox.export(self.svc.db, self.svc.settings)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        msg = (f"Exported {res['tracks']} tracks and {len(res['playlists'])} playlists.\n\n{res['xml']}\n\n"
               "In rekordbox: Preferences → Advanced → Database → rekordbox xml → Imported Library: choose this "
               "file. Then show “rekordbox xml” in the tree (Preferences → View → Layout) and import the "
               "CrateDigger playlists. M3U8 playlists are also in the M3U8 folder next to it.")
        if res["missing_files"]:
            msg += f"\n\n{res['missing_files']} library entries were skipped because their file is missing."
        QMessageBox.information(self, "Rekordbox export", msg)
        reveal_in_finder(res["xml"])
