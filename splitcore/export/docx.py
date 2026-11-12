"""Word (.docx) export: one page per tile.

Each tile is an ordinary picture *in the line of text* (the way Word itself
inserts pictures), alone in its paragraph, and every paragraph starts a new
page.  The page margins are set so that the picture lands exactly where the
page spec wants it:

* left / top margin = the picture's position on the page;
* the text area is at least as large as the picture, so nothing is scaled.

Pictures that float in front of the text were used before and gave the same
pages in desktop Word, but a user saw them blink when the file was opened
elsewhere.  A picture in the line is the plainest form there is, and every
program lays it out the same way, so that is what is written now.

There is exactly one page per tile and no trailing blank page: the last
paragraph is the last picture.

The package is written directly (no python-docx needed at run time).
"""
from __future__ import annotations

from ..splitter import PageSpec, SplitPlan, iter_tiles
from .base import Progress, encode_for_document, looks_like_graphics, quality_option, report
from .ooxml import DOC_REL, Package, app_properties, attr, core_properties, emu, relationships

TWIPS_PER_MM = 1440 / 25.4
MAX_PAGE_TWIPS = 31680  # 22 inches, Word's limit
MIN_PAGE_TWIPS = 144

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)

_STYLES = (
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Malgun Gothic" w:hAnsi="Malgun Gothic" w:eastAsia="맑은 고딕" w:cs="Times New Roman"/>'
    '<w:sz w:val="20"/><w:szCs w:val="20"/><w:lang w:val="en-US" w:eastAsia="ko-KR" w:bidi="ar-SA"/>'
    "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>"
    '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/>'
    "</w:pPr></w:pPrDefault></w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="a"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    "</w:styles>"
)

_SETTINGS = (
    '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:zoom w:percent="100"/><w:compat>'
    '<w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/>'
    "</w:compat></w:settings>"
)


#: room under the picture for the line's descent (a line holding a picture is a hair taller than it)
LINE_SLACK_TWIPS = 20


def _page_xml(index: int, count: int, rid: str, spec: PageSpec) -> str:
    cx, cy = emu(spec.w), emu(spec.h)
    name = attr(f"조각 {index + 1}")
    descr = attr(f"{count}조각 중 {index + 1}번째")
    page_break = "<w:pageBreakBefore/>" if index else ""
    return (
        f"<w:p><w:pPr>{page_break}<w:spacing w:before=\"0\" w:after=\"0\" w:line=\"240\" w:lineRule=\"auto\"/>"
        "<w:ind w:left=\"0\" w:right=\"0\" w:firstLine=\"0\"/><w:jc w:val=\"left\"/>"
        "<w:rPr><w:sz w:val=\"2\"/><w:szCs w:val=\"2\"/></w:rPr></w:pPr>"
        "<w:r><w:rPr><w:noProof/><w:sz w:val=\"2\"/><w:szCs w:val=\"2\"/></w:rPr><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{index + 1}" name="{name}" descr="{descr}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{index + 1}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def _margins(spec: PageSpec, page_w: int, page_h: int) -> tuple[int, int, int, int]:
    """(top, right, bottom, left) in twips that put a picture in the line exactly at the spec's place.

    Left and top are the picture's position.  Right and bottom are rounded down so the text area is
    never smaller than the picture, and the bottom leaves a point of room for the line's descent.
    """
    left = max(0, int(spec.x * TWIPS_PER_MM))
    top = max(0, int(spec.y * TWIPS_PER_MM))
    right = max(0, int((spec.page_w - spec.x - spec.w) * TWIPS_PER_MM))
    bottom = max(0, int((spec.page_h - spec.y - spec.h) * TWIPS_PER_MM) - LINE_SLACK_TWIPS)
    # tiny pages: never let rounding push the margins past the page
    left, right = min(left, page_w - 1), min(right, max(0, page_w - left - 1))
    top, bottom = min(top, page_h - 1), min(bottom, max(0, page_h - top - 1))
    return top, right, bottom, left


def export_docx(source, plan: SplitPlan, spec: PageSpec, options: dict,
                progress: Progress = None, title: str = "") -> bytes:
    quality = quality_option(options)
    lossless = looks_like_graphics(source)
    page_w = int(round(spec.page_w * TWIPS_PER_MM))
    page_h = int(round(spec.page_h * TWIPS_PER_MM))
    biggest = max(page_w, page_h)
    if biggest > MAX_PAGE_TWIPS:  # Word cannot go beyond 22 inches: shrink everything together
        k = MAX_PAGE_TWIPS / biggest
        spec = PageSpec(spec.page_w * k, spec.page_h * k, spec.x * k, spec.y * k, spec.w * k, spec.h * k,
                        spec.paper, spec.margin * k)
        page_w, page_h = int(spec.page_w * TWIPS_PER_MM), int(spec.page_h * TWIPS_PER_MM)
    page_w, page_h = max(MIN_PAGE_TWIPS, page_w), max(MIN_PAGE_TWIPS, page_h)
    top, right, bottom, left = _margins(spec, page_w, page_h)

    package = Package()
    body, rels, extensions = [], [
        ("rId1", f"{DOC_REL}/styles", "styles.xml"),
        ("rId2", f"{DOC_REL}/settings", "settings.xml"),
    ], set()
    for index, tile in iter_tiles(source, plan):
        image = encode_for_document(tile, quality, lossless)
        del tile
        rid = f"rId{index + 10}"
        target = f"media/image{index + 1}.{image.ext}"
        package.binary(f"word/{target}", image.data)
        extensions.add(image.ext)
        rels.append((rid, f"{DOC_REL}/image", target))
        body.append(_page_xml(index, plan.count, rid, spec))
        report(progress, index + 1, plan.count)

    orient = ' w:orient="landscape"' if page_w > page_h else ""
    sect = (
        f'<w:sectPr><w:pgSz w:w="{page_w}" w:h="{page_h}"{orient}/>'
        f'<w:pgMar w:top="{top}" w:right="{right}" w:bottom="{bottom}" w:left="{left}" '
        'w:header="0" w:footer="0" w:gutter="0"/></w:sectPr>'
    )
    package.xml("word/document.xml", f"<w:document {_NS}><w:body>{''.join(body)}{sect}</w:body></w:document>")
    package.xml("word/_rels/document.xml.rels", relationships(rels))
    package.xml("word/styles.xml", _STYLES)
    package.xml("word/settings.xml", _SETTINGS)
    package.xml("docProps/core.xml", core_properties(title or "split"))
    package.xml("docProps/app.xml", app_properties("Microsoft Office Word"))
    package.xml("_rels/.rels", relationships([
        ("rId1", f"{DOC_REL}/officeDocument", "word/document.xml"),
        ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties", "docProps/core.xml"),
        ("rId3", f"{DOC_REL}/extended-properties", "docProps/app.xml"),
    ]))
    defaults = "".join(
        f'<Default Extension="{ext}" ContentType="{mime}"/>'
        for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")) if ext in extensions
    )
    package.xml("[Content_Types].xml", (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>' + defaults +
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    ))
    return package.finish()
