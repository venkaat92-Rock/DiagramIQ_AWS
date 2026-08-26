"""Text extraction for the ⬆ Notes channel: .txt, .md, .docx, .pdf.

The desktop app reads Word via python-docx and PDF via pdfplumber. Neither
suits a zip Lambda — python-docx pulls in lxml and pdfplumber pulls in Pillow,
both of which ship as platform-specific binaries. A .docx is just a zip of XML,
so Word is handled here with the standard library, and PDF uses pypdf, which is
pure Python.
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
SUPPORTED = ('.txt', '.md', '.text', '.docx', '.pdf')


class UnreadableDocument(Exception):
    """Raised with a message intended for the user, not the log."""


def _from_docx(data: bytes) -> str:
    """Paragraph text from a .docx, tables included.

    Table cells contain their own <w:p>, so walking every paragraph in document
    order covers body text and tables alike.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UnreadableDocument(
            'That file is not a valid .docx. If it is an older .doc, save it as '
            '.docx first.')
    try:
        xml = zf.read('word/document.xml')
    except KeyError:
        raise UnreadableDocument('That .docx has no readable document body.')

    root = ET.fromstring(xml)
    paras = []
    for p in root.iter(f'{W}p'):
        # w:t holds the runs; w:tab and w:br are separators worth preserving.
        buf = []
        for node in p.iter():
            if node.tag == f'{W}t' and node.text:
                buf.append(node.text)
            elif node.tag in (f'{W}tab',):
                buf.append('\t')
        line = ''.join(buf).strip()
        if line:
            paras.append(line)
    if not paras:
        raise UnreadableDocument(
            'No text found in that Word document — it may contain only images.')
    return '\n'.join(paras)


def _from_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        # Not just ImportError: pypdf probes optional crypto backends at import
        # time, and a broken one raises something else entirely. Either way the
        # user gets a clear message rather than a 500.
        raise UnreadableDocument(
            f'PDF support is unavailable in this deployment ({type(exc).__name__}). '
            'Convert the file to .docx or .txt and retry.')

    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, 'is_encrypted', False):
            try:
                reader.decrypt('')          # some PDFs carry an empty password
            except Exception:
                raise UnreadableDocument(
                    'That PDF is password-protected. Remove the password and retry.')
        pages = [t for t in (pg.extract_text() or '' for pg in reader.pages) if t.strip()]
    except UnreadableDocument:
        raise
    except Exception as exc:
        raise UnreadableDocument(f'Could not read that PDF: {exc}')

    if not pages:
        raise UnreadableDocument(
            'No selectable text in that PDF — it is probably a scan. Run OCR on it first.')
    return '\n'.join(pages)


def extract_text(data: bytes, filename: str) -> str:
    """Dispatch on the file extension. Raises UnreadableDocument on failure."""
    ext = ('.' + filename.rsplit('.', 1)[-1].lower()) if '.' in filename else ''

    if ext in ('.txt', '.md', '.text', ''):
        text = data.decode('utf-8', errors='replace')
    elif ext == '.docx':
        text = _from_docx(data)
    elif ext == '.pdf':
        text = _from_pdf(data)
    elif ext == '.doc':
        raise UnreadableDocument(
            'Legacy .doc is not supported. Save the file as .docx and retry.')
    else:
        raise UnreadableDocument(
            f"Unsupported file type '{ext}'. Upload a .txt, .md, .docx or .pdf.")

    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if not text:
        raise UnreadableDocument('That file appears to be empty.')
    return text
