# PDF Renamer

Windows desktop batch utility for renaming PDF files from values extracted using user-defined keywords.

## Features

- Select a directory and scan all PDF files.
- Configure how many keyword/value fields to extract.
- Case-insensitive keyword matching.
- User-controlled filename order.
- Analyze all PDFs with one rule set.
- Preview selected PDFs.
- Highlight matched keyword in yellow and extracted value in green.
- Show proposed filename before rename.
- Statuses: PENDING, PASS, REVIEW, CONFLICT, OCR NEEDED, ERROR, RENAMED.
- Rename only PASS files.
- Never silently overwrite an existing file.

## Extraction strategy

For each keyword the app searches every PDF page, first preferring text on the same line to the right of the keyword, then falling back to the nearest plausible line below it.

The first MVP supports text-based PDFs. Image-only/scanned PDFs are marked `OCR NEEDED` rather than renamed using unreliable guesses.

## Run on Windows

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

## Build EXE

```powershell
pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --onefile --name PDFRenamer app.py
```

Output:

```text
dist\PDFRenamer.exe
```

## GitHub Actions

The included workflow builds a Windows executable and uploads it as a workflow artifact.
