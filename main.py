import sys
import ctypes
import json
import os
import calendar
from datetime import datetime

from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu
from PySide6.QtGui import (
    QPainter, QColor, QFont, QBrush,
    QIcon, QPixmap, QPainterPath, QRegion
)
from PySide6.QtCore import Qt, QRectF, QTimer


# basic config / appearance
CONFIG_FILE = "config.json"

COLOR_BG = QColor("#1B1B1D")
COLOR_TEXT = QColor("#FFFFFF")
COLOR_DONE = QColor("#FF5722")
COLOR_FUTURE = QColor("#FFFFFF")


# enable high DPI scaling (Qt sometimes behaves weird without this)
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"

try:
    # improves scaling consistency on Windows
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except:
    pass


# --- desktop embedding helpers ---
# this is based on the WorkerW trick used by wallpaper tools
# undocumented and may break in future Windows versions


def get_workerw():
    # tries to locate WorkerW window (sits behind desktop icons)
    user32 = ctypes.windll.user32
    progman = user32.FindWindowW("Progman", None)

    # undocumented message which forces WorkerW creation
    user32.SendMessageTimeoutW(
        progman, 0x052C, 0, 0, 0, 1000, ctypes.byref(ctypes.c_ulong())
    )

    workerw = None

    def find_workerw(hwnd, _):
        nonlocal workerw
        shell = user32.FindWindowExW(hwnd, 0, "SHELLDLL_DefView", None)
        if shell:
            workerw = user32.FindWindowExW(0, hwnd, "WorkerW", None)
        return True

    enum_windows = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
    user32.EnumWindows(enum_windows(find_workerw), 0)

    return workerw


def set_acrylic(hwnd):
    # enables acrylic blur using undocumented composition attribute
    # works on Win10/11, but Microsoft keeps changing this stuff...

    class ACCENTPOLICY(ctypes.Structure):
        _fields_ = [
            ("AccentState", ctypes.c_int),
            ("AccentFlags", ctypes.c_int),
            ("GradientColor", ctypes.c_uint),
            ("AnimationId", ctypes.c_int),
        ]

    class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
        _fields_ = [
            ("Attribute", ctypes.c_int),
            ("Data", ctypes.POINTER(ACCENTPOLICY)),
            ("SizeOfData", ctypes.c_size_t),
        ]

    accent = ACCENTPOLICY()
    accent.AccentState = 4  # acrylic
    accent.GradientColor = 0x99000000  # slightly transparent black

    data = WINDOWCOMPOSITIONATTRIBDATA()
    data.Attribute = 19  # WCA_ACCENT_POLICY
    data.Data = ctypes.pointer(accent)
    data.SizeOfData = ctypes.sizeof(accent)

    ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))


# main widget


class YearWidget(QWidget):
    def __init__(self):
        super().__init__()

        # UI tuning (these values changed a lot while experimenting)
        self.dot_size = 5
        self.gap = 6
        self.dots_per_row = 28  # 30 looked cramped on smaller screens
        self.padding = 30
        self.text_height = 58

        self.load_settings()

        # grab current date info
        self.refresh_date()

        # calculate layout size
        rows = (self.total_days + self.dots_per_row - 1) // self.dots_per_row

        self.w_width = self.dots_per_row * (self.dot_size + self.gap)
        self.w_height = rows * (self.dot_size + self.gap)

        final_size = max(
            self.w_width + self.padding * 2,
            self.w_height + self.padding * 2 + self.text_height,
        )

        self.setFixedSize(final_size, final_size)

        # frameless + transparent background
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_NoSystemBackground)

        # progress only changes once per day, but 1 minute refresh is safe enough
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_date)
        self.timer.start(60000)

        self.setup_tray()

    def showEvent(self, event):
        # slight delay needed — window handle must exist first
        QTimer.singleShot(100, self.attach_window)
        super().showEvent(event)

    def attach_window(self):
        hwnd = int(self.winId())

        set_acrylic(hwnd)

        workerw = get_workerw()
        if workerw:
            ctypes.windll.user32.SetParent(hwnd, workerw)
            ctypes.windll.user32.ShowWindow(hwnd, 5)

    def resizeEvent(self, event):
        # keeps rounded corners after DPI or scaling changes
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 20, 20)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))
        super().resizeEvent(event)

    def refresh_date(self):
        now = datetime.now()
        year = now.year

        self.day_of_year = now.timetuple().tm_yday
        self.total_days = 366 if calendar.isleap(year) else 365
        self.percent = (self.day_of_year / self.total_days) * 100

        self.update()

    def setup_tray(self):
        # needed because window has no title bar
        pix = QPixmap(64, 64)
        pix.fill(COLOR_DONE)

        self.tray = QSystemTrayIcon(QIcon(pix), self)

        menu = QMenu()
        menu.addAction("Refresh", self.refresh_date)
        menu.addSeparator()
        menu.addAction("Quit", QApplication.quit)

        self.tray.setContextMenu(menu)
        self.tray.show()

    # dragging support (since window is frameless)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.drag_start = (
                e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, e):
        if e.buttons() == Qt.LeftButton:
            pos = e.globalPosition().toPoint() - self.drag_start
            self.move(pos)
            self.save_settings(pos.x(), pos.y())

    # drawing

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(self.rect())

        # clear background manually
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(rect, Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        # widget background
        path = QPainterPath()
        path.addRoundedRect(rect, 20, 20)
        painter.fillPath(path, QBrush(COLOR_BG))

        # grid positioning
        start_x = (self.width() - self.w_width) // 2
        start_y = (self.height() - self.w_height - self.text_height) // 2 - 12

        painter.setPen(Qt.NoPen)

        for i in range(self.total_days):
            row = i // self.dots_per_row
            col = i % self.dots_per_row

            x = start_x + col * (self.dot_size + self.gap)
            y = start_y + row * (self.dot_size + self.gap)

            color = COLOR_DONE if i < self.day_of_year else COLOR_FUTURE
            painter.setBrush(color)
            painter.drawEllipse(QRectF(x, y, self.dot_size, self.dot_size))

        # labels

        painter.setPen(QColor(140, 140, 145))
        painter.setFont(QFont("Segoe UI", 9))
        painter.drawText(
            QRectF(0, self.height() - 55, self.width(), 30),
            Qt.AlignCenter,
            "YEAR PROGRESS",
        )

        painter.setPen(COLOR_TEXT)
        painter.setFont(QFont("Segoe UI", 20, QFont.DemiBold))
        painter.drawText(
            QRectF(0, self.height() - 30, self.width(), 30),
            Qt.AlignCenter,
            f"{100 - self.percent:.1f}%",
        )

    # position persistence

    def load_settings(self):
        if os.path.exists(CONFIG_FILE):
            try:
                data = json.load(open(CONFIG_FILE))
                self.move(data.get("x", 100), data.get("y", 100))
            except:
                pass

    def save_settings(self, x, y):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump({"x": x, "y": y}, f)
        except:
            pass


if __name__ == "__main__":
    app = QApplication(sys.argv)

    if hasattr(Qt, "HighDpiScaleFactorRoundingPolicy"):
        app.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    widget = YearWidget()
    widget.show()

    sys.exit(app.exec())
