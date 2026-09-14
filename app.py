from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz
import pytesseract
from PIL import Image
from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QAction, QColor, QImage, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QSplitter, QTableWidget, QTableWidgetItem, QToolBar, QVBoxLayout, QWidget,
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
    ocr_words: dict[int, list] = field(default_factory=dict)


class PdfCanvas(QWidget):
    pageClicked = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.pixmap: Optional[QPixmap] = None
        self.page_w = 1.0
        self.page_h = 1.0
        self.keyword_rect = None
        self.value_rect = None
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

    def display_geometry(self):
        if not self.pixmap:
            return None
        margin = 12
        rotated = self.pixmap.transformed(QTransform().rotate(self.rotation), Qt.SmoothTransformation)
        scaled = rotated.scaled(
            max(1, self.width() - margin * 2), max(1, self.height() - margin * 2),
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        x = (self.width() - scaled.width()) / 2
        y = (self.height() - scaled.height()) / 2
        rw, rh = (self.page_h, self.page_w) if self.rotation in (90, 270) else (self.page_w, self.page_h)
        return scaled, x, y, scaled.width() / rw, scaled.height() / rh

    def page_to_rotated(self, x, y):
        if self.rotation == 90: return self.page_h - y, x
        if self.rotation == 180: return self.page_w - x, self.page_h - y
        if self.rotation == 270: return y, self.page_w - x
        return x, y

    def rotated_to_page(self, x, y):
        if self.rotation == 90: return y, self.page_h - x
        if self.rotation == 180: return self.page_w - x, self.page_h - y
        if self.rotation == 270: return self.page_w - y, x
        return x, y

    def rotate_rect(self, rect):
        pts = [self.page_to_rotated(rect.x0, rect.y0), self.page_to_rotated(rect.x1, rect.y0),
               self.page_to_rotated(rect.x0, rect.y1), self.page_to_rotated(rect.x1, rect.y1)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return fitz.Rect(min(xs), min(ys), max(xs), max(ys))

    def mousePressEvent(self, event):
        g = self.display_geometry()
        if g and event.button() == Qt.LeftButton:
            scaled, x, y, sx, sy = g
            px, py = event.position().x(), event.position().y()
            if x <= px <= x + scaled.width() and y <= py <= y + scaled.height():
                rx, ry = (px - x) / sx, (py - y) / sy
                self.pageClicked.emit(*self.rotated_to_page(rx, ry))
                return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(42, 42, 42))
        g = self.display_geometry()
        if not g:
            painter.setPen(Qt.white)
            painter.drawText(self.rect(), Qt.AlignCenter, "Select a PDF")
            return
        scaled, x, y, sx, sy = g
        painter.drawPixmap(int(x), int(y), scaled)

        def box(rect, color, width):
            rr = self.rotate_rect(rect)
            q = QRectF(x + rr.x0 * sx, y + rr.y0 * sy, rr.width * sx, rr.height * sy)
            painter.setPen(QPen(color, width))
            fill = QColor(color); fill.setAlpha(55); painter.setBrush(fill)
            painter.drawRect(q)

        if self.keyword_rect: box(self.keyword_rect, QColor(255, 193, 7), 2)
        if self.value_rect: box(self.value_rect, QColor(76, 175, 80), 3)


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
        self.tesseract_cmd = self.detect_tesseract()
        if self.tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
        self.build_ui()

    @staticmethod
    def detect_tesseract():
        candidates = []
        env = os.environ.get("TESSERACT_CMD")
        if env: candidates.append(Path(env))
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            candidates += [exe_dir / "tesseract" / "tesseract.exe", exe_dir / "Tesseract-OCR" / "tesseract.exe"]
        candidates += [Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"), Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")]
        for c in candidates:
            if c.exists(): return str(c)
        return shutil.which("tesseract")

    def build_ui(self):
        tb = QToolBar("Main"); self.addToolBar(tb)
        a = QAction("Open Folder", self); a.triggered.connect(self.open_folder); tb.addAction(a)
        a = QAction("Rescan", self); a.triggered.connect(self.rescan); tb.addAction(a)
        self.folder_label = QLabel("No folder selected"); tb.addWidget(self.folder_label)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget(); ll = QVBoxLayout(left)
        top = QHBoxLayout(); top.addWidget(QLabel("Values:"))
        self.count = QSpinBox(); self.count.setRange(1, 20); self.count.setValue(1); self.count.valueChanged.connect(self.rebuild_rules); top.addWidget(self.count)
        top.addWidget(QLabel("Separator:")); self.separator = QLineEdit("_"); self.separator.setMaximumWidth(80); self.separator.textChanged.connect(self.refresh_current_filename); top.addWidget(self.separator); ll.addLayout(top)

        self.rules = QTableWidget(1, 2); self.rules.setHorizontalHeaderLabels(["Order", "Keyword"]); self.rules.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch); ll.addWidget(self.rules)
        rb = QHBoxLayout(); up = QPushButton("Move Up"); down = QPushButton("Move Down"); up.clicked.connect(lambda: self.move_rule(-1)); down.clicked.connect(lambda: self.move_rule(1)); rb.addWidget(up); rb.addWidget(down); ll.addLayout(rb)

        ocr = QHBoxLayout()
        self.ocr_enabled = QCheckBox("Auto OCR scanned PDFs"); self.ocr_enabled.setChecked(True); ocr.addWidget(self.ocr_enabled)
        ocr.addWidget(QLabel("Lang:")); self.ocr_lang = QLineEdit("eng"); self.ocr_lang.setMaximumWidth(70); ocr.addWidget(self.ocr_lang)
        ocr.addWidget(QLabel("DPI:")); self.ocr_dpi = QSpinBox(); self.ocr_dpi.setRange(150, 600); self.ocr_dpi.setValue(300); ocr.addWidget(self.ocr_dpi)
        ll.addLayout(ocr)
        self.ocr_status = QLabel("OCR engine: " + (self.tesseract_cmd if self.tesseract_cmd else "NOT FOUND")); self.ocr_status.setWordWrap(True); ll.addWidget(self.ocr_status)

        ll.addWidget(QLabel("PDF files:")); self.file_list = QListWidget(); self.file_list.currentRowChanged.connect(self.select_job); ll.addWidget(self.file_list, 2)
        bb = QHBoxLayout(); analyze = QPushButton("Analyze All PDFs"); rename = QPushButton("Rename Passed Files"); analyze.clicked.connect(self.analyze_all); rename.clicked.connect(self.rename_passed); bb.addWidget(analyze); bb.addWidget(rename); ll.addLayout(bb)
        self.status = QLabel("Ready."); self.status.setWordWrap(True); ll.addWidget(self.status)

        center = QWidget(); cl = QVBoxLayout(center)
        self.selected = QLabel("No PDF selected"); self.selected.setWordWrap(True); cl.addWidget(self.selected)
        self.results = QTableWidget(0, 5); self.results.setHorizontalHeaderLabels(["Order", "Keyword", "Value", "Confidence", "Page"]); self.results.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch); self.results.itemSelectionChanged.connect(self.result_selected); cl.addWidget(self.results)
        mb = QHBoxLayout(); self.manual_pick_btn = QPushButton("Manual Pick Tag → Value"); self.manual_pick_btn.clicked.connect(self.start_manual_pick); mb.addWidget(self.manual_pick_btn); clear = QPushButton("Clear Selected Result"); clear.clicked.connect(self.clear_selected_result); mb.addWidget(clear); cl.addLayout(mb)
        help_label = QLabel("Manual mode works with normal text and OCR text. Select a row, click the tag, then the value."); help_label.setWordWrap(True); cl.addWidget(help_label)
        cl.addWidget(QLabel("Proposed filename:")); self.filename = QLineEdit(); cl.addWidget(self.filename)

        right = QWidget(); rl = QVBoxLayout(right)
        nav = QHBoxLayout(); prev = QPushButton("◀ Previous"); nxt = QPushButton("Next ▶"); leftrot = QPushButton("⟲ Rotate Left"); rightrot = QPushButton("⟳ Rotate Right"); reset = QPushButton("Reset")
        prev.clicked.connect(self.prev_page); nxt.clicked.connect(self.next_page); leftrot.clicked.connect(lambda: self.rotate_preview(-90)); rightrot.clicked.connect(lambda: self.rotate_preview(90)); reset.clicked.connect(self.reset_rotation)
        self.page_label = QLabel("Page 0 / 0"); self.page_label.setAlignment(Qt.AlignCenter)
        for w in (prev, self.page_label, nxt, leftrot, rightrot, reset): nav.addWidget(w)
        rl.addLayout(nav); self.rotation_label = QLabel("Rotation: 0° (preview only)"); self.rotation_label.setAlignment(Qt.AlignRight); rl.addWidget(self.rotation_label)
        rl.addWidget(QLabel('<span style="color:#ffc107;">■ Keyword</span> &nbsp; <span style="color:#4caf50;">■ Value</span>'))
        self.canvas = PdfCanvas(); self.canvas.pageClicked.connect(self.pdf_clicked); rl.addWidget(self.canvas, 1)

        splitter.addWidget(left); splitter.addWidget(center); splitter.addWidget(right); splitter.setSizes([450, 500, 650]); self.setCentralWidget(splitter); self.rebuild_rules()

    def rebuild_rules(self):
        old = [self.rules.item(i, 1).text() if self.rules.item(i, 1) else "" for i in range(self.rules.rowCount())]
        self.rules.setRowCount(self.count.value())
        for i in range(self.count.value()):
            order = QTableWidgetItem(str(i + 1)); order.setFlags(order.flags() & ~Qt.ItemIsEditable); self.rules.setItem(i, 0, order); self.rules.setItem(i, 1, QTableWidgetItem(old[i] if i < len(old) else ""))

    def move_rule(self, delta):
        r = self.rules.currentRow(); t = r + delta
        if r < 0 or t < 0 or t >= self.rules.rowCount(): return
        a, b = self.rules.item(r, 1).text(), self.rules.item(t, 1).text(); self.rules.item(r, 1).setText(b); self.rules.item(t, 1).setText(a); self.rules.selectRow(t)

    def keywords(self):
        return [self.rules.item(i, 1).text().strip() if self.rules.item(i, 1) else "" for i in range(self.rules.rowCount())]

    def open_folder(self):
        p = QFileDialog.getExistingDirectory(self, "Select PDF folder")
        if p: self.folder = Path(p); self.folder_label.setText(str(self.folder)); self.rescan()

    def rescan(self):
        if not self.folder: return
        self.cancel_manual_pick()
        pdfs = sorted([p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"], key=lambda p: p.name.casefold())
        self.jobs = [PdfJob(p) for p in pdfs]; self.refresh_list(); self.status.setText(f"Found {len(self.jobs)} PDF(s).")
        if self.jobs: self.file_list.setCurrentRow(0)

    def refresh_list(self):
        selected = self.file_list.currentRow(); self.file_list.blockSignals(True); self.file_list.clear()
        for j in self.jobs: self.file_list.addItem(f"[{j.status}] {j.path.name}" + (f" → {j.proposed_name}" if j.proposed_name else ""))
        self.file_list.blockSignals(False)
        if self.jobs and selected >= 0: self.file_list.setCurrentRow(min(selected, len(self.jobs)-1))

    @staticmethod
    def norm(s): return re.sub(r"\s+", " ", s.strip()).casefold()
    @staticmethod
    def union(words): return fitz.Rect(min(w[0] for w in words), min(w[1] for w in words), max(w[2] for w in words), max(w[3] for w in words))

    @staticmethod
    def find_span(words, target):
        if len(target) > len(words): return None
        for i in range(len(words)-len(target)+1):
            if words[i:i+len(target)] == target: return i, i+len(target)
        a = [re.sub(r"[:：]+$", "", x) for x in words]; b = [re.sub(r"[:：]+$", "", x) for x in target]
        for i in range(len(a)-len(b)+1):
            if a[i:i+len(b)] == b: return i, i+len(b)
        return None

    def ocr_page_words(self, page):
        if not self.tesseract_cmd: raise RuntimeError("Tesseract OCR engine not found")
        dpi = self.ocr_dpi.value(); scale = dpi / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        data = pytesseract.image_to_data(image, lang=self.ocr_lang.text().strip() or "eng", output_type=pytesseract.Output.DICT, config="--psm 6")
        out = []
        for i, text in enumerate(data["text"]):
            text = text.strip()
            try: conf = float(data["conf"][i])
            except Exception: conf = -1
            if not text or conf < 35: continue
            x, y, w, h = data["left"][i]/scale, data["top"][i]/scale, data["width"][i]/scale, data["height"][i]/scale
            out.append((x, y, x+w, y+h, text, int(data["block_num"][i]), int(data["line_num"][i]), int(data["word_num"][i])))
        return out

    def page_words(self, page_index, allow_ocr=True):
        page = self.doc[page_index] if self.doc else None
        if page is None: return [], False
        words = page.get_text("words", sort=True)
        if words: return words, False
        if not allow_ocr or not self.ocr_enabled.isChecked(): return [], False
        job = self.jobs[self.current_job] if 0 <= self.current_job < len(self.jobs) else None
        if job and page_index in job.ocr_words: return job.ocr_words[page_index], True
        words = self.ocr_page_words(page)
        if job is not None: job.ocr_words[page_index] = words
        return words, True

    def take_value_words(self, words, labels):
        words = list(words)
        while words and words[0][4].strip() in {":", "：", "-", "–", "—", "="}: words.pop(0)
        chosen, previous_x1 = [], None
        for w in words:
            text = w[4].strip(); normalized = re.sub(r"[:：]+$", "", self.norm(text))
            if normalized in labels or (text.endswith((":", "：")) and not chosen):
                if chosen: break
                return []
            if previous_x1 is not None and w[0]-previous_x1 > 32: break
            chosen.append(w); previous_x1 = w[2]
            if len(chosen) >= 12: break
        return chosen

    def find_value_in_words(self, words_by_page, keyword, configured_keywords=None, ocr_pages=None):
        target = self.norm(keyword).split(); labels = {re.sub(r"[:：]+$", "", self.norm(k)) for k in (configured_keywords or []) if k.strip()}
        ocr_pages = ocr_pages or set()
        for page_index, words in words_by_page.items():
            lines = {}
            for w in words: lines.setdefault((int(w[5]), int(w[6])), []).append(w)
            for lw in lines.values():
                lw.sort(key=lambda w: w[7]); span = self.find_span([self.norm(w[4]) for w in lw], target)
                if span is None: continue
                start, end = span; kw = lw[start:end]; kr = self.union(kw); cy = (kr.y0+kr.y1)/2; kh = max(kr.height, 1.0)
                right = []
                for w in words:
                    r = fitz.Rect(w[0], w[1], w[2], w[3])
                    if r.intersects(kr) and r.get_area() > 0: continue
                    if r.x0 < kr.x1-1 or r.x0-kr.x1 > 320: continue
                    if abs(((r.y0+r.y1)/2)-cy) > max(5.0, kh*.75): continue
                    right.append(w)
                right.sort(key=lambda w: w[0]); value_words = self.take_value_words(right, labels)
                confidence = "OCR HIGH" if page_index in ocr_pages else "HIGH"
                if value_words: return MatchResult(keyword, " ".join(w[4] for w in value_words).strip(), page_index, kr, self.union(value_words), confidence)
                candidates = []
                for other in lines.values():
                    other = sorted(other, key=lambda w: w[0]); rr = self.union(other); gap = rr.y0-kr.y1
                    if gap < 0 or gap > 120: continue
                    hd = min(abs(rr.x0-kr.x0), abs(rr.x0-kr.x1))
                    if hd > 180: continue
                    vw = self.take_value_words(other, labels)
                    if vw: candidates.append((gap+min(hd*.25,60), vw))
                if candidates:
                    _, vw = min(candidates, key=lambda x:x[0]); confidence = "OCR MEDIUM" if page_index in ocr_pages else "MEDIUM"
                    return MatchResult(keyword, " ".join(w[4] for w in vw).strip(), page_index, kr, self.union(vw), confidence)
        return None

    def analyze_all(self):
        keys = self.keywords()
        if not self.jobs: return QMessageBox.information(self, "No PDFs", "Select a folder first.")
        if any(not k for k in keys): return QMessageBox.warning(self, "Missing keyword", "Fill every keyword row.")
        self.cancel_manual_pick(); QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for idx, job in enumerate(self.jobs):
                self.status.setText(f"Analyzing {idx+1}/{len(self.jobs)}: {job.path.name}"); QApplication.processEvents()
                job.ocr_words.clear()
                try:
                    with fitz.open(str(job.path)) as d:
                        words_by_page, ocr_pages = {}, set()
                        for pi in range(d.page_count):
                            page = d[pi]; words = page.get_text("words", sort=True)
                            if not words and self.ocr_enabled.isChecked():
                                if not self.tesseract_cmd:
                                    job.status = "OCR NEEDED"; job.error = "Tesseract OCR engine not found"; continue
                                self.status.setText(f"OCR {idx+1}/{len(self.jobs)} page {pi+1}/{d.page_count}: {job.path.name}"); QApplication.processEvents()
                                words = self.ocr_page_words(page); job.ocr_words[pi] = words; ocr_pages.add(pi)
                            words_by_page[pi] = words
                        if not any(words_by_page.values()):
                            job.status = "OCR NEEDED" if not self.tesseract_cmd else "REVIEW"; job.error = "No readable text found"; job.results = None; job.proposed_name = ""; continue
                        job.results = [self.find_value_in_words(words_by_page, k, keys, ocr_pages) for k in keys]
                        job.proposed_name = self.proposed(job.results); self.recalculate_job_status(job)
                except Exception as e:
                    job.status = "ERROR"; job.error = str(e); job.results = None; job.proposed_name = ""
        finally: QApplication.restoreOverrideCursor()
        self.refresh_list(); self.status.setText("Analysis complete.")
        if self.current_job >= 0: self.select_job(self.current_job)

    def start_manual_pick(self):
        if not self.doc or self.current_job < 0: return QMessageBox.information(self, "No PDF selected", "Select a PDF first.")
        row = self.results.currentRow()
        if row < 0: return QMessageBox.information(self, "Select a field", "Select the keyword/value row first.")
        self.manual_row = row; self.manual_stage = "tag"; self.manual_tag_rect = None; self.manual_tag_page = None
        self.status.setText(f"Manual mapping for '{self.keywords()[row]}': click the TAG in the PDF."); self.manual_pick_btn.setText("Picking tag…")

    def cancel_manual_pick(self):
        self.manual_row = -1; self.manual_stage = None; self.manual_tag_rect = None; self.manual_tag_page = None
        if hasattr(self, "manual_pick_btn"): self.manual_pick_btn.setText("Manual Pick Tag → Value")

    def words_for_manual(self, page_index):
        words, _ = self.page_words(page_index, True); return words

    def word_at_point(self, words, x, y, tolerance=3.0):
        p = fitz.Point(x,y); direct=[]
        for w in words:
            r=fitz.Rect(w[0],w[1],w[2],w[3]); ex=fitz.Rect(r.x0-tolerance,r.y0-tolerance,r.x1+tolerance,r.y1+tolerance)
            if p in ex: direct.append((r.get_area(),w))
        if direct: return min(direct,key=lambda z:z[0])[1]
        nearby=[]
        for w in words:
            r=fitz.Rect(w[0],w[1],w[2],w[3]); cx=min(max(x,r.x0),r.x1); cy=min(max(y,r.y0),r.y1); d=(x-cx)**2+(y-cy)**2
            if d<=100: nearby.append((d,w))
        return min(nearby,key=lambda z:z[0])[1] if nearby else None

    def pdf_clicked(self, x, y):
        if self.manual_stage not in {"tag","value"} or not self.doc: return
        words = self.words_for_manual(self.current_page)
        if not words: self.status.setText("No text/OCR text available on this page."); return
        clicked = self.word_at_point(words,x,y)
        if not clicked: self.status.setText("No text found at that point."); return
        if self.manual_stage == "tag":
            self.manual_tag_rect = fitz.Rect(clicked[0],clicked[1],clicked[2],clicked[3]); self.manual_tag_page=self.current_page; self.manual_stage="value"; self.render_page(self.manual_tag_rect,None); self.manual_pick_btn.setText("Picking value…"); self.status.setText("Tag selected. Now click the value."); return
        if self.manual_tag_page != self.current_page: self.cancel_manual_pick(); return
        line = [w for w in words if int(w[5])==int(clicked[5]) and int(w[6])==int(clicked[6])]; line.sort(key=lambda w:w[7]); start=next((i for i,w in enumerate(line) if int(w[7])==int(clicked[7])),0)
        value_words=[]; prev=None
        for w in line[start:]:
            if prev is not None and w[0]-prev>32: break
            value_words.append(w); prev=w[2]
            if len(value_words)>=12: break
        value=" ".join(w[4] for w in value_words).strip(); vr=self.union(value_words); keyword=self.keywords()[self.manual_row]; job=self.jobs[self.current_job]
        if job.results is None or len(job.results)!=len(self.keywords()): job.results=[None]*len(self.keywords())
        result=MatchResult(keyword,value,self.current_page,self.manual_tag_rect,vr,"MANUAL OCR" if self.current_page in job.ocr_words else "MANUAL")
        row=self.manual_row; job.results[row]=result; job.proposed_name=self.proposed(job.results); self.recalculate_job_status(job); self.cancel_manual_pick(); self.refresh_list(); self.populate_current_job(); self.results.selectRow(row); self.show_result(result); self.status.setText(f"Manual mapping saved: {keyword} → {value}")

    def clear_selected_result(self):
        if self.current_job < 0: return
        row=self.results.currentRow(); job=self.jobs[self.current_job]
        if row>=0 and job.results and row<len(job.results): job.results[row]=None; job.proposed_name=self.proposed(job.results); self.recalculate_job_status(job); self.refresh_list(); self.populate_current_job(); self.results.selectRow(row)

    @staticmethod
    def safe(text):
        text=re.sub(r'[<>:"/\\|?*\x00-\x1f]',"_",text); return re.sub(r"\s+"," ",text).strip().rstrip(". ")[:120]

    def proposed(self, results):
        if not results or any(r is None for r in results): return ""
        parts=[self.safe(r.value) for r in results]; return self.separator.text().join(parts)+".pdf" if all(parts) else ""

    def recalculate_job_status(self, job):
        if not job.results or any(r is None for r in job.results): job.status="REVIEW"; job.error="One or more values were not found"; return
        job.proposed_name=self.proposed(job.results)
        if not job.proposed_name: job.status="REVIEW"; job.error="Generated filename is empty"; return
        dest=job.path.with_name(job.proposed_name)
        if dest.exists() and dest!=job.path: job.status="CONFLICT"; job.error="Destination already exists"
        else: job.status="PASS"; job.error=""

    def refresh_current_filename(self):
        if 0<=self.current_job<len(self.jobs) and self.jobs[self.current_job].results:
            job=self.jobs[self.current_job]; job.proposed_name=self.proposed(job.results); self.recalculate_job_status(job); self.filename.setText(job.proposed_name); self.refresh_list()

    def select_job(self, row):
        if row<0 or row>=len(self.jobs): return
        self.cancel_manual_pick(); self.current_job=row; self.preview_rotation=0; self.open_doc(self.jobs[row].path); self.populate_current_job()

    def populate_current_job(self):
        if self.current_job<0: return
        job=self.jobs[self.current_job]; self.selected.setText(f"{job.path.name}\nStatus: {job.status}"+(f"\n{job.error}" if job.error else "")); self.filename.setText(job.proposed_name); keys=self.keywords(); self.results.setRowCount(len(keys))
        for i,k in enumerate(keys):
            r=job.results[i] if job.results and i<len(job.results) else None; vals=[str(i+1),k,r.value if r else "NOT FOUND",r.confidence if r else "-",str(r.page_index+1) if r else "-"]
            for c,v in enumerate(vals):
                it=QTableWidgetItem(v); it.setFlags(it.flags() & ~Qt.ItemIsEditable); self.results.setItem(i,c,it)
        if job.results:
            for i,r in enumerate(job.results):
                if r: self.results.selectRow(i); self.show_result(r); break
        else: self.render_page()

    def open_doc(self,path):
        if self.doc: self.doc.close()
        self.doc=fitz.open(str(path)); self.current_page=0; self.render_page()

    def render_page(self, kr=None, vr=None):
        if not self.doc: return
        page=self.doc[self.current_page]; pix=page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False); image=QImage(pix.samples,pix.width,pix.height,pix.stride,QImage.Format_RGB888).copy(); self.canvas.set_page(QPixmap.fromImage(image),page.rect.width,page.rect.height,kr,vr,self.preview_rotation); self.page_label.setText(f"Page {self.current_page+1} / {self.doc.page_count}"); self.rotation_label.setText(f"Rotation: {self.preview_rotation}° (preview only)")

    def rotate_preview(self,delta):
        if not self.doc: return
        self.preview_rotation=(self.preview_rotation+delta)%360; self.render_page()
    def reset_rotation(self): self.preview_rotation=0; self.render_page()
    def result_selected(self):
        if self.current_job<0:return
        row=self.results.currentRow(); job=self.jobs[self.current_job]
        if job.results and 0<=row<len(job.results) and job.results[row]: self.show_result(job.results[row])
    def show_result(self,r): self.current_page=r.page_index; self.render_page(r.keyword_rect,r.value_rect)
    def prev_page(self):
        if self.doc: self.current_page=max(0,self.current_page-1); self.render_page()
    def next_page(self):
        if self.doc: self.current_page=min(self.doc.page_count-1,self.current_page+1); self.render_page()

    def rename_passed(self):
        passed=[j for j in self.jobs if j.status=="PASS"]
        if not passed:return QMessageBox.information(self,"Nothing to rename","No PASS files.")
        if QMessageBox.question(self,"Confirm",f"Rename {len(passed)} PDF(s)?",QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        self.cancel_manual_pick()
        if self.doc:self.doc.close();self.doc=None
        renamed=0
        for j in passed:
            try:
                dest=j.path.with_name(j.proposed_name)
                if dest.exists() and dest!=j.path:j.status="CONFLICT";continue
                if dest!=j.path:j.path.rename(dest);j.path=dest
                j.status="RENAMED";renamed+=1
            except Exception as e:j.status="ERROR";j.error=str(e)
        self.refresh_list();self.status.setText(f"Renamed {renamed} PDF(s).")


def main():
    app=QApplication(sys.argv); w=MainWindow(); w.show(); raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
