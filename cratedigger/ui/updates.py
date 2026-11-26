"""Update prompts: check GitHub Releases in the background and offer to install a newer version."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QProcess, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from .. import __version__
from ..core import updater
from .common import run_bg

log = logging.getLogger(__name__)


class UpdateController(QObject):
    def __init__(self, svc, parent_widget):
        super().__init__(parent_widget)
        self.svc = svc
        self.win = parent_widget
        self._busy = False
        self._worker = None

    def check(self, manual: bool = False):
        """manual=True: always report the outcome; otherwise stay quiet unless there is something to offer."""
        if self._busy:
            return
        self._busy = True
        self._worker = run_bg(updater.check_latest,
                              on_done=lambda rel: self._checked(rel, manual),
                              on_fail=lambda msg: self._check_failed(msg, manual))

    def _check_failed(self, msg, manual):
        self._busy = False
        log.info("update check failed: %s", msg)
        if manual:
            QMessageBox.warning(self.win, "Update check failed",
                                f"Could not reach GitHub to check for updates.\n\n{msg}")

    def _checked(self, rel, manual):
        self._busy = False
        if rel is None:
            log.info("CrateDigger %s is up to date", __version__)
            if manual:
                QMessageBox.information(self.win, "No update", f"CrateDigger {__version__} is the latest version.")
            return
        if not manual and rel.version == self.svc.settings.skipped_update:
            log.info("update %s available but skipped by the user", rel.version)
            return
        self._offer(rel)

    def _offer(self, rel):
        box = QMessageBox(self.win)
        box.setWindowTitle("Update available")
        box.setIcon(QMessageBox.Information)
        box.setTextFormat(Qt.PlainText)
        box.setText(f"CrateDigger {rel.version} is available. You have {__version__}.")
        notes = rel.notes[:1500] + ("…" if len(rel.notes) > 1500 else "")
        box.setInformativeText((notes + "\n\n" if notes else "") +
                               "Your library, playlists and settings are kept.")
        if updater.can_self_update():
            install = box.addButton("Install and restart", QMessageBox.AcceptRole)
        else:
            install = box.addButton("Open download page", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        skip = box.addButton("Skip this version", QMessageBox.DestructiveRole)
        box.setDefaultButton(install)
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip:
            self.svc.settings.skipped_update = rel.version
            self.svc.settings.save()
        elif clicked is install:
            if updater.can_self_update():
                self._install(rel)
            else:
                QDesktopServices.openUrl(QUrl(rel.page_url))

    def _install(self, rel):
        prog = QProgressDialog(f"Installing CrateDigger {rel.version}…", None, 0, 0, self.win)
        prog.setWindowTitle("Updating")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.show()
        self._busy = True

        def done(summary):
            self._busy = False
            prog.close()
            QMessageBox.information(self.win, "Update installed", f"{summary}\n\nCrateDigger will now restart.")
            cmd = updater.relaunch_command()
            if cmd:
                QProcess.startDetached(cmd[0], cmd[1:])
            QApplication.instance().quit()

        def failed(msg):
            self._busy = False
            prog.close()
            QMessageBox.critical(self.win, "Update failed",
                                 f"The update could not be installed. Your current version still works.\n\n{msg}")

        self._worker = run_bg(updater.install, rel, on_done=done, on_fail=failed)
