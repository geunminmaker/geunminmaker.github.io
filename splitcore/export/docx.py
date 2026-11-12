"""Word (.docx) export: one page per tile.

Each tile is a picture *anchored to the page* (not inline text), which gives

* exact placement - a tile from a square grid covers the whole sheet;
* exactly one page per tile and never a trailing blank page, because the
  picture does not take part in text flow;
* ordinary page margins for the (empty) text layer, so Word does not show its
  "margins are outside the printable area" warning when printing.

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


def _page_xml(index: int, count: int, rid: str, spec: PageSpec) -> str:
    x, y, cx, cy = emu(spec.x), emu(spec.y), emu(spec.w), emu(spec.h)
    name = attr(f"조각 {index + 1}")
    descr = attr(f"{count}조각 중 {index + 1}번째")
    page_break = "<w:pageBreakBefore/>" if index else ""
    return (
        f"<w:p><w:pPr>{page_break}<w:spacing w:before=\"0\" w:after=\"0\"/><w:rPr><w:sz w:val=\"2\"/><w:szCs w:val=\"2\"/></w:rPr></w:pPr>"
        "<w:r><w:rPr><w:noProof/><w:sz w:val=\"2\"/><w:szCs w:val=\"2\"/></w:rPr><w:drawing>"
        f'<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="{251658240 + index}" '
        'behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="page"><wp:posOffset>{x}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="page"><wp:posOffset>{y}</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
        f'<wp:docPr id="{index + 1}" name="{name}" descr="{descr}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{index + 1}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        "</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r></w:p>"
    )


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
    margin = int(min(720, min(page_w, page_h) / 4))  # 0.5 inch, less on tiny pages

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
        f'<w:pgMar w:top="{margin}" w:right="{margin}" w:bottom="{margin}" w:left="{margin}" '
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
