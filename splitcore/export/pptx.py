"""PowerPoint (.pptx) export: one slide per tile, slide size = page size.

The package is written directly (no python-pptx needed at run time): one
blank layout, one master, a standard theme, and a picture on every slide.
"""
from __future__ import annotations

from ..splitter import PageSpec, SplitPlan, iter_tiles
from .base import Progress, encode_for_document, looks_like_graphics, quality_option, report
from .ooxml import DOC_REL, Package, app_properties, attr, core_properties, emu, relationships

MIN_SLIDE_EMU = 914400      # 1 inch
MAX_SLIDE_EMU = 51206400    # 56 inches

_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)

_GROUP = (
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
)

_MASTER = (
    f"<p:sldMaster {_NS}><p:cSld><p:bg><p:bgRef idx=\"1001\"><a:schemeClr val=\"bg1\"/></p:bgRef></p:bg>"
    f"<p:spTree>{_GROUP}</p:spTree></p:cSld>"
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" '
    'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
    "</p:sldMaster>"
)

_LAYOUT = (
    f'<p:sldLayout {_NS} type="blank" preserve="1"><p:cSld name="Blank"><p:spTree>{_GROUP}</p:spTree></p:cSld>'
    "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"
)

_FILL = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
_THEME = (
    '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme"><a:themeElements>'
    '<a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="44546A"/></a:dk2>'
    '<a:lt2><a:srgbClr val="E7E6E6"/></a:lt2><a:accent1><a:srgbClr val="4472C4"/></a:accent1>'
    '<a:accent2><a:srgbClr val="ED7D31"/></a:accent2><a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>'
    '<a:accent4><a:srgbClr val="FFC000"/></a:accent4><a:accent5><a:srgbClr val="5B9BD5"/></a:accent5>'
    '<a:accent6><a:srgbClr val="70AD47"/></a:accent6><a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
    '<a:folHlink><a:srgbClr val="954F72"/></a:folHlink></a:clrScheme>'
    '<a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/><a:cs typeface=""/>'
    '<a:font script="Hang" typeface="맑은 고딕"/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/>'
    '<a:font script="Hang" typeface="맑은 고딕"/></a:minorFont></a:fontScheme>'
    f'<a:fmtScheme name="Office"><a:fillStyleLst>{_FILL * 3}</a:fillStyleLst>'
    f'<a:lnStyleLst><a:ln w="6350">{_FILL}</a:ln><a:ln w="12700">{_FILL}</a:ln><a:ln w="19050">{_FILL}</a:ln></a:lnStyleLst>'
    "<a:effectStyleLst>" + "<a:effectStyle><a:effectLst/></a:effectStyle>" * 3 + "</a:effectStyleLst>"
    f"<a:bgFillStyleLst>{_FILL * 3}</a:bgFillStyleLst></a:fmtScheme>"
    "</a:themeElements></a:theme>"
)


def _slide_xml(index: int, count: int, spec: PageSpec) -> str:
    name = attr(f"조각 {index + 1}")
    descr = attr(f"{count}조각 중 {index + 1}번째")
    return (
        f"<p:sld {_NS}><p:cSld><p:spTree>{_GROUP}"
        f'<p:pic><p:nvPicPr><p:cNvPr id="2" name="{name}" descr="{descr}"/>'
        '<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
        '<p:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="{emu(spec.x)}" y="{emu(spec.y)}"/><a:ext cx="{emu(spec.w)}" cy="{emu(spec.h)}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
        "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def _fit_limits(spec: PageSpec) -> PageSpec:
    """Scale the whole page into PowerPoint's 1 - 56 inch slide range."""
    longest, shortest = emu(max(spec.page_w, spec.page_h)), emu(min(spec.page_w, spec.page_h))
    k = 1.0
    if longest > MAX_SLIDE_EMU:
        k = MAX_SLIDE_EMU / longest
    elif shortest < MIN_SLIDE_EMU:
        k = min(MIN_SLIDE_EMU / shortest, MAX_SLIDE_EMU / longest)
    if k == 1.0:
        return spec
    return PageSpec(spec.page_w * k, spec.page_h * k, spec.x * k, spec.y * k, spec.w * k, spec.h * k,
                    spec.paper, spec.margin * k)


def export_pptx(source, plan: SplitPlan, spec: PageSpec, options: dict,
                progress: Progress = None, title: str = "") -> bytes:
    quality = quality_option(options)
    lossless = looks_like_graphics(source)
    spec = _fit_limits(spec)
    package = Package()
    slide_ids, pres_rels, overrides, extensions = [], [], [], set()
    master_rel = "rId1"
    pres_rels.append((master_rel, f"{DOC_REL}/slideMaster", "slideMasters/slideMaster1.xml"))

    for index, tile in iter_tiles(source, plan):
        image = encode_for_document(tile, quality, lossless)
        del tile
        number = index + 1
        media = f"image{number}.{image.ext}"
        extensions.add(image.ext)
        package.binary(f"ppt/media/{media}", image.data)
        package.xml(f"ppt/slides/slide{number}.xml", _slide_xml(index, plan.count, spec))
        package.xml(f"ppt/slides/_rels/slide{number}.xml.rels", relationships([
            ("rId1", f"{DOC_REL}/slideLayout", "../slideLayouts/slideLayout1.xml"),
            ("rId2", f"{DOC_REL}/image", f"../media/{media}"),
        ]))
        rid = f"rId{number + 1}"
        pres_rels.append((rid, f"{DOC_REL}/slide", f"slides/slide{number}.xml"))
        slide_ids.append(f'<p:sldId id="{255 + number}" r:id="{rid}"/>')
        overrides.append(
            f'<Override PartName="/ppt/slides/slide{number}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
        report(progress, number, plan.count)

    theme_rel = f"rId{plan.count + 2}"
    pres_rels.append((theme_rel, f"{DOC_REL}/theme", "theme/theme1.xml"))
    package.xml("ppt/presentation.xml", (
        f'<p:presentation {_NS} saveSubsetFonts="1">'
        f'<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="{master_rel}"/></p:sldMasterIdLst>'
        f'<p:sldIdLst>{"".join(slide_ids)}</p:sldIdLst>'
        f'<p:sldSz cx="{emu(spec.page_w)}" cy="{emu(spec.page_h)}"/><p:notesSz cx="6858000" cy="9144000"/>'
        "</p:presentation>"
    ))
    package.xml("ppt/_rels/presentation.xml.rels", relationships(pres_rels))
    package.xml("ppt/slideMasters/slideMaster1.xml", _MASTER)
    package.xml("ppt/slideMasters/_rels/slideMaster1.xml.rels", relationships([
        ("rId1", f"{DOC_REL}/slideLayout", "../slideLayouts/slideLayout1.xml"),
        ("rId2", f"{DOC_REL}/theme", "../theme/theme1.xml"),
    ]))
    package.xml("ppt/slideLayouts/slideLayout1.xml", _LAYOUT)
    package.xml("ppt/slideLayouts/_rels/slideLayout1.xml.rels", relationships([
        ("rId1", f"{DOC_REL}/slideMaster", "../slideMasters/slideMaster1.xml"),
    ]))
    package.xml("ppt/theme/theme1.xml", _THEME)
    package.xml("docProps/core.xml", core_properties(title or "split"))
    package.xml("docProps/app.xml", app_properties("Microsoft Office PowerPoint"))
    package.xml("_rels/.rels", relationships([
        ("rId1", f"{DOC_REL}/officeDocument", "ppt/presentation.xml"),
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
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        + "".join(overrides) +
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        "</Types>"
    ))
    return package.finish()
