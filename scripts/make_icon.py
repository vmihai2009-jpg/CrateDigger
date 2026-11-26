"""Draws the app icon (a record in a crate) with Qt; no external assets."""
import sys
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

app = QGuiApplication(sys.argv[:1] + ["-platform", "offscreen"])
S = 1024
img = QImage(S, S, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
p.setBrush(QColor("#151617"))
p.setPen(Qt.NoPen)
p.drawRoundedRect(QRectF(80, 80, 864, 864), 190, 190)
# record
p.setBrush(QColor("#0B0C0C"))
p.drawEllipse(QRectF(230, 150, 564, 564))
p.setPen(QPen(QColor("#2A2D30"), 6))
p.setBrush(Qt.NoBrush)
for r in (240, 205, 170):
    p.drawEllipse(QRectF(512 - r, 432 - r, 2 * r, 2 * r))
p.setPen(Qt.NoPen)
p.setBrush(QColor("#B5F23D"))
p.drawEllipse(QRectF(512 - 85, 432 - 85, 170, 170))
p.setBrush(QColor("#151617"))
p.drawEllipse(QRectF(512 - 16, 432 - 16, 32, 32))
# crate front
p.setBrush(QColor("#B5F23D"))
p.drawRoundedRect(QRectF(190, 600, 644, 250), 40, 40)
p.setBrush(QColor("#151617"))
for x in (300, 470, 640):
    p.drawRoundedRect(QRectF(x, 660, 90, 34), 17, 17)
p.end()
img.save(sys.argv[1])
