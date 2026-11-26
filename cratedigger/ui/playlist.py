from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QInputDialog, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ..core.quality import TIER_LABELS
from ..core.service import TRACK_STATUSES
from ..core.text import camelot, key_name
from . import theme
from .common import (Col, DictTableModel, fmt_dur, hbox, make_table, needs_fresh_av, open_path, score_color,
                     selected_rows)

TRACK_SQL = """
SELECT t.*,
  (SELECT MAX(score) FROM candidates c WHERE c.track_id=t.id AND c.state!='rejected') AS best_score,
  (SELECT MIN(quality_tier) FROM candidates c WHERE c.track_id=t.id AND c.state!='rejected' AND c.score>=?) AS best_tier,
  (SELECT COUNT(*) FROM candidates c WHERE c.track_id=t.id) AS n_results,
  (SELECT COUNT(*) FROM searches s WHERE s.track_id=t.id) AS n_searches,
  (SELECT group_concat(p.name, ', ') FROM playlist_tracks pt JOIN playlists p ON p.id=pt.playlist_id
     WHERE pt.track_id=t.id) AS playlists
FROM tracks t
"""


def track_rows(db, min_score, playlist_id=None):
    sql = TRACK_SQL
    params = [min_score]
    if playlist_id:
        sql += " JOIN playlist_tracks px ON px.track_id=t.id AND px.playlist_id=? ORDER BY px.position"
        params.append(playlist_id)
    else:
        sql += " ORDER BY t.id"
    return [dict(r) for r in db.q(sql, params)]


TRACK_COLS = [
    Col("#", "id", width=40, align="right", sort=lambda r: r["id"]),
    Col("Status", "status", width=84, color=lambda r: theme.STATUS_COLORS.get(r["status"]),
        fmt=lambda r: r["status"].replace("_", " ")),
    Col("Artist", "artists", fmt=lambda r: r["artists"].replace(";", ", "), width=170),
    Col("Title", "title", width=220),
    Col("Album", "album", width=150),
    Col("Time", "duration_ms", fmt=lambda r: fmt_dur((r["duration_ms"] or 0) / 1000), sort=lambda r: r["duration_ms"] or 0,
        width=48, align="right"),
    Col("BPM", "tempo", fmt=lambda r: f"{r['tempo']:.0f}" if r["tempo"] else "", sort=lambda r: r["tempo"] or 0,
        width=44, align="right"),
    Col("Key", "musical_key", fmt=lambda r: f"{camelot(r['musical_key'], r['mode'])} {key_name(r['musical_key'], r['mode'])}".strip(),
        width=64),
    Col("Folder", "genre_folder", width=100),
    Col("Best", "best_score", align="right", width=44, color=lambda r: score_color(r["best_score"]),
        sort=lambda r: r["best_score"] if r["best_score"] is not None else -1),
    Col("Quality", "best_tier", fmt=lambda r: TIER_LABELS.get(r["best_tier"], "") if r["best_tier"] else "", width=74,
        sort=lambda r: r["best_tier"] or 99),
    Col("Results", "n_results", align="right", width=56, sort=lambda r: r["n_results"]),
    Col("Search query", "search_query", width=200, editable=True),
    Col("Genres", "genres", width=180),
    Col("Label", "label", width=120),
    Col("ISRC", "isrc", width=104),
]


class PlaylistPage(QWidget):
    open_track = Signal(int)
    changed = Signal()

    def __init__(self, svc, parent=None):
        super().__init__(parent)
        self.svc = svc
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 12)
        lay.setSpacing(8)
        title = QLabel("Playlist")
        title.setObjectName("h1")

        imp = QPushButton("Import CSV…")
        imp.setObjectName("primary")
        imp.clicked.connect(self.import_csv)
        self.playlist_box = QComboBox()
        self.playlist_box.setMinimumWidth(180)
        self.playlist_box.currentIndexChanged.connect(self.refresh)
        lay.addWidget(hbox(title, None, QLabel("Playlist"), self.playlist_box, imp))

        self.search_sel = QPushButton("Search selected")
        self.search_sel.clicked.connect(lambda: self.search_selected())
        self.search_all = QPushButton("Search all new")
        self.search_all.setObjectName("primary")
        self.search_all.clicked.connect(lambda: self.search_new())
        retry = QPushButton("Retry missing/failed")
        retry.setToolTip("Re-search missing or failed tracks with the next fallback query")
        retry.clicked.connect(lambda: self.retry())
        cancel = QPushButton("Cancel queue")
        cancel.clicked.connect(self.cancel)
        copyq = QPushButton("Copy queries")
        copyq.setToolTip("Copy the search queries of the selected tracks, for pasting into Nicotine+ manually")
        copyq.clicked.connect(self.copy_queries)

        self.text = QLineEdit()
        self.text.setPlaceholderText("Filter artist / title / label…")
        self.text.textChanged.connect(self.apply_filter)
        self.genre = QComboBox()
        self.genre.currentIndexChanged.connect(self.apply_filter)
        self.status = QComboBox()
        self.status.addItem("All statuses", None)
        for s in TRACK_STATUSES:
            self.status.addItem(s.replace("_", " "), s)
        self.status.currentIndexChanged.connect(self.apply_filter)
        self.bpm_lo = QDoubleSpinBox()
        self.bpm_hi = QDoubleSpinBox()
        for b, v in ((self.bpm_lo, 0), (self.bpm_hi, 250)):
            b.setRange(0, 250)
            b.setDecimals(0)
            b.setValue(v)
            b.setFixedWidth(60)
            b.valueChanged.connect(self.apply_filter)
        self.count = QLabel()
        self.count.setObjectName("muted")
        review = QPushButton("Review && select best")
        review.setToolTip("For every track in this playlist, select the best match (score ≥ 85, no security or size "
                          "flags; best quality, then free slot). Writes a review page to ~/Tracks/<playlist>/.")
        review.clicked.connect(lambda: self.auto_review())
        bulk = QPushButton("Authorise && download selected…")
        bulk.setToolTip("Queue the selected/best match of the highlighted tracks (or the whole playlist) in "
                        "Nicotine+, after you record one authorisation basis.")
        bulk.clicked.connect(lambda: self.bulk_download())
        export = QPushButton("Export to ~/Tracks")
        export.setObjectName("primary")
        export.setToolTip("Copy this playlist's approved tracks to ~/Tracks/<playlist>/ and update "
                          "~/Tracks/CrateDigger_rekordbox.xml (mihai → Tracks → <playlist>)")
        export.clicked.connect(lambda: self.export_playlist())
        lay.addWidget(hbox(self.search_all, self.search_sel, retry, cancel, copyq, None, self.count))
        lay.addWidget(hbox(review, bulk, export, None))
        lay.addWidget(hbox(self.text, self.genre, self.status, QLabel("BPM"), self.bpm_lo, QLabel("–"), self.bpm_hi))

        self.model = DictTableModel(TRACK_COLS)
        self.model.edited.connect(self.on_edit)
        self.view, self.proxy = make_table(self.model)
        self.view.sortByColumn(0, Qt.AscendingOrder)
        self.view.doubleClicked.connect(self.on_double)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self.menu)
        lay.addWidget(self.view, 1)
        self.reload_playlists()

    # ------------------------------------------------------------ data
    def reload_playlists(self):
        cur = self.playlist_box.currentData()
        self.playlist_box.blockSignals(True)
        self.playlist_box.clear()
        self.playlist_box.addItem("All tracks", None)
        for p in self.svc.db.q("SELECT p.*, (SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id=p.id) n "
                               "FROM playlists p ORDER BY id DESC"):
            self.playlist_box.addItem(f"{p['name']} ({p['n']})", p["id"])
        i = self.playlist_box.findData(cur)
        self.playlist_box.setCurrentIndex(max(i, 0))
        self.playlist_box.blockSignals(False)

    def refresh(self):
        rows = track_rows(self.svc.db, self.svc.settings.min_match_score, self.playlist_box.currentData())
        sel = {r["id"] for r in selected_rows(self.view, self.proxy)}
        self.model.set_rows(rows)
        folders = sorted({r["genre_folder"] or "Unsorted" for r in rows})
        cur = self.genre.currentData()
        self.genre.blockSignals(True)
        self.genre.clear()
        self.genre.addItem("All folders", None)
        for f in folders:
            self.genre.addItem(f, f)
        self.genre.setCurrentIndex(max(0, self.genre.findData(cur)))
        self.genre.blockSignals(False)
        self.apply_filter()
        if sel:
            sm = self.view.selectionModel()
            for i, r in enumerate(rows):
                if r["id"] in sel:
                    idx = self.proxy.mapFromSource(self.model.index(i, 0))
                    if idx.isValid():
                        sm.select(idx, sm.SelectionFlag.Select | sm.SelectionFlag.Rows)

    def apply_filter(self):
        txt = self.text.text().lower().strip()
        g = self.genre.currentData()
        st = self.status.currentData()
        lo, hi = self.bpm_lo.value(), self.bpm_hi.value()

        def pred(r):
            if txt and txt not in f"{r['artists']} {r['title']} {r['album']} {r['label']} {r['genres']}".lower():
                return False
            if g and (r["genre_folder"] or "Unsorted") != g:
                return False
            if st and r["status"] != st:
                return False
            t = r["tempo"] or 0
            if (lo > 0 or hi < 250) and not (lo <= t <= hi):
                return False
            return True

        self.proxy.predicate = pred
        self.proxy.invalidateFilter()
        self.count.setText(f"{self.proxy.rowCount()} of {self.model.rowCount()} tracks")

    # ------------------------------------------------------------ actions
    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Spotify CSV", "", "CSV files (*.csv)")
        if not path:
            return
        try:
            res = self.svc.import_csv(path)
        except Exception as e:
            QMessageBox.critical(self, "Import failed", str(e))
            return
        msg = f"{res.added} tracks added, {res.updated} already known (updated)."
        if res.errors:
            msg += f"\n{len(res.errors)} rows skipped:\n" + "\n".join(res.errors[:10])
        QMessageBox.information(self, "Import complete", msg)
        self.reload_playlists()
        self.playlist_box.setCurrentIndex(self.playlist_box.findData(res.playlist_id))
        self.refresh()
        self.changed.emit()

    def _queue(self, ids, **kw):
        n = self.svc.queue_search(ids, **kw)
        st = self.svc.bridge.status()
        if n and not st.logged_in:
            QMessageBox.information(self, "Searches queued",
                                    f"{n} searches queued. They will start when Nicotine+ is running with the "
                                    f"CrateDigger Bridge plugin enabled and logged in.\n\nCurrent state: {st.message}")
        self.refresh()
        self.changed.emit()

    @needs_fresh_av
    def search_selected(self):
        self._queue([r["id"] for r in selected_rows(self.view, self.proxy)])

    @needs_fresh_av
    def search_new(self):
        ids = [r["id"] for r in self.model.rows if r["status"] == "new"]
        if not ids:
            QMessageBox.information(self, "Nothing to search", "All tracks have been searched already. "
                                    "Use ‘Retry missing/failed’ or select tracks and ‘Search selected’.")
            return
        self._queue(ids)

    @needs_fresh_av
    def retry(self):
        ids = [r["id"] for r in self.model.rows if r["status"] in ("missing", "failed")]
        sel = [r["id"] for r in selected_rows(self.view, self.proxy)]
        ids = sel or ids
        n = self.svc.queue_search(ids, fallback=True)
        rest = [i for i in ids if not self.svc.db.scalar(
            "SELECT COUNT(*) FROM searches WHERE track_id=? AND status IN ('sent','running')", (i,))]
        if rest:  # all generated queries used: repeat the primary query (results change over time)
            n += self.svc.queue_search(rest)
        self.refresh()
        self.changed.emit()

    def cancel(self):
        n = self.svc.cancel_pending()
        self.refresh()
        self.changed.emit()
        QMessageBox.information(self, "Cancelled", f"{n} pending searches cancelled.")

    # ------------------------------------------------------------ review / bulk / export
    def _playlist_id(self):
        pid = self.playlist_box.currentData()
        if pid is None:
            QMessageBox.information(self, "Choose a playlist", "Pick a playlist in the ‘Playlist’ menu first.")
        return pid

    def auto_review(self):
        pid = self._playlist_id()
        if pid is None:
            return
        from ..core.review import review_playlist
        res = review_playlist(self.svc, pid)
        self.refresh()
        self.changed.emit()
        box = QMessageBox(self)
        box.setWindowTitle("Review complete")
        box.setText(f"{res['selected']} tracks: best match selected\n{res['manual']} need a manual look "
                    f"(no match ≥ 85 without warnings)\n{res['missing']} not found\n{res['unchanged']} unchanged "
                    "(already chosen, downloading or in library)\n\nReview page saved for future reference:\n"
                    f"{res['report']}")
        open_btn = box.addButton("Open review page", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            open_path(res["report"])

    @needs_fresh_av
    def bulk_download(self):
        pid = self.playlist_box.currentData()
        rows = selected_rows(self.view, self.proxy)
        if rows:
            tids = [r["id"] for r in rows]
        elif pid is not None:
            tids = [r["id"] for r in self.model.rows]
        else:
            QMessageBox.information(self, "Nothing selected", "Select tracks, or choose a playlist.")
            return
        plan = [(tid, self.svc.best_download_candidate(tid)) for tid in tids]
        plan = [(t, c) for t, c in plan if c is not None and self.svc.db.scalar(
            "SELECT status FROM tracks WHERE id=?", (t,)) not in ("in_library", "downloading", "downloaded")]
        if not plan:
            QMessageBox.information(self, "Nothing to download", "No selected tracks have a safe match ≥ 85 that "
                                    "isn't already downloading or in the library.")
            return
        st = self.svc.bridge.status()
        if not st.logged_in:
            QMessageBox.warning(self, "Nicotine+ not connected", st.message)
            return
        from .results import BulkAuthorizeDialog
        dlg = BulkAuthorizeDialog(self.svc, plan, self)
        if dlg.exec() != QDialog.Accepted:
            return
        res = self.svc.bulk_authorize_download([t for t, _ in plan], dlg.basis.currentText(), dlg.note.text().strip())
        self.refresh()
        self.changed.emit()
        QMessageBox.information(self, "Downloads queued", f"{len(res['queued'])} downloads queued in Nicotine+. "
                                "Each file is virus-scanned and quality-checked automatically when it arrives.")

    def export_playlist(self):
        pid = self._playlist_id()
        if pid is None:
            return
        from ..core.rekordbox import export_playlist
        try:
            res = export_playlist(self.svc.db, self.svc.settings, pid)
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        path = " → ".join(self.svc.settings.rekordbox_folder_path)
        QMessageBox.information(
            self, "Playlist exported",
            f"{res['approved']} of {res['total']} tracks are approved: {res['copied']} copied now, "
            f"{res['already']} already there.\n\nFolder: {res['folder']}\nrekordbox XML: {res['xml']}\n\n"
            f"In rekordbox the playlist appears under rekordbox xml → {path} → (playlist). Tracks you approve later "
            "are added to this folder and the XML automatically.")
        open_path(res["folder"])

    def copy_queries(self):
        rows = selected_rows(self.view, self.proxy) or [self.model.rows[self.proxy.mapToSource(self.proxy.index(i, 0)).row()]
                                                        for i in range(self.proxy.rowCount())]
        QGuiApplication.clipboard().setText("\n".join(r["search_query"] or "" for r in rows))

    def on_edit(self, row, key, value):
        if key == "search_query":
            self.svc.db.exec("UPDATE tracks SET search_query=? WHERE id=?", (value.strip() or None, row["id"]))
            row["search_query"] = value.strip()

    def on_double(self, idx):
        col = self.model.cols[idx.column()]
        if col.editable:
            return
        self.open_track.emit(self.model.rows[self.proxy.mapToSource(idx).row()]["id"])

    def menu(self, pos):
        rows = selected_rows(self.view, self.proxy)
        if not rows:
            return
        m = QMenu(self)
        m.addAction("Show results", lambda: self.open_track.emit(rows[0]["id"]))
        m.addAction("Search now", self.search_selected)
        m.addAction("Search with custom query…", lambda: self.custom_search(rows[0]))
        m.addAction("Copy query", lambda: QGuiApplication.clipboard().setText(rows[0]["search_query"] or ""))
        m.addSeparator()
        fm = m.addMenu("Set library folder")
        for f in [r[0] for r in self.svc.settings.genre_rules] + ["Unsorted"]:
            fm.addAction(f, lambda f=f: self.set_folder(rows, f))
        m.exec(self.view.viewport().mapToGlobal(pos))

    @needs_fresh_av
    def custom_search(self, row):
        q, ok = QInputDialog.getText(self, "Custom search", "Soulseek query:", text=row["search_query"] or "")
        if ok and q.strip():
            self._queue([row["id"]], query=q.strip())

    def set_folder(self, rows, folder):
        for r in rows:
            self.svc.db.exec("UPDATE tracks SET genre_folder=? WHERE id=?", (folder, r["id"]))
        self.refresh()
