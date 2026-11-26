from __future__ import annotations

import html
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QInputDialog, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton, QSplitter, QTextBrowser, QVBoxLayout,
                               QWidget)

from ..core.db import loads
from ..core.library import LibraryError
from ..core.service import AUTH_BASES
from ..core.text import path_parts
from . import theme
from .common import (Col, DictTableModel, fmt_dur, fmt_size, hbox, make_table, needs_fresh_av, open_path, reveal_in_finder, run_bg,
                     score_color, selected_rows)
from .playlist import track_rows

ATTENTION = ("review", "selected", "downloading", "downloaded", "missing", "failed", "searching", "queued")


def _quality(r):
    return (loads(r["score_detail"], {}) or {}).get("quality") or (r["ext"] or "").upper()


def _kbps(r):
    if r["bitrate"]:
        return r["bitrate"]
    d = loads(r["score_detail"], {}) or {}
    return d.get("implied_kbps")


def _dur(r):
    d = (loads(r["score_detail"], {}) or {}).get("duration_diff_s")
    base = fmt_dur(r["duration_s"])
    return f"{base} ({d:+.0f}s)" if d is not None and abs(d) >= 1 else base


def _state(r):
    if r["state"] in ("queued_download", "downloading") and r["dl_status"]:
        pct = f" {r['dl_percent']}%" if r["dl_percent"] and r["dl_status"] == "Transferring" else ""
        return f"{r['dl_status']}{pct}"
    if r["state"] == "failed" and r["dl_status"]:
        return f"failed: {r['dl_status']}"
    return r["state"]


STATE_COLORS = {"selected": "#C99BF2", "rejected": theme.MUTED, "authorized": theme.INFO, "queued_download": theme.INFO,
                "downloading": theme.INFO, "downloaded": theme.OK, "failed": theme.BAD}

CAND_COLS = [
    Col("Score", "score", align="right", width=46, color=lambda r: score_color(r["score"])),
    Col("Src", "source", fmt=lambda r: "SLSK" if r["source"] == "soulseek" else "LOCAL", width=46),
    Col("Version", "version", width=120),
    Col("Quality", "quality_tier", fmt=_quality, sort=lambda r: r["quality_tier"] or 99, width=110),
    Col("kbps", "bitrate", fmt=lambda r: _kbps(r) or "", sort=lambda r: _kbps(r) or 0, align="right", width=48,
        tip=lambda r: "reported by the sharer" if r["bitrate"] else "estimated from size ÷ duration"),
    Col("Rate", "sample_rate", fmt=lambda r: (f"{r['sample_rate'] / 1000:g}k" if r["sample_rate"] else "")
        + (f"/{r['bit_depth']}" if r["bit_depth"] else ""), width=58),
    Col("Length", "duration_s", fmt=_dur, sort=lambda r: r["duration_s"] or 0, width=86, align="right"),
    Col("Size", "size", fmt=lambda r: fmt_size(r["size"]), sort=lambda r: r["size"] or 0, width=66, align="right"),
    Col("User", "username", width=100),
    Col("Slot", "free_slot", fmt=lambda r: ("free" if r["free_slot"] else "busy") if r["source"] == "soulseek" else "",
        color=lambda r: theme.OK if r["free_slot"] else theme.MUTED, width=40),
    Col("Queue", "queue_len", align="right", width=46),
    Col("State", "state", fmt=_state, color=lambda r: STATE_COLORS.get(r["state"]), width=110),
    Col("Flags", "flags", fmt=lambda r: "; ".join(loads(r["flags"], []) or []),
        color=lambda r: theme.WARN if loads(r["flags"], []) else None,
        tip=lambda r: "\n".join(loads(r["flags"], []) or []) or None, width=220),
    Col("File", "path", fmt=lambda r: "/".join(r["path"].replace("\\", "/").split("/")[-2:]),
        tip=lambda r: r["path"], width=320),
]

LEFT_COLS = [
    Col("Status", "status", width=74, color=lambda r: theme.STATUS_COLORS.get(r["status"]),
        fmt=lambda r: r["status"].replace("_", " ")),
    Col("Best", "best_score", align="right", width=36, color=lambda r: score_color(r["best_score"]),
        sort=lambda r: r["best_score"] if r["best_score"] is not None else -1),
    Col("Track", "title", fmt=lambda r: f"{r['artists'].replace(';', ', ')} — {r['title']}", width=260),
]


class AuthorizeDialog(QDialog):
    def __init__(self, cand, track, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Authorise download")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        info = QLabel(
            f"<b>{html.escape(track['artists'].replace(';', ', '))} — {html.escape(track['title'])}</b><br>"
            f"<span style='color:{theme.MUTED}'>{html.escape(cand['path'])}<br>shared by "
            f"{html.escape(cand['username'] or '')}</span><br><br>"
            "Discovery and download are separate steps. CrateDigger only queues a download after you record "
            "<b>why you are permitted</b> to obtain this recording (public domain, Creative Commons, explicit "
            "permission from the rights holder or uploader, …). The basis is stored with the file.")
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        lay.addWidget(info)
        form = QFormLayout()
        self.basis = QComboBox()
        self.basis.addItems(AUTH_BASES)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Licence URL, permission reference, receipt… (recommended)")
        form.addRow("Basis", self.basis)
        form.addRow("Evidence / note", self.note)
        lay.addLayout(form)
        self.confirm = QCheckBox("I confirm I have permission to obtain this recording")
        lay.addWidget(self.confirm)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.ok = bb.addButton("Authorise & queue in Nicotine+", QDialogButtonBox.AcceptRole)
        self.ok.setObjectName("primary")
        self.ok.setEnabled(False)
        self.confirm.toggled.connect(self.ok.setEnabled)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class BulkAuthorizeDialog(QDialog):
    """One authorisation, recorded per file, for several downloads."""

    def __init__(self, svc, plan, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Authorise downloads")
        self.setMinimumWidth(640)
        lay = QVBoxLayout(self)
        items = []
        for tid, c in plan[:40]:
            t = svc.db.one("SELECT artists, title FROM tracks WHERE id=?", (tid,))
            items.append(f"<tr><td>{html.escape(t['artists'].replace(';', ', '))} — {html.escape(t['title'])}</td>"
                         f"<td style='color:{theme.MUTED}'>{html.escape(_quality(c))} · {c['score']} · "
                         f"{html.escape(c['username'] or '')}</td></tr>")
        more = f"<br>…and {len(plan) - 40} more" if len(plan) > 40 else ""
        info = QLabel(
            f"<b>{len(plan)} downloads</b> (best match per track)<table>{''.join(items)}</table>{more}<br>"
            "Discovery and download are separate steps. Record <b>why you are permitted</b> to obtain these "
            "recordings. The basis is stored with every file.")
        info.setWordWrap(True)
        info.setTextFormat(Qt.RichText)
        lay.addWidget(info)
        form = QFormLayout()
        self.basis = QComboBox()
        self.basis.addItems(AUTH_BASES)
        self.note = QLineEdit()
        self.note.setPlaceholderText("Licence URL, permission reference, receipt… (recommended)")
        form.addRow("Basis", self.basis)
        form.addRow("Evidence / note", self.note)
        lay.addLayout(form)
        self.confirm = QCheckBox(f"I confirm I have permission to obtain all {len(plan)} recordings listed")
        lay.addWidget(self.confirm)
        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        ok = bb.addButton(f"Authorise && queue {len(plan)} in Nicotine+", QDialogButtonBox.AcceptRole)
        ok.setObjectName("primary")
        ok.setEnabled(False)
        self.confirm.toggled.connect(ok.setEnabled)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class ResultsPage(QWidget):
    changed = Signal()

    def __init__(self, svc, parent=None):
        super().__init__(parent)
        self.svc = svc
        self.track_id: int | None = None
        self.workers = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 12)
        lay.setSpacing(8)
        title = QLabel("Search results")
        title.setObjectName("h1")
        self.scope = QComboBox()
        self.scope.addItem("Needs attention", "attention")
        self.scope.addItem("All tracks", "all")
        self.scope.addItem("In library", "in_library")
        self.scope.currentIndexChanged.connect(self.refresh)
        lay.addWidget(hbox(title, None, QLabel("Show"), self.scope))

        split = QSplitter(Qt.Horizontal)
        # left: tracks
        self.tmodel = DictTableModel(LEFT_COLS)
        self.tview, self.tproxy = make_table(self.tmodel, multi=False)
        self.tview.selectionModel().selectionChanged.connect(self._track_changed)
        split.addWidget(self.tview)

        # right: candidates
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(8, 0, 0, 0)
        rl.setSpacing(6)
        self.header = QLabel("Select a track")
        self.header.setTextFormat(Qt.RichText)
        self.header.setWordWrap(True)
        rl.addWidget(self.header)

        self.b_select = QPushButton("Select")
        self.b_select.setToolTip("Mark as your preferred candidate")
        self.b_reject = QPushButton("Reject")
        self.b_dl = QPushButton("Authorise && download…")
        self.b_analyze = QPushButton("Analyse file")
        self.b_approve = QPushButton("Approve → library")
        self.b_approve.setObjectName("primary")
        self.b_research = QPushButton("Search again…")
        self.b_spotify = QPushButton("▶ Spotify")
        self.b_spotify.setToolTip("Hear the reference version on Spotify")
        self.b_spotify.clicked.connect(self.preview_spotify)
        self.b_play = QPushButton("▶ Play file")
        self.b_play.setToolTip("Quick Look preview of the downloaded/local file")
        self.b_play.clicked.connect(self.preview_file)
        self.show_hidden = QCheckBox("Show low scores && duplicates")
        self.show_hidden.toggled.connect(self.load_candidates)
        self.b_select.clicked.connect(lambda: self._set_state("selected"))
        self.b_reject.clicked.connect(lambda: self._set_state("rejected"))
        self.b_dl.clicked.connect(lambda: self.download())
        self.b_analyze.clicked.connect(self.analyze)
        self.b_approve.clicked.connect(self.approve)
        self.b_research.clicked.connect(lambda: self.research())
        rl.addWidget(hbox(self.b_spotify, self.b_play, self.b_select, self.b_reject, self.b_dl, self.b_analyze,
                          self.b_approve, self.b_research,
                          None, self.show_hidden))

        self.cmodel = DictTableModel(CAND_COLS)
        self.cview, self.cproxy = make_table(self.cmodel)
        self.cview.selectionModel().selectionChanged.connect(self._cand_changed)
        self.cview.doubleClicked.connect(lambda _: self._set_state("selected"))
        self.cview.setContextMenuPolicy(Qt.CustomContextMenu)
        self.cview.customContextMenuRequested.connect(self.menu)
        csplit = QSplitter(Qt.Vertical)
        csplit.addWidget(self.cview)
        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        csplit.addWidget(self.detail)
        csplit.setSizes([420, 170])
        rl.addWidget(csplit, 1)
        split.addWidget(right)
        split.setSizes([330, 1000])
        lay.addWidget(split, 1)
        self._update_buttons()

    # ------------------------------------------------------------ loading
    def refresh(self):
        rows = track_rows(self.svc.db, self.svc.settings.min_match_score)
        scope = self.scope.currentData()
        if scope == "attention":
            rows = [r for r in rows if r["status"] in ATTENTION]
        elif scope == "in_library":
            rows = [r for r in rows if r["status"] == "in_library"]
        self.tmodel.set_rows(rows)
        self._reselect_track()
        self.load_candidates()

    def show_track(self, track_id: int):
        self.track_id = track_id
        if not any(r["id"] == track_id for r in self.tmodel.rows):
            self.scope.setCurrentIndex(1)
        self.refresh()

    def _reselect_track(self):
        if self.track_id is None:
            return
        for i, r in enumerate(self.tmodel.rows):
            if r["id"] == self.track_id:
                idx = self.tproxy.mapFromSource(self.tmodel.index(i, 0))
                self.tview.blockSignals(True)
                self.tview.selectRow(idx.row())
                self.tview.blockSignals(False)
                self.tview.scrollTo(idx)
                return

    def _track_changed(self, *_):
        rows = selected_rows(self.tview, self.tproxy)
        if rows:
            self.track_id = rows[0]["id"]
            self.load_candidates()

    def load_candidates(self):
        sel = {r["id"] for r in selected_rows(self.cview, self.cproxy)}
        if self.track_id is None:
            self.cmodel.set_rows([])
            return
        t = self.svc.db.one("SELECT * FROM tracks WHERE id=?", (self.track_id,))
        if t is None:
            return
        rows = [dict(r) for r in self.svc.candidates_for(self.track_id, self.show_hidden.isChecked())]
        hidden = (self.svc.db.scalar("SELECT COUNT(*) FROM candidates WHERE track_id=?", (self.track_id,)) or 0) - len(rows)
        searches = self.svc.db.q("SELECT * FROM searches WHERE track_id=? ORDER BY id", (self.track_id,))
        srch = ", ".join(f"“{html.escape(s['query'])}” {s['status']} ({s['result_count']})" for s in searches) or "not searched"
        self.header.setText(
            f"<span style='font-size:15px;font-weight:600'>{html.escape(t['artists'].replace(';', ', '))} — "
            f"{html.escape(t['title'])}</span><br><span style='color:{theme.MUTED}'>"
            f"{html.escape(t['album'] or '')} · {fmt_dur((t['duration_ms'] or 0) / 1000)} · ISRC {t['isrc'] or '—'} · "
            f"{t['tempo'] or 0:.0f} BPM · {html.escape(t['label'] or '')} · folder: {html.escape(t['genre_folder'] or '')}"
            f"<br>Searches: {srch}" + (f" · {hidden} hidden (low score / duplicates)" if hidden > 0 else "") + "</span>")
        self.cmodel.set_rows(rows)
        self.cview.sortByColumn(-1, Qt.AscendingOrder)
        if sel:
            for i, r in enumerate(rows):
                if r["id"] in sel:
                    self.cview.selectRow(self.cproxy.mapFromSource(self.cmodel.index(i, 0)).row())
        self._cand_changed()

    def _cand(self):
        rows = selected_rows(self.cview, self.cproxy)
        return rows[0] if rows else None

    def _cand_changed(self, *_):
        self._update_buttons()
        c = self._cand()
        if not c:
            self.detail.setHtml(f"<span style='color:{theme.MUTED}'>Select a candidate to see the match breakdown.</span>")
            return
        d = loads(c["score_detail"], {}) or {}
        flags = loads(c["flags"], []) or []
        lines = [f"<b>Match {c['score']}</b> — artist {d.get('artist', '?')}, title {d.get('title', '?')}, "
                 f"duration {d.get('duration', '?')}, version penalty −{d.get('version_penalty', 0)}"
                 + (", album ✓" if d.get("album") else "") + (f", ISRC {d['isrc']}" if d.get("isrc") else "")]
        lines.append(f"<span style='color:{theme.MUTED}'>{html.escape(c['path'])}</span>")
        if c["source"] == "soulseek":
            lines.append(f"Reported by sharer: {_quality(c)}, {_kbps(c) or '?'} kbps"
                         f"{' (estimated from size)' if not c['bitrate'] else ''}, "
                         f"{fmt_dur(c['duration_s']) or 'no length'}, {fmt_size(c['size'])}. "
                         "<i>Not verified until downloaded and analysed.</i>")
        for f in flags:
            lines.append(f"<span style='color:{theme.WARN}'>⚠ {html.escape(f)}</span>")
        lf = self.svc.local_file_for_candidate(c["id"])
        if lf:
            col = {"ok": theme.OK, "warn": theme.WARN, "bad": theme.BAD}[lf["verdict"] or "ok"]
            lines.append(
                f"<br><b>Analysed file</b> <span style='color:{col}'>● {lf['verdict'].upper()}</span>: "
                f"codec {lf['codec']}, {lf['bitrate'] or '?'} kbps avg, {lf['sample_rate'] or '?'} Hz"
                f"{', ' + str(lf['bit_depth']) + '-bit' if lf['bit_depth'] else ''}, {lf['channels']} ch, "
                f"{fmt_dur(lf['duration_s'])}, spectrum to {(lf['cutoff_hz'] or 0) / 1000:.1f} kHz. "
                f"Tags: {html.escape(lf['tag_artist'] or '—')} / {html.escape(lf['tag_title'] or '—')}"
                f"{' / ISRC ' + lf['tag_isrc'] if lf['tag_isrc'] else ''}")
            for f in loads(lf["flags"], []) or []:
                lines.append(f"<span style='color:{col}'>• {html.escape(f)}</span>")
            av = loads(lf["av_detail"], {}) or {}
            avs = lf["av_status"] or "unscanned"
            avc = theme.OK if avs == "clean" else theme.BAD if avs in ("infected", "suspicious") else theme.WARN
            lines.append(f"<b>Virus/malware scan</b> <span style='color:{avc}'>● {avs.upper()}</span>"
                         + (f" — {html.escape(av.get('engine') or '')}" if av.get("engine") else ""))
            for x in (av.get("threats") or []) + (av.get("notes") or []):
                lines.append(f"<span style='color:{avc}'>• {html.escape(x)}</span>")
            if avs != "clean":
                lines.append(f"<span style='color:{theme.BAD}'>This file cannot be approved into the library "
                             "until it scans clean.</span>")
        auth = self.svc.authorization_for(c["id"])
        if auth:
            lines.append(f"<span style='color:{theme.INFO}'>Authorised: {html.escape(auth['basis'])}"
                         f"{' — ' + html.escape(auth['note']) if auth['note'] else ''} ({auth['created_at']} UTC)</span>")
        self.detail.setHtml("<br>".join(lines))

    def _update_buttons(self):
        c = self._cand()
        has = c is not None
        local = has and bool(c["local_path"]) and Path(c["local_path"]).exists()
        self.b_select.setEnabled(has)
        self.b_reject.setEnabled(has)
        self.b_dl.setEnabled(has and c["source"] == "soulseek" and c["state"] not in
                             ("queued_download", "downloading", "downloaded"))
        self.b_analyze.setEnabled(local)
        self.b_approve.setEnabled(local)
        self.b_research.setEnabled(self.track_id is not None)
        self.b_spotify.setEnabled(self.track_id is not None)
        self.b_play.setEnabled(local)

    # ------------------------------------------------------------ actions
    def _set_state(self, state):
        for c in selected_rows(self.cview, self.cproxy):
            self.svc.set_candidate_state(c["id"], state)
        self.load_candidates()
        self.changed.emit()

    @needs_fresh_av
    def download(self):
        c = self._cand()
        if not c:
            return
        t = self.svc.db.one("SELECT * FROM tracks WHERE id=?", (c["track_id"],))
        st = self.svc.bridge.status()
        if not st.logged_in:
            QMessageBox.warning(self, "Nicotine+ not connected", st.message)
            return
        dlg = AuthorizeDialog(c, t, self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.svc.authorize(c["id"], dlg.basis.currentText(), dlg.note.text().strip())
        try:
            self.svc.request_download(c["id"])
        except Exception as e:
            QMessageBox.critical(self, "Download not queued", str(e))
        self.load_candidates()
        self.changed.emit()

    def analyze(self):
        c = self._cand()
        if not c:
            return
        self.b_analyze.setEnabled(False)
        self.b_analyze.setText("Analysing…")

        def done(_):
            self.b_analyze.setText("Analyse file")
            self.load_candidates()
            self.changed.emit()

        def fail(msg):
            self.b_analyze.setText("Analyse file")
            QMessageBox.critical(self, "Analysis failed", msg)
            self._update_buttons()

        self.workers.append(run_bg(self.svc.analyze_candidate, c["id"], on_done=done, on_fail=fail))

    def approve(self):
        c = self._cand()
        if not c:
            return
        lf = self.svc.local_file_for_candidate(c["id"])
        if lf is None:
            QMessageBox.information(self, "Analyse first", "Run ‘Analyse file’ first so the real format and quality "
                                    "are verified before the file enters your library.")
            return
        if lf["av_status"] in ("infected", "suspicious"):
            QMessageBox.critical(self, "Blocked by security scan",
                                 "This file failed the virus/malware scan and cannot be added to the library:\n\n"
                                 + "\n".join("• " + f for f in loads(lf["flags"], []) or []))
            return
        if lf["verdict"] != "ok":
            flags = "\n".join("• " + f for f in loads(lf["flags"], []) or [])
            if QMessageBox.question(self, "Quality warnings", f"This file has warnings:\n\n{flags}\n\nApprove anyway?") \
                    != QMessageBox.Yes:
                return
        if c["score"] is not None and c["score"] < 85:
            if QMessageBox.question(self, "Uncertain match",
                                    f"Match score is only {c['score']}. Are you sure this is the right recording?") \
                    != QMessageBox.Yes:
                return
        try:
            lid = self.svc.approve(lf["id"])
        except LibraryError as e:
            if "already in library" in str(e) and QMessageBox.question(
                    self, "Duplicate", f"{e}\n\nAdd another copy anyway?") == QMessageBox.Yes:
                lid = self.svc.approve(lf["id"], allow_duplicate=True)
            else:
                if "already in library" not in str(e):
                    QMessageBox.critical(self, "Not approved", str(e))
                return
        row = self.svc.db.one("SELECT library_path FROM library WHERE id=?", (lid,))
        self.header.setText(self.header.text() + f"<br><span style='color:{theme.OK}'>✓ Copied to "
                            f"{html.escape(row['library_path'])}</span>")
        self.changed.emit()
        self.refresh()

    @needs_fresh_av
    def research(self):
        if self.track_id is None:
            return
        t = self.svc.db.one("SELECT * FROM tracks WHERE id=?", (self.track_id,))
        opts = self.svc.queries_for(t)
        q, ok = QInputDialog.getItem(self, "Search again", "Query (editable):", opts, 0, True)
        if ok and q.strip():
            self.svc.queue_search([self.track_id], query=q.strip())
            self.changed.emit()
            self.load_candidates()

    def preview_spotify(self):
        from ..core.review import spotify_url
        t = self.svc.db.one("SELECT spotify_uri FROM tracks WHERE id=?", (self.track_id,))
        if t and t["spotify_uri"]:
            # Opens the Spotify app if installed, otherwise the web player
            subprocess.Popen(["open", t["spotify_uri"]]) if Path("/Applications/Spotify.app").exists() \
                else open_path(spotify_url(t["spotify_uri"]))

    def preview_file(self):
        c = self._cand()
        if c and c["local_path"] and Path(c["local_path"]).exists():
            subprocess.Popen(["qlmanage", "-p", c["local_path"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def menu(self, pos):
        c = self._cand()
        if not c:
            return
        m = QMenu(self)
        m.addAction("Select", lambda: self._set_state("selected"))
        m.addAction("Reject", lambda: self._set_state("rejected"))
        m.addAction("Reset", lambda: self._set_state("new"))
        m.addSeparator()
        m.addAction("Copy path", lambda: QGuiApplication.clipboard().setText(c["path"]))
        folder, stem, _ = path_parts(c["path"])
        m.addAction("Copy filename as query", lambda: QGuiApplication.clipboard().setText(stem))
        if c["local_path"]:
            m.addAction("Reveal in Finder", lambda: reveal_in_finder(c["local_path"]))
        m.exec(self.cview.viewport().mapToGlobal(pos))
