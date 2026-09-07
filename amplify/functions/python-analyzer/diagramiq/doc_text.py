"""Reading the ⬆ Notes / Document channel: .txt, .md, .docx, .pdf.

Two jobs, and the second is why this file is more than a text extractor.

A meeting transcript is prose, and flattening it to a stream of lines loses
nothing. An SOP is not prose: its procedure lives in a numbered clause list,
its roles live in a responsibilities table, its decision criteria live in
thresholds inside a cell, and its flow is often drawn in a figure pasted into
the middle of the document. Flatten that and you hand the model a bag of
disconnected phrases — "Approve" and "Finance Manager" and "£5,000" with
nothing to say they belong to the same row.

So .docx is walked in document order and re-emitted as light markup: headings
as `#`, list items as `-`, tables as pipe rows with their header, and each
embedded figure as a numbered marker where it actually sat. The figures
themselves come back alongside the text so the vision pass can read them.

The desktop app reads Word via python-docx and PDF via pdfplumber. Neither
suits a zip Lambda — python-docx pulls in lxml and pdfplumber pulls in Pillow,
both platform-specific binaries. A .docx is a zip of XML, so Word is handled
here with the standard library, and PDF uses pypdf, which is pure Python.
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
PKG_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'

SUPPORTED = ('.txt', '.md', '.text', '.docx', '.pdf')

# Bedrock takes png, jpeg, gif and webp. Word also embeds emf/wmf (pasted
# Visio and Office drawings) and x-emf thumbnails, which it cannot read.
IMAGE_TYPES = {'png': 'png', 'jpg': 'jpeg', 'jpeg': 'jpeg', 'gif': 'gif', 'webp': 'webp'}
MAX_FIGURES = 4
MAX_FIGURE_BYTES = 3_500_000
MIN_FIGURE_BYTES = 6_000        # smaller than this is a logo or a bullet glyph


class UnreadableDocument(Exception):
    """Raised with a message intended for the user, not the log."""


@dataclass
class Figure:
    """An image embedded in the document, with where it appeared."""
    name: str
    fmt: str                     # png | jpeg | gif | webp
    data: bytes
    marker: str                  # the text marker written in its place


@dataclass
class Document:
    """What a source document turned out to contain."""
    text: str
    figures: list[Figure] = field(default_factory=list)
    headings: int = 0
    tables: int = 0
    skipped_figures: int = 0     # embedded, but in a format Bedrock cannot read

    @property
    def summary(self) -> dict:
        """Counts worth showing the user, so they can see what was read."""
        return {
            'headings': self.headings,
            'tables': self.tables,
            'figures': len(self.figures),
            'skippedFigures': self.skipped_figures,
            'characters': len(self.text),
        }


# ── .docx ────────────────────────────────────────────────────────────────────

def _rel_targets(zf: zipfile.ZipFile) -> dict:
    """rId -> the part it points at, from word/_rels/document.xml.rels."""
    try:
        rels = ET.fromstring(zf.read('word/_rels/document.xml.rels'))
    except (KeyError, ET.ParseError):
        return {}
    out = {}
    for rel in rels.iter(f'{PKG_REL}Relationship'):
        rid, target = rel.get('Id'), rel.get('Target') or ''
        if rid and target:
            out[rid] = target.split('/')[-1]
        # Targets are usually 'media/image1.png'; the basename is enough to
        # find the part, since everything embedded lives in word/media.
    return out


def _para_text(p: ET.Element) -> str:
    """Run text for one paragraph, with tabs kept as separators."""
    buf = []
    for node in p.iter():
        if node.tag == f'{W}t' and node.text:
            buf.append(node.text)
        elif node.tag == f'{W}tab':
            buf.append('\t')
        elif node.tag in (f'{W}br', f'{W}cr'):
            buf.append(' ')
    return ''.join(buf).strip()


def _style_of(p: ET.Element) -> str:
    ppr = p.find(f'{W}pPr')
    if ppr is None:
        return ''
    style = ppr.find(f'{W}pStyle')
    return (style.get(f'{W}val') or '') if style is not None else ''


def _is_list_item(p: ET.Element) -> bool:
    ppr = p.find(f'{W}pPr')
    return ppr is not None and ppr.find(f'{W}numPr') is not None


def _heading_level(style: str) -> int:
    """Heading1/Heading 2/heading3 -> 1..6, else 0."""
    m = re.match(r'heading\s*([1-6])$', style.strip(), re.I)
    return int(m.group(1)) if m else 0


def _blip_rels(p: ET.Element) -> list:
    """Relationship ids of every image drawn in this paragraph."""
    return [rid for blip in p.iter(f'{A}blip')
            if (rid := blip.get(f'{R}embed'))]


def _collect_figure(zf, rels, rid, index, doc: Document) -> str | None:
    """Pull one embedded image out of the zip. Returns its marker, or None."""
    target = rels.get(rid)
    if not target:
        return None
    ext = target.rsplit('.', 1)[-1].lower() if '.' in target else ''
    fmt = IMAGE_TYPES.get(ext)
    if not fmt:
        # emf/wmf: a pasted Office drawing. It is genuinely in the document,
        # so it is counted rather than ignored — the user should know the
        # figure was seen and skipped, not silently dropped.
        doc.skipped_figures += 1
        return None
    if len(doc.figures) >= MAX_FIGURES:
        doc.skipped_figures += 1
        return None
    try:
        data = zf.read(f'word/media/{target}')
    except KeyError:
        return None
    if not (MIN_FIGURE_BYTES <= len(data) <= MAX_FIGURE_BYTES):
        return None

    marker = f'[Figure {index}]'
    doc.figures.append(Figure(name=target, fmt=fmt, data=data, marker=marker))
    return marker


def _table_lines(tbl: ET.Element) -> list:
    """A table as pipe rows, header separated — the shape the model reads best."""
    rows = []
    for tr in tbl.findall(f'{W}tr'):
        cells = []
        for tc in tr.findall(f'{W}tc'):
            parts = [_para_text(p) for p in tc.findall(f'{W}p')]
            cells.append(' '.join(x for x in parts if x).strip())
        if any(cells):
            rows.append(cells)
    if not rows:
        return []

    width = max(len(r) for r in rows)
    out = []
    for i, r in enumerate(rows):
        r = r + [''] * (width - len(r))
        out.append('| ' + ' | '.join(r) + ' |')
        if i == 0:
            out.append('|' + '|'.join([' --- '] * width) + '|')
    return out


def _from_docx(data: bytes) -> Document:
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
    body = root.find(f'{W}body')
    if body is None:
        raise UnreadableDocument('That .docx has no readable document body.')

    rels = _rel_targets(zf)
    doc = Document(text='')
    lines: list = []
    fig_no = 0

    # Document order matters: a figure means "the flow at this point in the
    # procedure", and a table means "these cells belong to each other".
    for el in body:
        if el.tag == f'{W}p':
            text = _para_text(el)
            markers = []
            for rid in _blip_rels(el):
                fig_no += 1
                marker = _collect_figure(zf, rels, rid, fig_no, doc)
                if marker:
                    markers.append(marker)
                else:
                    fig_no -= 1
            if markers:
                lines.append(' '.join(markers) + (f' {text}' if text else ''))
                continue
            if not text:
                continue
            level = _heading_level(_style_of(el))
            if level:
                doc.headings += 1
                lines.append('')
                lines.append('#' * level + ' ' + text)
            elif _is_list_item(el):
                lines.append('- ' + text)
            else:
                lines.append(text)

        elif el.tag == f'{W}tbl':
            rows = _table_lines(el)
            if rows:
                doc.tables += 1
                lines.append('')
                lines.extend(rows)
                lines.append('')

    if not any(l.strip() for l in lines):
        if doc.figures or doc.skipped_figures:
            raise UnreadableDocument(
                'That Word document contains only images. Export the diagram as a '
                'PNG and use ⬆ Image, or add the steps as text.')
        raise UnreadableDocument('No text found in that Word document.')

    doc.text = '\n'.join(lines)
    return doc


# ── .pdf ─────────────────────────────────────────────────────────────────────

def _from_pdf(data: bytes) -> Document:
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
            'No selectable text in that PDF — it is probably a scan. Run OCR on it, '
            'or save the source as .docx.')

    # Raster figures are deliberately not pulled out of PDFs: decoding an
    # arbitrary embedded image needs Pillow, which cannot be vendored here. A
    # vector flowchart is the common case anyway, and its labels come through
    # as text above, which is the part that carries the process.
    return Document(text='\n'.join(pages))


# ── entry points ─────────────────────────────────────────────────────────────

def extract_document(data: bytes, filename: str) -> Document:
    """Read a source document. Raises UnreadableDocument on failure."""
    ext = ('.' + filename.rsplit('.', 1)[-1].lower()) if '.' in filename else ''

    if ext in ('.txt', '.md', '.text', ''):
        doc = Document(text=data.decode('utf-8', errors='replace'))
    elif ext == '.docx':
        doc = _from_docx(data)
    elif ext == '.pdf':
        doc = _from_pdf(data)
    elif ext == '.doc':
        raise UnreadableDocument(
            'Legacy .doc is not supported. Save the file as .docx and retry.')
    elif ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        raise UnreadableDocument(
            'That is an image, not a document. Use ⬆ Image to read a diagram '
            'picture.')
    else:
        raise UnreadableDocument(
            f"Unsupported file type '{ext}'. Upload a .txt, .md, .docx or .pdf.")

    doc.text = re.sub(r'\n{3,}', '\n\n', doc.text).strip()
    if not doc.text:
        raise UnreadableDocument('That file appears to be empty.')
    return doc


def extract_text(data: bytes, filename: str) -> str:
    """Text only, for callers that do not care about figures or structure."""
    return extract_document(data, filename).text
