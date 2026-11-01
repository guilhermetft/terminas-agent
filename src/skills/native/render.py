"""Renderizadores de documentos sem dependências externas: Markdown -> HTML, PDF e DOCX."""

from __future__ import annotations

import html
import io
import re
import textwrap
import zipfile
from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    kind: str  # h1 | h2 | h3 | p | li | code
    text: str


_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$")


def parse_markdown(text: str) -> list[Block]:
    """Subconjunto de Markdown: títulos (#..###), listas, blocos ``` e parágrafos."""
    blocks: list[Block] = []
    paragraph: list[str] = []
    code: list[str] | None = None

    def flush() -> None:
        if paragraph:
            blocks.append(Block("p", " ".join(paragraph)))
            paragraph.clear()

    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            if code is None:
                flush()
                code = []
            else:
                blocks.append(Block("code", "\n".join(code)))
                code = None
            continue
        if code is not None:
            code.append(raw.rstrip("\n"))
            continue
        if not line.strip():
            flush()
        elif m := _HEADING.match(line):
            flush()
            blocks.append(Block(f"h{len(m.group(1))}", m.group(2).strip()))
        elif m := _BULLET.match(line):
            flush()
            blocks.append(Block("li", m.group(1).strip()))
        else:
            paragraph.append(line.strip())
    if code is not None:  # cerca não fechada
        blocks.append(Block("code", "\n".join(code)))
    flush()
    return blocks


# ------------------------------------------------------------------------------------ HTML
def to_html(blocks: list[Block], title: str = "") -> bytes:
    body: list[str] = []
    in_list = False
    for block in blocks:
        if block.kind != "li" and in_list:
            body.append("</ul>")
            in_list = False
        text = html.escape(block.text)
        if block.kind == "li":
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{text}</li>")
        elif block.kind == "code":
            body.append(f"<pre><code>{text}</code></pre>")
        elif block.kind == "p":
            body.append(f"<p>{text}</p>")
        else:
            body.append(f"<{block.kind}>{text}</{block.kind}>")
    if in_list:
        body.append("</ul>")
    page = (
        '<!doctype html>\n<html lang="pt-BR">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n</head>\n<body>\n" + "\n".join(body) + "\n</body>\n</html>\n"
    )
    return page.encode("utf-8")


# ------------------------------------------------------------------------------------- PDF
_PAGE_W, _PAGE_H, _MARGIN = 595, 842, 56
_STYLE = {  # kind -> (fonte, tamanho, espaço depois)
    "h1": ("F2", 20, 10), "h2": ("F2", 15, 8), "h3": ("F2", 12, 6),
    "p": ("F1", 11, 6), "li": ("F1", 11, 2), "code": ("F1", 9, 6),
}  # fmt: skip
_AVG_CHAR = 0.52  # largura média do caractere Helvetica, em múltiplos do corpo


def _pdf_escape(text: str) -> bytes:
    raw = text.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def to_pdf(blocks: list[Block], title: str = "") -> bytes:
    """PDF de texto (Helvetica, A4, quebra de linha e paginação automáticas)."""
    usable_w = _PAGE_W - 2 * _MARGIN
    pages: list[list[bytes]] = [[]]
    y = _PAGE_H - _MARGIN

    items = ([Block("h1", title)] if title else []) + blocks
    for block in items:
        font, size, after = _STYLE[block.kind]
        width = max(10, int(usable_w / (_AVG_CHAR * size)))
        prefix = "- " if block.kind == "li" else ""
        if block.kind == "code":
            lines = [ln for raw in block.text.split("\n") for ln in (textwrap.wrap(raw, width) or [""])]
        else:
            lines = textwrap.wrap(prefix + block.text, width) or [""]
        leading = size * 1.35
        for line in lines:
            if y - leading < _MARGIN:
                pages.append([])
                y = _PAGE_H - _MARGIN
            y -= leading
            pages[-1].append(
                b"BT /%s %d Tf %d %.1f Td (" % (font.encode(), size, _MARGIN, y) + _pdf_escape(line) + b") Tj ET"
            )
        y -= after

    objects: list[bytes] = []  # índice 0 = objeto 1
    page_ids = [5 + 2 * i for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects.append(b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % len(pages))
    for font_name in (b"Helvetica", b"Helvetica-Bold"):
        objects.append(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /" + font_name + b" /Encoding /WinAnsiEncoding >>"
        )
    for number, commands in enumerate(pages):
        stream = b"\n".join(commands)
        page_id = page_ids[number]
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
            b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>" % (_PAGE_W, _PAGE_H, page_id + 1)
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


# ------------------------------------------------------------------------------------ DOCX
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    "</Relationships>"
)


def _style(style_id: str, name: str, size_half_points: int, bold: bool, mono: bool = False) -> str:
    fonts = '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>' if mono else ""
    return (
        f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>'
        f'<w:basedOn w:val="Normal"/><w:rPr>{fonts}{"<w:b/>" if bold else ""}'
        f'<w:sz w:val="{size_half_points}"/></w:rPr></w:style>'
    )


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles xmlns:w="{_W}">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    '<w:rPr><w:sz w:val="22"/></w:rPr></w:style>'
    + _style("Heading1", "heading 1", 40, True)
    + _style("Heading2", "heading 2", 30, True)
    + _style("Heading3", "heading 3", 24, True)
    + _style("ListBullet", "List Bullet", 22, False)
    + _style("Code", "Code", 18, False, mono=True)
    + "</w:styles>"
)
_DOCX_STYLE = {"h1": "Heading1", "h2": "Heading2", "h3": "Heading3", "li": "ListBullet", "code": "Code", "p": "Normal"}


def to_docx(blocks: list[Block], title: str = "") -> bytes:
    """DOCX mínimo (OOXML via zipfile): títulos, parágrafos, listas e código."""
    paragraphs: list[str] = []
    for block in ([Block("h1", title)] if title else []) + blocks:
        text = block.text
        if block.kind == "li":
            text = "• " + text
        runs = []
        for number, line in enumerate(text.split("\n")):
            if number:
                runs.append("<w:br/>")
            runs.append(f'<w:t xml:space="preserve">{html.escape(line, quote=False)}</w:t>')
        paragraphs.append(
            f'<w:p><w:pPr><w:pStyle w:val="{_DOCX_STYLE[block.kind]}"/></w:pPr><w:r>{"".join(runs)}</w:r></w:p>'
        )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{"".join(paragraphs)}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        archive.writestr("word/styles.xml", _STYLES)
    return buffer.getvalue()
