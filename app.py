from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fitz
from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QAction, QColor, QImage, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)


@dataclass
class MatchResult:
    keyword: str
    value: str
    page_index: int
    keyword_rect: fitz.Rect
    value_rect: fitz.Rect
    confidence: str


@dataclass
class PdfJob:
    path: Path
    status: str = "PENDING"
    proposed_name: str = ""
    results: Optional[list[Optional[MatchResult]]] = None
    error: str = ""


class PdfCanvas(QWidget):
    pageClicked = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.pixmap: Optional[QPixmap] = None
        self.page_w = 1.0
        self.page_h = 1.0
        self.keyword_rect: Optional[fitz.Rect] = None
        self.value_rect: Optional[fitz.Rect] = None
        self.rotation = 0
        self.setMinimumSize(500, 600)

    def set_page(self, pixmap, page_w, page_h, keyword_rect=None, value_rect=None, rotation=0):
        self.pixmap = pixmap
        self.page_w = max(float(page_w), 1.0)
        self.page_h = max(float(page_h), 1.0)
        self.keyword_rect = keyword_rect
        self.value_rect = value_rect
        self.rotation = rotation % 360
        self.update()

    def clear(self):
        self.pixmap = None
        self.keyword_rect = None
        self.value_rect = None
        self.update()

    def rotated_page_size(self):
        if self.rotation in (90, 270):
            return self.page_h, self.page_w
        return self.page_w, self.page_h

    def display_geometry(self):
        if not self.pixmap:
            return None
        margin = 12
        available_w = max(1, self.width() - margin * 2)
        available_h = max(1, self.height() - margin * 2)
        rotated = self.pixmap.transformed(QTransform().rotate(self.rotation), Qt.SmoothTransformation)
        scaled = rotated.scaled(available_w, available_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = (self.width() - scaled.width()) / 2
        y = (self.height() - scaled.height()) / 2
        rw, rh = self.rotated_page_size()
        sx = scaled.width() / max(rw, 1.0)
        sy = scaled.height() / max(rh, 1.0)
        return scaled, x, y, sx, sy

    def page_to_rotated(self, x, y):
        r = self.rotation
        if r == 90:
            return self.page_h - y, x
        if r == 180:
            return self.page_w - x, self.page_h - y
        if r == 270:
            return y, self.page_w - x
        return x, y

    def rotated_to_page(self, x, y):
        r = self.rotation
        if r == 90:
            return y, self.page_h - x
        if r == 180:
            return self.page_w - x, self.page_h - y
        if r == 270:
            return self.page_w - y, x
        return x, y

    def rotate_rect(self, rect: fitz.Rect) -> fitz.Rect:
        pts = [
            self.page_to_rotated(rect.x0, rect.y0),
            self.page_to_rotated(rect.x1, rect.y0),
            self.page_to_rotated(rect.x0, rect.y1),
            self.page_to_rotated(rect.x1, rect.y1),
        ]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return fitz.Rect(min(xs), min(ys), max(xs), max(ys))

    def mousePressEvent(self, event):
        geometry = self.display_geometry()
        if geometry and event.button() == Qt.LeftButton:
            scaled, x, y, sx, sy = geometry
            px = event.position().x()
            py = event.position().y()
            if x <= px <= x + scaled.width() and y <= py <= y + scaled.height():
                rx = (px - x) / sx
                ry = (py - y) / sy
                page_x, page_y = self.rotated_to_page(rx, ry)
                self.pageClicked.emit(page_x, page_y)
                event.accept()
                return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(42, 42, 42))
        geometry = self.display_geometry()
        if not geometry:
            painter.setPen(Qt.white)
            painter.drawText(self.rect(), Qt.AlignCenter, "Select a PDF")
            return

        scaled, x, y, sx, sy = geometry
        painter.drawPixmap(int(x), int(y), scaled)

        def draw_box(rect, color, width):
            rr = self.rotate_rect(rect)
            qrect = QRectF(
                x + rr.x0 * sx,
                y + rr.y0 * sy,
                rr.width * sx,
                rr.height * sy,
            )
            painter.setPen(QPen(color, width))
            fill = QColor(color)
            fill.setAlpha(55)
            painter.setBrush(fill)
            painter.drawRect(qrect)

        if self.keyword_rect:
            draw_box(self.keyword_rect, QColor(255, 193, 7), 2)
        if self.value_rect:
            draw_box(self.value_rect, QColor(76, 175, 80), 3)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Renamer - Batch")
        self.resize(1600, 900)
        self.folder: Optional[Path] = None
        self.jobs: list[PdfJob] = []
        self.doc = None
        self.current_job = -1
        self.current_page = 0
        self.preview_rotation = 0
        self.manual_row = -1
        self.manual_stage = None
        self.manual_tag_rect = None
        self.manual_tag_page = None
        self.build_ui()

    def build_ui(self):
        tb = QToolBar("Main")
        self.addToolBar(tb)
        open_action = QAction("Open Folder", self)
        open_action.triggered.connect(self.open_folder)
        tb.addAction(open_action)
        rescan_action = QAction("Rescan", self)
        rescan_action.triggered.connect(self.rescan)
        tb.addAction(rescan_action)
        self.folder_label = QLabel("No folder selected")
        tb.addWidget(self.folder_label)

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        top = QHBoxLayout()
        top.addWidget(QLabel("Values:"))
        self.count = QSpinBox()
        self.count.setRange(1, 20)
        self.count.setValue(1)
        self.count.valueChanged.connect(self.rebuild_rules)
        top.addWidget(self.count)
        top.addWidget(QLabel("Separator:"))
        self.separator = QLineEdit("_")
        self.separator.setMaximumWidth(80)
        self.separator.textChanged.connect(self.refresh_current_filename)
        top.addWidget(self.separator)
        ll.addLayout(top)

        self.rules = QTableWidget(1, 2)
        self.rules.setHorizontalHeaderLabels(["Order", "Keyword"])
        self.rules.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        ll.addWidget(self.rules)

        rb = QHBoxLayout()
        up = QPushButton("Move Up")
        down = QPushButton("Move Down")
        up.clicked.connect(lambda: self.move_rule(-1))
        down.clicked.connect(lambda: self.move_rule(1))
        rb.addWidget(up)
        rb.addWidget(down)
        ll.addLayout(rb)

        ll.addWidget(QLabel("PDF files:"))
        self.file_list = QListWidget()
        self.file_list.currentRowChanged.connect(self.select_job)
        ll.addWidget(self.file_list, 2)

        bb = QHBoxLayout()
        analyze = QPushButton("Analyze All PDFs")
        rename = QPushButton("Rename Passed Files")
        analyze.clicked.connect(self.analyze_all)
        rename.clicked.connect(self.rename_passed)
        bb.addWidget(analyze)
        bb.addWidget(rename)
        ll.addLayout(bb)
        self.status = QLabel("Ready.")
        self.status.setWordWrap(True)
        ll.addWidget(self.status)

        center = QWidget()
        cl = QVBoxLayout(center)
        self.selected = QLabel("No PDF selected")
        self.selected.setWordWrap(True)
        cl.addWidget(self.selected)
        self.results = QTableWidget(0, 5)
        self.results.setHorizontalHeaderLabels(["Order", "Keyword", "Value", "Confidence", "Page"])
        self.results.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.results.itemSelectionChanged.connect(self.result_selected)
        cl.addWidget(self.results)

        manual_buttons = QHBoxLayout()
        self.manual_pick_btn = QPushButton("Manual Pick Tag → Value")
        self.manual_pick_btn.clicked.connect(self.start_manual_pick)
        manual_buttons.addWidget(self.manual_pick_btn)
        clear_manual_btn = QPushButton("Clear Selected Result")
        clear_manual_btn.clicked.connect(self.clear_selected_result)
        manual_buttons.addWidget(clear_manual_btn)
        cl.addLayout(manual_buttons)

        self.manual_help = QLabel(
            "Manual mode: select a result row, click 'Manual Pick Tag → Value', "
            "then click the tag and the value in the PDF. Rotation is preview-only."
        )
        self.manual_help.setWordWrap(True)
        cl.addWidget(self.manual_help)

        cl.addWidget(QLabel("Proposed filename:"))
        self.filename = QLineEdit()
        cl.addWidget(self.filename)

        right = QWidget()
        rl = QVBoxLayout(right)
        nav = QHBoxLayout()
        prev_btn = QPushButton("◀ Previous")
        next_btn = QPushButton("Next ▶")
        rotate_left_btn = QPushButton("⟲ Rotate Left")
        rotate_right_btn = QPushButton("⟳ Rotate Right")
        reset_rotation_btn = QPushButton("Reset")
        prev_btn.clicked.connect(self.prev_page)
        next_btn.clicked.connect(self.next_page)
        rotate_left_btn.clicked.connect(lambda: self.rotate_preview(-90))
        rotate_right_btn.clicked.connect(lambda: self.rotate_preview(90))
        reset_rotation_btn.clicked.connect(self.reset_rotation)
        self.page_label = QLabel("Page 0 / 0")
        self.page_label.setAlignment(Qt.AlignCenter)
        nav.addWidget(prev_btn)
        nav.addWidget(self.page_label, 1)
        nav.addWidget(next_btn)
        nav.addWidget(rotate_left_btn)
        nav.addWidget(rotate_right_btn)
        nav.addWidget(reset_rotation_btn)
        rl.addLayout(nav)
        self.rotation_label = QLabel("Rotation: 0° (preview only)")
        self.rotation_label.setAlignment(Qt.AlignRight)
        rl.addWidget(self.rotation_label)
        rl.addWidget(QLabel('<span style="color:#ffc107;">■ Keyword</span> &nbsp; <span style="color:#4caf50;">■ Value</span>'))
        self.canvas = PdfCanvas()
        self.canvas.pageClicked.connect(self.pdf_clicked)
        rl.addWidget(self.canvas, 1)

        splitter.addWidget(left)
        splitter.addWidget(center)
        splitter.addWidget(right)
        splitter.setSizes([450, 500, 650])
        self.setCentralWidget(splitter)
        self.rebuild_rules()

    def rebuild_rules(self):
        old = [self.rules.item(i, 1).text() if self.rules.item(i, 1) else "" for i in range(self.rules.rowCount())]
        self.rules.setRowCount(self.count.value())
        for i in range(self.count.value()):
            order = QTableWidgetItem(str(i + 1))
            order.setFlags(order.flags() & ~Qt.ItemIsEditable)
            self.rules.setItem(i, 0, order)
            self.rules.setItem(i, 1, QTableWidgetItem(old[i] if i < len(old) else ""))

    def move_rule(self, delta):
        row = self.rules.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.rules.rowCount():
            return
        a = self.rules.item(row, 1).text()
        b = self.rules.item(target, 1).text()
        self.rules.item(row, 1).setText(b)
        self.rules.item(target, 1).setText(a)
        self.rules.selectRow(target)

    def keywords(self):
        return [self.rules.item(i, 1).text().strip() if self.rules.item(i, 1) else "" for i in range(self.rules.rowCount())]

    def open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select PDF folder")
        if path:
            self.folder = Path(path)
            self.folder_label.setText(str(self.folder))
            self.rescan()

    def rescan(self):
        if not self.folder:
            return
        self.cancel_manual_pick()
        pdfs = sorted(
            [p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"],
            key=lambda p: p.name.casefold(),
        )
        self.jobs = [PdfJob(p) for p in pdfs]
        self.refresh_list()
        self.status.setText(f"Found {len(self.jobs)} PDF(s).")
        if self.jobs:
            self.file_list.setCurrentRow(0)

    def refresh_list(self):
        selected = self.file_list.currentRow()
        self.file_list.blockSignals(True)
        self.file_list.clear()
        for job in self.jobs:
            text = f"[{job.status}] {job.path.name}"
            if job.proposed_name:
                text += f" → {job.proposed_name}"
            self.file_list.addItem(text)
        self.file_list.blockSignals(False)
        if self.jobs and selected >= 0:
            self.file_list.setCurrentRow(min(selected, len(self.jobs) - 1))

    @staticmethod
    def norm(text):
        return re.sub(r"\s+", " ", text.strip()).casefold()

    @staticmethod
    def union(words):
        return fitz.Rect(
            min(w[0] for w in words), min(w[1] for w in words),
            max(w[2] for w in words), max(w[3] for w in words)
        )

    @staticmethod
    def find_span(words, target):
        if len(target) > len(words):
            return None
        for i in range(len(words) - len(target) + 1):
            if words[i:i + len(target)] == target:
                return i, i + len(target)
        stripped_words = [re.sub(r"[:：]+$", "", w) for w in words]
        stripped_target = [re.sub(r"[:：]+$", "", t) for t in target]
        for i in range(len(stripped_words) - len(stripped_target) + 1):
            if stripped_words[i:i + len(stripped_target)] == stripped_target:
                return i, i + len(stripped_target)
        return None

    def take_value_words(self, words, label_norms):
        if not words:
            return []
        words = list(words)
        while words and words[0][4].strip() in {":", "：", "-", "–", "—", "="}:
            words.pop(0)
        if not words:
            return []
        chosen = []
        previous_x1 = None
        for word in words:
            text = word[4].strip()
            normalized = re.sub(r"[:：]+$", "", self.norm(text))
            if normalized in label_norms:
                if chosen:
                    break
                return []
            if text.endswith((":", "：")):
                if chosen:
                    break
                return []
            if previous_x1 is not None and word[0] - previous_x1 > 32:
                break
            chosen.append(word)
            previous_x1 = word[2]
            if len(chosen) >= 12:
                break
        return chosen

    def find_value(self, doc, keyword, configured_keywords=None):
        target = self.norm(keyword).split()
        if not target:
            return None
        labels = {
            re.sub(r"[:：]+$", "", self.norm(k))
            for k in (configured_keywords or []) if k.strip()
        }

        for page_index in range(doc.page_count):
            page = doc[page_index]
            words = page.get_text("words", sort=True)
            if not words:
                continue
            lines = {}
            for word in words:
                lines.setdefault((int(word[5]), int(word[6])), []).append(word)

            for line_words in lines.values():
                line_words.sort(key=lambda w: w[7])
                normalized_words = [self.norm(w[4]) for w in line_words]
                span = self.find_span(normalized_words, target)
                if span is None:
                    continue

                start, end = span
                keyword_words = line_words[start:end]
                keyword_rect = self.union(keyword_words)
                kw_center_y = (keyword_rect.y0 + keyword_rect.y1) / 2
                kw_height = max(keyword_rect.height, 1.0)

                right_candidates = []
                for word in words:
                    rect = fitz.Rect(word[0], word[1], word[2], word[3])
                    if rect.intersects(keyword_rect) and rect.get_area() > 0:
                        continue
                    if rect.x0 < keyword_rect.x1 - 1:
                        continue
                    if rect.x0 - keyword_rect.x1 > 320:
                        continue
                    center_y = (rect.y0 + rect.y1) / 2
                    if abs(center_y - kw_center_y) > max(5.0, kw_height * 0.75):
                        continue
                    right_candidates.append(word)

                right_candidates.sort(key=lambda w: w[0])
                value_words = self.take_value_words(right_candidates, labels)
                if value_words:
                    return MatchResult(
                        keyword,
                        " ".join(w[4] for w in value_words).strip(),
                        page_index,
                        keyword_rect,
                        self.union(value_words),
                        "HIGH",
                    )

                candidates = []
                for other in lines.values():
                    if not other:
                        continue
                    other = sorted(other, key=lambda w: w[0])
                    rect = self.union(other)
                    vertical_gap = rect.y0 - keyword_rect.y1
                    if vertical_gap < 0 or vertical_gap > 120:
                        continue
                    horizontal_distance = min(
                        abs(rect.x0 - keyword_rect.x0),
                        abs(rect.x0 - keyword_rect.x1),
                    )
                    if horizontal_distance > 180:
                        continue
                    value_words = self.take_value_words(other, labels)
                    if not value_words:
                        continue
                    score = vertical_gap + min(horizontal_distance * 0.25, 60)
                    candidates.append((score, value_words))

                if candidates:
                    _, value_words = min(candidates, key=lambda item: item[0])
                    return MatchResult(
                        keyword,
                        " ".join(w[4] for w in value_words).strip(),
                        page_index,
                        keyword_rect,
                        self.union(value_words),
                        "MEDIUM",
                    )
        return None

    def start_manual_pick(self):
        if not self.doc or self.current_job < 0:
            return QMessageBox.information(self, "No PDF selected", "Select a PDF before using manual mapping.")
        row = self.results.currentRow()
        if row < 0:
            return QMessageBox.information(self, "Select a field", "Select the keyword/value row you want to correct first.")
        self.manual_row = row
        self.manual_stage = "tag"
        self.manual_tag_rect = None
        self.manual_tag_page = None
        keyword = self.keywords()[row]
        self.status.setText(f"Manual mapping for '{keyword}': click the TAG in the PDF.")
        self.manual_pick_btn.setText("Picking tag…")

    def cancel_manual_pick(self):
        self.manual_row = -1
        self.manual_stage = None
        self.manual_tag_rect = None
        self.manual_tag_page = None
        if hasattr(self, "manual_pick_btn"):
            self.manual_pick_btn.setText("Manual Pick Tag → Value")

    def pdf_clicked(self, page_x, page_y):
        if self.manual_stage not in {"tag", "value"}:
            return
        if not self.doc or self.current_job < 0:
            self.cancel_manual_pick()
            return
        page = self.doc[self.current_page]

        if self.manual_stage == "tag":
            rect = self.manual_tag_rect_from_click(page, page_x, page_y, self.keywords()[self.manual_row])
            if rect is None:
                self.status.setText("No text found at that point. Click directly on the tag text.")
                return
            self.manual_tag_rect = rect
            self.manual_tag_page = self.current_page
            self.manual_stage = "value"
            self.render_page(keyword_rect=rect)
            self.status.setText("Tag selected. Now click the VALUE in the PDF (for multi-word values, click the first word).")
            self.manual_pick_btn.setText("Picking value…")
            return

        value_words = self.manual_value_words_from_click(page, page_x, page_y)
        if not value_words:
            self.status.setText("No value text found at that point. Click directly on the value.")
            return
        if self.manual_tag_page != self.current_page:
            self.status.setText("Tag and value must be selected on the same page. Start manual mapping again.")
            self.cancel_manual_pick()
            return

        value = " ".join(word[4] for word in value_words).strip()
        value_rect = self.union(value_words)
        keyword = self.keywords()[self.manual_row]
        job = self.jobs[self.current_job]
        if job.results is None or len(job.results) != len(self.keywords()):
            job.results = [None] * len(self.keywords())
        result = MatchResult(keyword, value, self.current_page, self.manual_tag_rect, value_rect, "MANUAL")
        job.results[self.manual_row] = result
        job.proposed_name = self.proposed(job.results)
        self.recalculate_job_status(job)
        selected_row = self.manual_row
        self.cancel_manual_pick()
        self.refresh_list()
        self.populate_current_job()
        self.results.selectRow(selected_row)
        self.show_result(result)
        self.status.setText(f"Manual mapping saved: {keyword} → {value}")

    def word_at_point(self, page, x, y, tolerance=3.0):
        words = page.get_text("words", sort=True)
        if not words:
            return None
        point = fitz.Point(x, y)
        direct = []
        for word in words:
            rect = fitz.Rect(word[0], word[1], word[2], word[3])
            expanded = fitz.Rect(rect.x0 - tolerance, rect.y0 - tolerance, rect.x1 + tolerance, rect.y1 + tolerance)
            if point in expanded:
                direct.append((rect.get_area(), word))
        if direct:
            return min(direct, key=lambda item: item[0])[1]
        nearby = []
        for word in words:
            rect = fitz.Rect(word[0], word[1], word[2], word[3])
            cx = min(max(x, rect.x0), rect.x1)
            cy = min(max(y, rect.y0), rect.y1)
            distance_sq = (x - cx) ** 2 + (y - cy) ** 2
            if distance_sq <= 100:
                nearby.append((distance_sq, word))
        return min(nearby, key=lambda item: item[0])[1] if nearby else None

    def manual_tag_rect_from_click(self, page, x, y, keyword):
        clicked = self.word_at_point(page, x, y)
        if clicked is None:
            return None
        words = page.get_text("words", sort=True)
        target = self.norm(keyword).split()
        lines = {}
        for word in words:
            lines.setdefault((int(word[5]), int(word[6])), []).append(word)
        point = fitz.Point(x, y)
        for line_words in lines.values():
            line_words.sort(key=lambda w: w[7])
            normalized_words = [self.norm(w[4]) for w in line_words]
            span = self.find_span(normalized_words, target)
            if span is None:
                continue
            start, end = span
            tag_words = line_words[start:end]
            rect = self.union(tag_words)
            expanded = fitz.Rect(rect.x0 - 4, rect.y0 - 4, rect.x1 + 4, rect.y1 + 4)
            if point in expanded:
                return rect
        return fitz.Rect(clicked[0], clicked[1], clicked[2], clicked[3])

    def manual_value_words_from_click(self, page, x, y):
        clicked = self.word_at_point(page, x, y)
        if clicked is None:
            return []
        words = page.get_text("words", sort=True)
        block_no = int(clicked[5])
        line_no = int(clicked[6])
        word_no = int(clicked[7])
        line_words = [
            word for word in words
            if int(word[5]) == block_no and int(word[6]) == line_no
        ]
        line_words.sort(key=lambda word: word[7])
        start_index = None
        for index, word in enumerate(line_words):
            if int(word[7]) == word_no:
                start_index = index
                break
        if start_index is None:
            return [clicked]

        chosen = []
        previous_x1 = None
        for word in line_words[start_index:]:
            text = word[4].strip()
            if previous_x1 is not None and word[0] - previous_x1 > 32:
                break
            if chosen and text.endswith((":", "：")):
                break
            chosen.append(word)
            previous_x1 = word[2]
            if len(chosen) >= 12:
                break
        return chosen

    def clear_selected_result(self):
        if self.current_job < 0:
            return
        row = self.results.currentRow()
        if row < 0:
            return
        job = self.jobs[self.current_job]
        if job.results is None:
            return
        if row < len(job.results):
            job.results[row] = None
            job.proposed_name = self.proposed(job.results)
            self.recalculate_job_status(job)
            self.refresh_list()
            self.populate_current_job()
            self.results.selectRow(row)
            self.status.setText("Selected extraction result cleared.")

    def recalculate_job_status(self, job):
        if not job.results or any(result is None for result in job.results):
            job.status = "REVIEW"
            job.error = "One or more values were not found"
            return
        job.proposed_name = self.proposed(job.results)
        if not job.proposed_name:
            job.status = "REVIEW"
            job.error = "Generated filename is empty"
            return
        destination = job.path.with_name(job.proposed_name)
        if destination.exists() and destination != job.path:
            job.status = "CONFLICT"
            job.error = "Destination already exists"
        else:
            job.status = "PASS"
            job.error = ""

    @staticmethod
    def safe(text):
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text)
        return re.sub(r"\s+", " ", text).strip().rstrip(". ")[:120]

    def proposed(self, results):
        if not results or any(result is None for result in results):
            return ""
        parts = [self.safe(result.value) for result in results]
        if not all(parts):
            return ""
        return self.separator.text().join(parts) + ".pdf"

    def refresh_current_filename(self):
        if self.current_job < 0 or self.current_job >= len(self.jobs):
            return
        job = self.jobs[self.current_job]
        if job.results:
            job.proposed_name = self.proposed(job.results)
            self.recalculate_job_status(job)
            self.filename.setText(job.proposed_name)
            self.refresh_list()

    def analyze_all(self):
        keys = self.keywords()
        if not self.jobs:
            return QMessageBox.information(self, "No PDFs", "Select a folder first.")
        if any(not key for key in keys):
            return QMessageBox.warning(self, "Missing keyword", "Fill every keyword row.")

        self.cancel_manual_pick()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for index, job in enumerate(self.jobs):
                self.status.setText(f"Analyzing {index + 1}/{len(self.jobs)}: {job.path.name}")
                QApplication.processEvents()
                try:
                    with fitz.open(str(job.path)) as doc:
                        if sum(len(page.get_text("words")) for page in doc) == 0:
                            job.status = "OCR NEEDED"
                            job.error = "No embedded text"
                            job.results = None
                            job.proposed_name = ""
                            continue
                        job.results = [self.find_value(doc, key, keys) for key in keys]
                        job.proposed_name = self.proposed(job.results)
                        self.recalculate_job_status(job)
                except Exception as exc:
                    job.status = "ERROR"
                    job.error = str(exc)
                    job.results = None
                    job.proposed_name = ""
        finally:
            QApplication.restoreOverrideCursor()

        self.refresh_list()
        self.status.setText("Analysis complete.")
        if self.current_job >= 0:
            self.select_job(self.current_job)

    def select_job(self, row):
        if row < 0 or row >= len(self.jobs):
            return
        self.cancel_manual_pick()
        self.current_job = row
        self.preview_rotation = 0
        self.open_doc(self.jobs[row].path)
        self.populate_current_job()

    def populate_current_job(self):
        if self.current_job < 0 or self.current_job >= len(self.jobs):
            return
        job = self.jobs[self.current_job]
        self.selected.setText(
            f"{job.path.name}\nStatus: {job.status}" + (f"\n{job.error}" if job.error else "")
        )
        self.filename.setText(job.proposed_name)
        keys = self.keywords()
        self.results.setRowCount(len(keys))
        for i, key in enumerate(keys):
            result = job.results[i] if job.results and i < len(job.results) else None
            values = [
                str(i + 1),
                key,
                result.value if result else "NOT FOUND",
                result.confidence if result else "-",
                str(result.page_index + 1) if result else "-",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.results.setItem(i, column, item)
        if job.results:
            for i, result in enumerate(job.results):
                if result:
                    self.results.selectRow(i)
                    self.show_result(result)
                    break
        else:
            self.render_page()

    def open_doc(self, path):
        if self.doc:
            self.doc.close()
        self.doc = fitz.open(str(path))
        self.current_page = 0
        self.render_page()

    def render_page(self, keyword_rect=None, value_rect=None):
        if not self.doc:
            return
        page = self.doc[self.current_page]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image = QImage(
            pix.samples,
            pix.width,
            pix.height,
            pix.stride,
            QImage.Format_RGB888,
        ).copy()
        self.canvas.set_page(
            QPixmap.fromImage(image),
            page.rect.width,
            page.rect.height,
            keyword_rect,
            value_rect,
            self.preview_rotation,
        )
        self.page_label.setText(f"Page {self.current_page + 1} / {self.doc.page_count}")
        self.rotation_label.setText(f"Rotation: {self.preview_rotation}° (preview only)")

    def rotate_preview(self, delta):
        if not self.doc:
            return
        self.preview_rotation = (self.preview_rotation + delta) % 360
        keyword_rect = None
        value_rect = None
        if self.current_job >= 0:
            row = self.results.currentRow()
            job = self.jobs[self.current_job]
            if job.results and 0 <= row < len(job.results) and job.results[row]:
                result = job.results[row]
                if result.page_index == self.current_page:
                    keyword_rect = result.keyword_rect
                    value_rect = result.value_rect
        if self.manual_stage == "value" and self.manual_tag_page == self.current_page:
            keyword_rect = self.manual_tag_rect
            value_rect = None
        self.render_page(keyword_rect, value_rect)

    def reset_rotation(self):
        if not self.doc:
            return
        self.preview_rotation = 0
        self.rotate_preview(0)

    def result_selected(self):
        if self.current_job < 0:
            return
        row = self.results.currentRow()
        job = self.jobs[self.current_job]
        if job.results and 0 <= row < len(job.results) and job.results[row]:
            self.show_result(job.results[row])

    def show_result(self, result):
        self.current_page = result.page_index
        self.render_page(result.keyword_rect, result.value_rect)

    def prev_page(self):
        if self.doc:
            self.current_page = max(0, self.current_page - 1)
            self.render_page()

    def next_page(self):
        if self.doc:
            self.current_page = min(self.doc.page_count - 1, self.current_page + 1)
            self.render_page()

    def rename_passed(self):
        passed = [job for job in self.jobs if job.status == "PASS"]
        if not passed:
            return QMessageBox.information(self, "Nothing to rename", "No PASS files.")
        answer = QMessageBox.question(
            self,
            "Confirm",
            f"Rename {len(passed)} PDF(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self.cancel_manual_pick()
        if self.doc:
            self.doc.close()
            self.doc = None

        renamed = 0
        for job in passed:
            try:
                destination = job.path.with_name(job.proposed_name)
                if destination.exists() and destination != job.path:
                    job.status = "CONFLICT"
                    continue
                if destination != job.path:
                    job.path.rename(destination)
                    job.path = destination
                job.status = "RENAMED"
                renamed += 1
            except Exception as exc:
                job.status = "ERROR"
                job.error = str(exc)

        self.refresh_list()
        self.status.setText(f"Renamed {renamed} PDF(s).")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
