"""Testes dos renderizadores de documentos (Markdown -> HTML/PDF/DOCX)."""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

from pypdf import PdfReader

from src.skills.native import render
from src.skills.native.render import Block, parse_markdown

MD = """# Título Principal

Primeiro parágrafo
continua aqui.

## Seção

- item um
* item dois
1. item três

```python
x = 1
  y = 2
```

### Sub

Texto final com <tags> & símbolos (ação).
"""


def test_parse_markdown_blocks():
    blocks = parse_markdown(MD)
    assert blocks[0] == Block("h1", "Título Principal")
    assert blocks[1] == Block("p", "Primeiro parágrafo continua aqui.")
    assert blocks[2] == Block("h2", "Seção")
    assert [b.text for b in blocks if b.kind == "li"] == ["item um", "item dois", "item três"]
    assert Block("code", "x = 1\n  y = 2") in blocks
    assert blocks[-2] == Block("h3", "Sub")
    assert blocks[-1].kind == "p"


def test_parse_markdown_unclosed_fence_and_empty():
    assert parse_markdown("") == []
    assert parse_markdown("```\ncodigo solto") == [Block("code", "codigo solto")]
    assert parse_markdown("texto\n```\na\n```\nfim") == [
        Block("p", "texto"),
        Block("code", "a"),
        Block("p", "fim"),
    ]


def test_to_html_escapes_and_structures():
    html = render.to_html(parse_markdown(MD), "Meu <Doc>").decode("utf-8")
    assert "<title>Meu &lt;Doc&gt;</title>" in html
    assert "<h1>Título Principal</h1>" in html and "<h2>Seção</h2>" in html
    assert html.count("<ul>") == 1 and html.count("<li>") == 3 and "</ul>" in html
    assert "<pre><code>x = 1\n  y = 2</code></pre>" in html
    assert "&lt;tags&gt; &amp; símbolos" in html
    assert "<tags>" not in html


def test_to_html_closes_trailing_list():
    html = render.to_html([Block("li", "a"), Block("li", "b")]).decode("utf-8")
    assert html.index("</ul>") > html.index("<li>b</li>")


def test_to_pdf_is_valid_and_text_is_extractable():
    data = render.to_pdf(parse_markdown(MD), "Relatório")
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "Relatório" in text and "Título Principal" in text
    assert "ação" in text and "(ação)" in text  # acentos e parênteses escapados
    assert "- item um" in text and "x = 1" in text


def test_to_pdf_paginates_long_documents():
    blocks = [Block("p", f"linha {i} " + "palavra " * 30) for i in range(120)]
    reader = PdfReader(io.BytesIO(render.to_pdf(blocks)))
    assert len(reader.pages) > 1
    last = reader.pages[-1].extract_text()
    assert "linha 119" in last


def test_to_pdf_handles_empty_and_unencodable_text():
    assert len(PdfReader(io.BytesIO(render.to_pdf([]))).pages) == 1
    data = render.to_pdf([Block("p", "emoji 😀 e barra \\ final"), Block("code", "")])
    assert "barra" in PdfReader(io.BytesIO(data)).pages[0].extract_text()


def test_to_docx_structure_and_text():
    data = render.to_docx(parse_markdown(MD), "Relatório")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        assert {
            "[Content_Types].xml", "_rels/.rels", "word/document.xml",
            "word/styles.xml", "word/_rels/document.xml.rels",
        } <= set(archive.namelist())  # fmt: skip
        for name in archive.namelist():
            ET.fromstring(archive.read(name))  # todo XML é bem formado
        document = archive.read("word/document.xml").decode("utf-8")
    assert 'w:pStyle w:val="Heading1"' in document and 'w:val="Heading3"' in document
    assert 'w:val="ListBullet"' in document and 'w:val="Code"' in document
    text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", document))
    assert "Relatório" in text and "• item um" in text and "&lt;tags&gt;" in text
    assert "<w:br/>" in document  # código multilinha
