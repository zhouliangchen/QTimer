"""Native Qt settings components and a shared visual system."""
from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QSizePolicy, QWidget

SETTINGS_STYLE = """
QDialog#settingsDialog { background:#f3f6fb; color:#172b49; }
QWidget { font-family:'Microsoft YaHei'; font-size:13px; color:#243752; }
QWidget#settingsSidebar { background:#edf2f9; border-right:1px solid #dfe6f0; }
QLabel { background:transparent; border:none; }
QLabel#brandName { font-size:18px; font-weight:700; color:#142945; }
QLabel#pageTitle { font-size:25px; font-weight:700; color:#142945; }
QLabel#pageDescription { color:#718097; font-size:12px; }
QLabel#cardTitle { font-size:15px; font-weight:700; color:#172b49; }
QLabel#rowTitle { font-weight:600; color:#263b58; }
QLabel#rowDescription, QLabel#settingsHint { color:#7b899e; font-size:11px; }
QLabel#settingsStatus { color:#60748f; font-size:12px; }
QListWidget { background:transparent; border:none; outline:none; padding:4px 0; }
QListWidget::item { height:44px; margin:3px 0; padding-left:16px;
                    color:#62738b; border-radius:9px; }
QListWidget::item:hover { background:#e4ebf6; color:#2353a7; }
QListWidget::item:selected { background:#dae7ff; color:#155ad2; font-weight:700; }
QFrame#settingsCard { background:#ffffff; border:1px solid #e1e7f0; border-radius:13px; }
QWidget#cardBody, QWidget#scrollWidget { background:transparent; border:none; }
QWidget#stageRow, QWidget#alertRow { background:#f8faff; border:1px solid #e5ebf4; border-radius:9px; }
QWidget#stageRow:hover, QWidget#alertRow:hover { border-color:#c6d7f4; }
QLineEdit, QSpinBox, QDoubleSpinBox, QKeySequenceEdit, QComboBox {
    background:#ffffff; color:#263b58; border:1px solid #dbe3ef;
    border-radius:7px; min-height:28px; padding:3px 8px;
    selection-background-color:#2869e8; selection-color:white;
}
QLineEdit:focus, QSpinBox:focus, QKeySequenceEdit:focus, QComboBox:focus { border-color:#3978ef; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { color:#a1adbd; background:#f1f4f8; }
QLineEdit:read-only { color:#71839a; background:#f8faff; }
QComboBox::drop-down { border:none; width:23px; }
QComboBox QAbstractItemView { background:white; color:#263b58; border:1px solid #dbe3ef;
    selection-background-color:#e4edff; selection-color:#155ad2; padding:4px; }
QPushButton { background:#ffffff; color:#415673; border:1px solid #dbe3ef;
    border-radius:7px; padding:7px 13px; min-height:20px; }
QPushButton:hover { background:#f0f5ff; border-color:#adc6ef; color:#175bd5; }
QPushButton:pressed { background:#e2ecff; }
QPushButton:disabled { color:#a6b1c0; border-color:#e7ecf3; }
QPushButton[role="primary"] { background:#2467ed; color:white; border-color:#2467ed; font-weight:600; }
QPushButton[role="primary"]:hover { background:#1457d9; border-color:#1457d9; }
QPushButton[role="quiet"] { border-color:transparent; background:transparent; color:#73849a; }
QPushButton[role="quiet"]:hover { background:#e7eef9; color:#245fbc; }
QCheckBox { color:#536781; spacing:7px; background:transparent; }
QCheckBox::indicator { width:15px; height:15px; }
QGroupBox { color:#627895; font-size:11px; border:1px solid #e1e8f2; border-radius:7px;
    margin-top:10px; padding-top:14px; }
QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 5px; }
QScrollArea { background:transparent; border:none; }
QScrollArea > QWidget > QWidget { background:transparent; }
QScrollBar:vertical { background:transparent; width:7px; border:none; margin:2px 0; }
QScrollBar::handle:vertical { background:#c9d4e4; border-radius:3px; min-height:35px; }
QScrollBar::handle:vertical:hover { background:#a8bad3; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:none; }
QSlider:horizontal { min-height:24px; }
QSlider::groove:horizontal { height:5px; border-radius:2px; background:#e4eaf3; }
QSlider::sub-page:horizontal { background:#3776ed; border-radius:2px; }
QSlider::handle:horizontal { background:white; border:2px solid #3776ed; width:13px;
    height:13px; margin:-6px 0; border-radius:8px; }
QSlider::handle:horizontal:hover { border-color:#1556c9; }
QToolTip { background:#253b5b; color:white; border:none; padding:6px; }
"""

class ToggleSwitch(QCheckBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._progress = 0.0
        self._animation = QPropertyAnimation(self, b'progress', self)
        self._animation.setDuration(150)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, checked):
        self._animation.stop()
        self._animation.setStartValue(self._progress)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()

    def _set_progress(self, value):
        self._progress = value
        self.update()

    progress = Property(float, lambda self: self._progress, _set_progress)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1.0 if self.isEnabled() else 0.45)
        painter.setPen(Qt.PenStyle.NoPen)
        off, on = QColor('#cbd6e5'), QColor('#2467ed')
        color = QColor.fromRgbF(*[off.getRgbF()[i] * (1-self._progress) + on.getRgbF()[i] * self._progress for i in range(3)])
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(2, 3, 40, 20), 10, 10)
        painter.setBrush(QColor('white'))
        painter.drawEllipse(QRectF(5 + 20*self._progress, 6, 14, 14))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor('#93b7ff'), 1.5))
            painter.drawRoundedRect(QRectF(0.8, 0.8, 42.4, 24.4), 12, 12)


class OptionCheckBox(QCheckBox):
    """Small labeled option with a visible unchecked state on light surfaces."""
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 27, 24)

    def minimumSizeHint(self):
        return self.sizeHint()

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(1.0 if self.isEnabled() else 0.45)
        top = (self.height()-15)/2
        box = QRectF(1, top, 15, 15)
        painter.setBrush(QColor('#2467ed') if self.isChecked() else QColor('white'))
        painter.setPen(QPen(QColor('#2467ed' if self.isChecked() else '#a8bbd5'), 1))
        painter.drawRoundedRect(box, 3, 3)
        if self.isChecked():
            painter.setPen(QPen(QColor('white'), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(4, int(top+8), 7, int(top+11))
            painter.drawLine(7, int(top+11), 13, int(top+4))
        painter.setPen(QColor('#536781'))
        painter.setFont(self.font())
        painter.drawText(QRectF(24, 0, self.width()-24, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.hasFocus():
            painter.setPen(QPen(QColor('#8ab0f5'), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(box.adjusted(-1,-1,1,1), 4, 4)


class AppearancePreview(QWidget):
    """A local material preview, independent of top-level window opacity."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.config = None
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_config(self, config):
        self.config = config
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#f0f4fa'))
        painter.drawRoundedRect(self.rect(), 9, 9)
        painter.setPen(QPen(QColor('#d8e1ef'), 1))
        for x in range(18, self.width(), 22):
            for y in range(16, self.height(), 22):
                painter.drawPoint(x, y)
        if self.config is None:
            return
        cfg = self.config
        time_font = QFont(cfg.font)
        time_font.setPixelSize(min(42, cfg.font_size))
        time_font.setBold(True)
        stage_font = QFont(cfg.stage_font)
        stage_font.setPixelSize(min(26, cfg.stage_font_size))
        name = cfg.stages[0].label.strip() if cfg.stages else ''
        if not cfg.show_stage_label:
            name = ''
        stage_width = QFontMetrics(stage_font).horizontalAdvance(name) + 18 if name else 0
        time_width = QFontMetrics(time_font).horizontalAdvance('05:00')
        width = min(self.width()-40, stage_width + time_width + 44)
        box = QRectF((self.width()-width)/2, (self.height()-68)/2, width, 68)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(cfg.bg_qcolor())
        painter.drawRoundedRect(box, 13, 13)
        foreground = QColor(cfg.color)
        foreground.setAlphaF(max(0, min(1, (100-cfg.font_transparency)/100)))
        painter.setPen(foreground)
        if name:
            painter.setFont(stage_font)
            painter.drawText(QRectF(box.left()+20, box.top(), stage_width, box.height()),
                             Qt.AlignmentFlag.AlignVCenter, QFontMetrics(stage_font).elidedText(name, Qt.TextElideMode.ElideRight, max(0,int(width-time_width-55))))
        painter.setFont(time_font)
        painter.drawText(QRectF(box.right()-time_width-22, box.top(), time_width+2, box.height()),
                         Qt.AlignmentFlag.AlignCenter, '05:00')
