from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fitz
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QAction, QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QMainWindow, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout, QWidget
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
    def __init__(self):
        super().__init__()
        self.pixmap = None
        self.page_w = 1.0
        self.page_h = 1.0
        self.keyword_rect = None
        self.value_rect = None
        self.setMinimumSize(500, 600)

    def set_page(self, pixmap, page_w, page_h, keyword_rect=None, value_rect=None):
        self.pixmap = pixmap
        self.page_w = max(page_w, 1)
        self.page_h = max(page_h, 1)
        self.keyword_rect = keyword_rect
        self.value_rect = value_rect
        self.update()

    def clear(self):
        self.pixmap = None
        self.keyword_rect = None
        self.value_rect = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(42, 42, 42))
        if not self.pixmap:
            painter.setPen(Qt.white)
            painter.drawText(self.rect(), Qt.AlignCenter, "Select a PDF")
            return

        margin = 12
        scaled = self.pixmap.scaled(
            max(1, self.width() - margin * 2),
            max(1, self.height() - margin * 2),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) / 2
        y = (self.height() - scaled.height()) / 2
        painter.drawPixmap(int(x), int(y), scaled)
        sx = scaled.width() / self.page_w
        sy = scaled.height() / self.page_h

        def draw_box(rect, color, width):
            qrect = QRectF(
                x + rect.x0 * sx,
                y + rect.y0 * sy,
                rect.width * sx,
                rect.height * sy,
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
        self.folder = None
        self.jobs: list[PdfJob] = []
        self.doc = None
        self.current_job = -1
        self.current_page = 0
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
        self.results.setHorizontalHeaderLabels(
            ["Order", "Keyword", "Value", "Confidence", "Page"]
        )
        self.results.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.results.itemSelectionChanged.connect(self.result_selected)
        cl.addWidget(self.results)
        cl.addWidget(QLabel("Proposed filename:"))
        self.filename = QLineEdit()
        cl.addWidget(self.filename)

        right = QWidget()
        rl = QVBoxLayout(right)
        nav = QHBoxLayout()
        prev_btn = QPushButton("◀ Previous")
        next_btn = QPushButton("Next ▶")
        prev_btn.clicked.connect(self.prev_page)
        next_btn.clicked.connect(self.next_page)
        self.page_label = QLabel("Page 0 / 0")
        self.page_label.setAlignment(Qt.AlignCenter)
        nav.addWidget(prev_btn)
        nav.addWidget(self.page_label, 1)
        nav.addWidget(next_btn)
        rl.addLayout(nav)
        rl.addWidget(
            QLabel('<span style="color:#ffc107;">■ Keyword</span> &nbsp; '
                   '<span style="color:#4caf50;">■ Value</span>')
        )
        self.canvas = PdfCanvas()
        rl.addWidget(self.canvas, 1)

        splitter.addWidget(left)
        splitter.addWidget(center)
        splitter.addWidget(right)
        splitter.setSizes([450, 500, 650])
        self.setCentralWidget(splitter)
        self.rebuild_rules()

    def rebuild_rules(self):
        old = [
            self.rules.item(i, 1).text() if self.rules.item(i, 1) else ""
            for i in range(self.rules.rowCount())
        ]
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
        return [
            self.rules.item(i, 1).text().strip() if self.rules.item(i, 1) else ""
            for i in range(self.rules.rowCount())
        ]

    def open_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select PDF folder")
        if path:
            self.folder = Path(path)
            self.folder_label.setText(str(self.folder))
            self.rescan()

    def rescan(self):
        if not self.folder:
            return
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
        self.file_list.clear()
        for job in self.jobs:
            text = f"[{job.status}] {job.path.name}"
            if job.proposed_name:
                text += f" → {job.proposed_name}"
            item = QTableWidgetItem(text) if False else None
            self.file_list.addItem(text)
        if self.jobs and selected >= 0:
            self.file_list.setCurrentRow(min(selected, len(self.jobs) - 1))

    @staticmethod
    def norm(text):
        return re.sub(r"\s+", " ", text.strip()).casefold()

    @staticmethod
    def union(words):
        return fitz.Rect(
            min(w[0] for w in words),
            min(w[1] for w in words),
            max(w[2] for w in words),
            max(w[3] for w in words),
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
            for k in (configured_keywords or [])
            if k.strip()
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

                # Primary strategy: visual geometry. Search every word on the page
                # that is horizontally aligned to the right of the keyword.
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

                # Fallback: nearest plausible line below the keyword.
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

    def analyze_all(self):
        keys = self.keywords()
        if not self.jobs:
            return QMessageBox.information(self, "No PDFs", "Select a folder first.")
        if any(not key for key in keys):
            return QMessageBox.warning(self, "Missing keyword", "Fill every keyword row.")

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for index, job in enumerate(self.jobs):
                self.status.setText(
                    f"Analyzing {index + 1}/{len(self.jobs)}: {job.path.name}"
                )
                QApplication.processEvents()
                try:
                    with fitz.open(str(job.path)) as doc:
                        if sum(len(page.get_text("words")) for page in doc) == 0:
                            job.status = "OCR NEEDED"
                            job.error = "No embedded text"
                            continue

                        job.results = [self.find_value(doc, key, keys) for key in keys]
                        job.proposed_name = self.proposed(job.results)

                        if any(result is None for result in job.results):
                            job.status = "REVIEW"
                            job.error = "One or more values were not found"
                        elif (
                            job.path.with_name(job.proposed_name).exists()
                            and job.path.with_name(job.proposed_name) != job.path
                        ):
                            job.status = "CONFLICT"
                            job.error = "Destination already exists"
                        else:
                            job.status = "PASS"
                            job.error = ""
                except Exception as exc:
                    job.status = "ERROR"
                    job.error = str(exc)
        finally:
            QApplication.restoreOverrideCursor()

        self.refresh_list()
        self.status.setText("Analysis complete.")
        if self.current_job >= 0:
            self.select_job(self.current_job)

    def select_job(self, row):
        if row < 0 or row >= len(self.jobs):
            return
        self.current_job = row
        job = self.jobs[row]
        self.selected.setText(
            f"{job.path.name}\nStatus: {job.status}"
            + (f"\n{job.error}" if job.error else "")
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

        self.open_doc(job.path)
        if job.results:
            for i, result in enumerate(job.results):
                if result:
                    self.results.selectRow(i)
                    self.show_result(result)
                    break

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
        )
        self.page_label.setText(
            f"Page {self.current_page + 1} / {self.doc.page_count}"
        )

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
            return QMessageBox.information(
                self, "Nothing to rename", "No PASS files."
            )

        answer = QMessageBox.question(
            self,
            "Confirm",
            f"Rename {len(passed)} PDF(s)?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

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
