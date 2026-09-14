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
            Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        x = (self.width() - scaled.width()) / 2
        y = (self.height() - scaled.height()) / 2
        painter.drawPixmap(int(x), int(y), scaled)
        sx, sy = scaled.width() / self.page_w, scaled.height() / self.page_h

        def box(rect, color, width):
            q = QRectF(x + rect.x0 * sx, y + rect.y0 * sy, rect.width * sx, rect.height * sy)
            painter.setPen(QPen(color, width))
            fill = QColor(color); fill.setAlpha(55); painter.setBrush(fill)
            painter.drawRect(q)

        if self.keyword_rect:
            box(self.keyword_rect, QColor(255, 193, 7), 2)
        if self.value_rect:
            box(self.value_rect, QColor(76, 175, 80), 3)


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
        tb = QToolBar("Main"); self.addToolBar(tb)
        open_action = QAction("Open Folder", self); open_action.triggered.connect(self.open_folder); tb.addAction(open_action)
        rescan_action = QAction("Rescan", self); rescan_action.triggered.connect(self.rescan); tb.addAction(rescan_action)
        self.folder_label = QLabel("No folder selected"); tb.addWidget(self.folder_label)

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget(); ll = QVBoxLayout(left)
        top = QHBoxLayout(); top.addWidget(QLabel("Values:"))
        self.count = QSpinBox(); self.count.setRange(1, 20); self.count.setValue(1); self.count.valueChanged.connect(self.rebuild_rules); top.addWidget(self.count)
        top.addWidget(QLabel("Separator:")); self.separator = QLineEdit("_"); self.separator.setMaximumWidth(80); top.addWidget(self.separator); ll.addLayout(top)
        self.rules = QTableWidget(1, 2); self.rules.setHorizontalHeaderLabels(["Order", "Keyword"]); self.rules.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch); ll.addWidget(self.rules)
        rb = QHBoxLayout(); up = QPushButton("Move Up"); down = QPushButton("Move Down"); up.clicked.connect(lambda: self.move_rule(-1)); down.clicked.connect(lambda: self.move_rule(1)); rb.addWidget(up); rb.addWidget(down); ll.addLayout(rb)
        ll.addWidget(QLabel("PDF files:")); self.file_list = QListWidget(); self.file_list.currentRowChanged.connect(self.select_job); ll.addWidget(self.file_list, 2)
        bb = QHBoxLayout(); analyze = QPushButton("Analyze All PDFs"); rename = QPushButton("Rename Passed Files"); analyze.clicked.connect(self.analyze_all); rename.clicked.connect(self.rename_passed); bb.addWidget(analyze); bb.addWidget(rename); ll.addLayout(bb)
        self.status = QLabel("Ready."); self.status.setWordWrap(True); ll.addWidget(self.status)

        center = QWidget(); cl = QVBoxLayout(center)
        self.selected = QLabel("No PDF selected"); self.selected.setWordWrap(True); cl.addWidget(self.selected)
        self.results = QTableWidget(0, 5); self.results.setHorizontalHeaderLabels(["Order", "Keyword", "Value", "Confidence", "Page"]); self.results.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch); self.results.itemSelectionChanged.connect(self.result_selected); cl.addWidget(self.results)
        cl.addWidget(QLabel("Proposed filename:")); self.filename = QLineEdit(); cl.addWidget(self.filename)

        right = QWidget(); rl = QVBoxLayout(right)
        nav = QHBoxLayout(); p = QPushButton("◀ Previous"); n = QPushButton("Next ▶"); p.clicked.connect(self.prev_page); n.clicked.connect(self.next_page); self.page_label = QLabel("Page 0 / 0"); self.page_label.setAlignment(Qt.AlignCenter); nav.addWidget(p); nav.addWidget(self.page_label, 1); nav.addWidget(n); rl.addLayout(nav)
        rl.addWidget(QLabel('<span style="color:#ffc107;">■ Keyword</span> &nbsp; <span style="color:#4caf50;">■ Value</span>'))
        self.canvas = PdfCanvas(); rl.addWidget(self.canvas, 1)

        splitter.addWidget(left); splitter.addWidget(center); splitter.addWidget(right); splitter.setSizes([450, 500, 650]); self.setCentralWidget(splitter)
        self.rebuild_rules()

    def rebuild_rules(self):
        old = [self.rules.item(i, 1).text() if self.rules.item(i, 1) else "" for i in range(self.rules.rowCount())]
        self.rules.setRowCount(self.count.value())
        for i in range(self.count.value()):
            o = QTableWidgetItem(str(i + 1)); o.setFlags(o.flags() & ~Qt.ItemIsEditable); self.rules.setItem(i, 0, o)
            self.rules.setItem(i, 1, QTableWidgetItem(old[i] if i < len(old) else ""))

    def move_rule(self, delta):
        r = self.rules.currentRow(); t = r + delta
        if r < 0 or t < 0 or t >= self.rules.rowCount(): return
        a, b = self.rules.item(r, 1).text(), self.rules.item(t, 1).text(); self.rules.item(r, 1).setText(b); self.rules.item(t, 1).setText(a); self.rules.selectRow(t)

    def keywords(self):
        return [self.rules.item(i, 1).text().strip() if self.rules.item(i, 1) else "" for i in range(self.rules.rowCount())]

    def open_folder(self):
        p = QFileDialog.getExistingDirectory(self, "Select PDF folder")
        if p:
            self.folder = Path(p); self.folder_label.setText(str(self.folder)); self.rescan()

    def rescan(self):
        if not self.folder: return
        pdfs = sorted([p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"], key=lambda p: p.name.casefold())
        self.jobs = [PdfJob(p) for p in pdfs]; self.refresh_list(); self.status.setText(f"Found {len(self.jobs)} PDF(s).")
        if self.jobs: self.file_list.setCurrentRow(0)

    def refresh_list(self):
        self.file_list.clear()
        for j in self.jobs:
            txt = f"[{j.status}] {j.path.name}" + (f" → {j.proposed_name}" if j.proposed_name else "")
            self.file_list.addItem(txt)

    @staticmethod
    def norm(s): return re.sub(r"\s+", " ", s.strip()).casefold()

    @staticmethod
    def union(words):
        return fitz.Rect(min(w[0] for w in words), min(w[1] for w in words), max(w[2] for w in words), max(w[3] for w in words))

    def find_value(self, doc, keyword):
        target = [re.sub(r"[:：]+$", "", x) for x in self.norm(keyword).split()]
        for pi in range(doc.page_count):
            words = doc[pi].get_text("words", sort=True)
            lines = {}
            for w in words: lines.setdefault((int(w[5]), int(w[6])), []).append(w)
            for lw in lines.values():
                lw.sort(key=lambda w: w[7]); nw = [re.sub(r"[:：]+$", "", self.norm(w[4])) for w in lw]
                for i in range(len(nw) - len(target) + 1):
                    if nw[i:i+len(target)] != target: continue
                    kw = lw[i:i+len(target)]; kr = self.union(kw)
                    right = [w for w in lw[i+len(target):] if w[0] >= kr.x1 - 2 and w[4].strip() not in {":", "-", "=", "–", "—"}]
                    if right:
                        vr = self.union(right[:12]); return MatchResult(keyword, " ".join(w[4] for w in right[:12]).strip(), pi, kr, vr, "HIGH")
                    candidates = []
                    for other in lines.values():
                        if not other: continue
                        r = self.union(other); gap = r.y0 - kr.y1
                        if -1 <= gap <= 120:
                            score = gap + min(abs(r.x0 - kr.x0) * .15, 50); candidates.append((score, other))
                    if candidates:
                        other = sorted(candidates, key=lambda x: x[0])[0][1][:12]; return MatchResult(keyword, " ".join(w[4] for w in other).strip(), pi, kr, self.union(other), "MEDIUM")
        return None

    @staticmethod
    def safe(s):
        s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s); return re.sub(r"\s+", " ", s).strip().rstrip(". ")[:120]

    def proposed(self, results):
        if not results or any(r is None for r in results): return ""
        parts = [self.safe(r.value) for r in results]; return self.separator.text().join(parts) + ".pdf" if all(parts) else ""

    def analyze_all(self):
        keys = self.keywords()
        if not self.jobs: return QMessageBox.information(self, "No PDFs", "Select a folder first.")
        if any(not k for k in keys): return QMessageBox.warning(self, "Missing keyword", "Fill every keyword row.")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for idx, j in enumerate(self.jobs):
                self.status.setText(f"Analyzing {idx+1}/{len(self.jobs)}: {j.path.name}"); QApplication.processEvents()
                try:
                    with fitz.open(str(j.path)) as d:
                        if sum(len(p.get_text("words")) for p in d) == 0:
                            j.status = "OCR NEEDED"; j.error = "No embedded text"; continue
                        j.results = [self.find_value(d, k) for k in keys]; j.proposed_name = self.proposed(j.results)
                        if any(r is None for r in j.results): j.status = "REVIEW"
                        elif j.path.with_name(j.proposed_name).exists() and j.path.with_name(j.proposed_name) != j.path: j.status = "CONFLICT"
                        else: j.status = "PASS"
                except Exception as e:
                    j.status = "ERROR"; j.error = str(e)
        finally:
            QApplication.restoreOverrideCursor()
        self.refresh_list(); self.status.setText("Analysis complete.")
        if self.current_job >= 0: self.select_job(self.current_job)

    def select_job(self, row):
        if row < 0 or row >= len(self.jobs): return
        self.current_job = row; j = self.jobs[row]; self.selected.setText(f"{j.path.name}\nStatus: {j.status}" + (f"\n{j.error}" if j.error else "")); self.filename.setText(j.proposed_name)
        keys = self.keywords(); self.results.setRowCount(len(keys))
        for i, k in enumerate(keys):
            r = j.results[i] if j.results and i < len(j.results) else None
            vals = [str(i+1), k, r.value if r else "NOT FOUND", r.confidence if r else "-", str(r.page_index+1) if r else "-"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v); it.setFlags(it.flags() & ~Qt.ItemIsEditable); self.results.setItem(i, c, it)
        self.open_doc(j.path)
        if j.results:
            for i, r in enumerate(j.results):
                if r: self.results.selectRow(i); self.show_result(r); break

    def open_doc(self, path):
        if self.doc: self.doc.close()
        self.doc = fitz.open(str(path)); self.current_page = 0; self.render_page()

    def render_page(self, kr=None, vr=None):
        if not self.doc: return
        page = self.doc[self.current_page]; pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        self.canvas.set_page(QPixmap.fromImage(img), page.rect.width, page.rect.height, kr, vr); self.page_label.setText(f"Page {self.current_page+1} / {self.doc.page_count}")

    def result_selected(self):
        if self.current_job < 0: return
        row = self.results.currentRow(); j = self.jobs[self.current_job]
        if j.results and 0 <= row < len(j.results) and j.results[row]: self.show_result(j.results[row])

    def show_result(self, r): self.current_page = r.page_index; self.render_page(r.keyword_rect, r.value_rect)
    def prev_page(self):
        if self.doc: self.current_page = max(0, self.current_page - 1); self.render_page()
    def next_page(self):
        if self.doc: self.current_page = min(self.doc.page_count - 1, self.current_page + 1); self.render_page()

    def rename_passed(self):
        passed = [j for j in self.jobs if j.status == "PASS"]
        if not passed: return QMessageBox.information(self, "Nothing to rename", "No PASS files.")
        if QMessageBox.question(self, "Confirm", f"Rename {len(passed)} PDF(s)?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes: return
        if self.doc: self.doc.close(); self.doc = None
        renamed = 0
        for j in passed:
            try:
                dest = j.path.with_name(j.proposed_name)
                if dest.exists() and dest != j.path: j.status = "CONFLICT"; continue
                if dest != j.path: j.path.rename(dest); j.path = dest
                j.status = "RENAMED"; renamed += 1
            except Exception as e:
                j.status = "ERROR"; j.error = str(e)
        self.refresh_list(); self.status.setText(f"Renamed {renamed} PDF(s).")


def main():
    app = QApplication(sys.argv)
    w = MainWindow(); w.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
