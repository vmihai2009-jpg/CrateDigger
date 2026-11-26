from __future__ import annotations

import logging
import sys

from . import __version__
from .core import config
from .core.bridge import NicotineBridge
from .core.db import Database
from .core.logging_setup import setup_logging
from .core.service import Service

log = logging.getLogger("cratedigger")


def build_service() -> Service:
    config.ensure_dirs()
    settings = config.Settings.load()
    if not config.SETTINGS_PATH.exists():
        settings.save()
    return Service(Database(config.DB_PATH), settings, NicotineBridge())


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    setup_logging()
    log.info("CrateDigger %s starting (Python %s)", __version__, sys.version.split()[0])
    svc = build_service()

    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow
    from .ui.theme import QSS

    app = QApplication(argv)
    app.setApplicationName("CrateDigger")
    app.setOrganizationName("CrateDigger")
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    icon = config.Path(__file__).resolve().parent / "resources" / "icon.png"
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    def _excepthook(et, ev, tb):  # uncaught errors in UI actions: log them and tell the user
        import traceback
        log.error("Unhandled error: %s", "".join(traceback.format_exception(et, ev, tb)))
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(None, "CrateDigger error", f"{et.__name__}: {ev}\n\nDetails are in the Activity log.")

    sys.excepthook = _excepthook
    win = MainWindow(svc)
    win.show()
    if "--screenshot" in argv:  # used by the test script: render, save, quit
        from PySide6.QtCore import QTimer
        out = argv[argv.index("--screenshot") + 1]
        page = argv[argv.index("--page") + 1] if "--page" in argv else "dashboard"

        def shoot():
            win.goto(page)
            if page == "results":
                tid = svc.db.scalar("SELECT track_id FROM candidates ORDER BY score DESC LIMIT 1")
                if tid:
                    win.open_track(tid)
                    win.results.cview.selectRow(0)
            QTimer.singleShot(700, lambda: (win.grab().save(out), app.quit()))

        QTimer.singleShot(1200, shoot)
    rc = app.exec()
    log.info("CrateDigger exiting")
    return rc
