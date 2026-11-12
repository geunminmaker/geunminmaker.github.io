# -*- coding: utf-8 -*-
"""hwpx_writer.py - tiny HWPX (OWPML / KS X 6101) writer: one picture per page.

Pure standard library (zipfile, zlib, struct, hashlib, datetime) so it runs
unchanged on CPython 3.8+ and inside Pyodide.  No lxml, no Windows-only code.

    data = build_hwpx(
        [{"data": png_bytes, "ext": "png", "width_px": 2480, "height_px": 3508}, ...],
        page_w_mm=210, page_h_mm=297, margin_mm=0.0)

The package layout, XML vocabulary, attribute order and the default style
tables below mirror what Hancom Office Hangul itself writes (reference files:
neolord0/hwpxlib testFile/reader_writer/SimplePicture.hwpx and
PageSize_Margin.hwpx, both saved by Hangul 9.1.1.5656).  header.xml produced
by _header_xml() is byte-identical to the header of those Hancom files except
for one removed <hc:fillBrush> element (see the comment in _header_xml).

Units
-----
HWPUNIT = 1/7200 inch  (1 pt = 100 HWPUNIT, 1 mm = 7200/25.4 = 283.4646 HWPUNIT)
A pixel of a picture is described to Hangul as 75 HWPUNIT (= 96 dpi).
Paper sizes are multiples of 4 HWPUNIT in Hangul (A4 = 59528 x 84188).

Rules this writer depends on (each one checked against Hancom-written files
and against Hangul 2018, build 10.0.0.5060, on screen and through its object model)
---------------------------------------------------------------------------
* zip: `mimetype` is the first entry, stored, content "application/hwp+zip";
  PNG/JPEG entries are stored, XML entries deflated.
* Hancom spellings are part of the format: tagetApplication (version.xml),
  isEmbeded (content.hpf), trackchageConfig (header.xml), media-type "image/jpg".
* Never emit <hc:fillBrush><hc:winBrush faceColor="none" .../> for the paragraph
  border fill: Hangul 2018 paints it as a solid black bar on every line.
* landscape="NARROWLY" is the rotated (landscape) sheet, "WIDELY" the normal one;
  width/height always describe the portrait sheet (width <= height); the four
  margins are given in the rotated, as-seen frame.
* One page per picture = one paragraph per picture; paragraphs 2..N carry
  pageBreak="1"; there is no trailing paragraph, hence no trailing blank page.
* The picture floats in front of the text, anchored to the PAPER with absolute
  offsets, so it can fill the whole sheet without ever changing the page count.
* The first run of the first paragraph starts with <hp:secPr> (paper, margins,
  header/footer length 0) followed by the <hp:colPr> control.
* Every *IDRef in section0.xml must exist in header.xml (border fill ids start
  at 1, all other ids at 0; outlineShapeIDRef="1" needs numbering id 1).
* <hp:pic>: orgSz = pixels x 75, curSz = sz = shown size, scaMatrix = cur/org,
  imgRect = the orgSz rectangle, imgDim = pixels x 75, imgClip = 0..imgDim.
"""

import datetime as _dt
import hashlib
import io
import struct
import zipfile
from xml.sax.saxutils import escape as _xml_escape

__all__ = ["build_hwpx", "mm_to_hwpunit", "HwpxWriterError", "HWPX_MIME", "HWPX_EXT"]

HWPX_MIME = "application/hwp+zip"   # content of the `mimetype` entry; also a sensible download MIME
HWPX_EXT = ".hwpx"

HWPUNIT_PER_INCH = 7200
HWPUNIT_PER_MM = HWPUNIT_PER_INCH / 25.4
HWPUNIT_PER_PX = 75                 # 7200 / 96 dpi - what Hangul stores in <hp:imgDim>

# version.xml values of the Hancom build whose package layout is reproduced.
_APP_NAME = "Hancom Office Hangul"
_APP_VERSION = "9, 1, 1, 5656 WIN32LEWindows_Unknown_Version"

_XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
_NS = (
    'xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" '
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
    'xmlns:hp10="http://www.hancom.co.kr/hwpml/2016/paragraph" '
    'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
    'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" '
    'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
    'xmlns:hhs="http://www.hancom.co.kr/hwpml/2011/history" '
    'xmlns:hm="http://www.hancom.co.kr/hwpml/2011/master-page" '
    'xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:opf="http://www.idpf.org/2007/opf/" '
    'xmlns:ooxmlchart="http://www.hancom.co.kr/hwpml/2016/ooxmlchart" '
    'xmlns:hwpunitchar="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar" '
    'xmlns:epub="http://www.idpf.org/2007/ops" '
    'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0"'
)


class HwpxWriterError(ValueError):
    """Bad arguments for build_hwpx()."""


def _esc(text):
    """Escape user supplied text (file names, title) for XML; drop characters XML 1.0 cannot carry."""
    text = str(text).encode("utf-8", "replace").decode("utf-8")          # lone surrogates -> "?"
    text = "".join(ch for ch in text if ord(ch) in (9, 10) or (ord(ch) >= 0x20 and ord(ch) not in (0xFFFE, 0xFFFF)))
    return _xml_escape(text)


def mm_to_hwpunit(mm):
    """Millimetres -> HWPUNIT (1/7200 inch), rounded to the nearest unit."""
    return int(round(float(mm) * HWPUNIT_PER_MM))


def _paper_unit(mm):
    # Hangul stores paper sizes as multiples of 4 HWPUNIT (1/1800 inch):
    # A4 = 59528 x 84188, B4 = 72852 x 103180.  Rounding the same way makes
    # 210 x 297 mm show up as "A4" in the page-setup dialog instead of "custom".
    return int(round(float(mm) * HWPUNIT_PER_MM / 4.0)) * 4


# --------------------------------------------------------------------------
# header.xml  (default style tables of a blank Hangul document)
# --------------------------------------------------------------------------
_LANGS = ("HANGUL", "LATIN", "HANJA", "JAPANESE", "OTHER", "SYMBOL", "USER")
_LANG_ATTRS = ("hangul", "latin", "hanja", "japanese", "other", "symbol", "user")
_FONTS = ("함초롬돋움", "함초롬바탕")   # HCR Dotum, HCR Batang

# (height, textColor, fontRef, spacing)
_CHAR_PR = (
    (1000, "#000000", 1, 0), (1000, "#000000", 0, 0), (900, "#000000", 0, 0),
    (900, "#000000", 1, 0), (900, "#000000", 0, -5), (1600, "#2E74B5", 0, 0),
    (1100, "#000000", 0, 0),
)
# (tabPr, condense, align, heading(type, level), breakNonLatinWord, widowOrphan,
#  autoSpacing, intent, left, prev, next, lineSpacing%)  - margins as written in
#  the HwpUnitChar <hp:case>; the <hp:default> branch carries exactly twice the value.
_PARA_PR = (
    (0, 0, "LEFT", ("NONE", 0), "BREAK_WORD", 0, 0, 0, 0, 0, 0, 130),
    (0, 0, "JUSTIFY", ("NONE", 0), "KEEP_WORD", 0, 0, -1310, 0, 0, 0, 130),
    (0, 0, "JUSTIFY", ("NONE", 0), "BREAK_WORD", 0, 0, 0, 0, 0, 0, 150),
    (0, 0, "JUSTIFY", ("NONE", 0), "KEEP_WORD", 0, 0, 0, 0, 0, 0, 160),
    (1, 20, "JUSTIFY", ("OUTLINE", 6), "KEEP_WORD", 0, 0, 0, 7000, 0, 0, 160),
    (1, 20, "JUSTIFY", ("OUTLINE", 5), "KEEP_WORD", 0, 0, 0, 6000, 0, 0, 160),
    (1, 20, "JUSTIFY", ("OUTLINE", 4), "KEEP_WORD", 0, 0, 0, 5000, 0, 0, 160),
    (1, 20, "JUSTIFY", ("OUTLINE", 3), "KEEP_WORD", 0, 0, 0, 4000, 0, 0, 160),
    (1, 20, "JUSTIFY", ("OUTLINE", 2), "KEEP_WORD", 0, 0, 0, 3000, 0, 0, 160),
    (1, 20, "JUSTIFY", ("OUTLINE", 1), "KEEP_WORD", 0, 0, 0, 2000, 0, 0, 160),
    (1, 20, "JUSTIFY", ("OUTLINE", 0), "KEEP_WORD", 0, 0, 0, 1000, 0, 0, 160),
    (0, 0, "JUSTIFY", ("NONE", 0), "KEEP_WORD", 0, 0, 0, 1500, 0, 0, 160),
    (1, 20, "LEFT", ("NONE", 0), "BREAK_WORD", 1, 1, 0, 0, 1200, 300, 160),
    (0, 0, "LEFT", ("NONE", 0), "BREAK_WORD", 1, 1, 0, 0, 0, 700, 160),
    (0, 0, "LEFT", ("NONE", 0), "BREAK_WORD", 1, 1, 0, 1100, 0, 700, 160),
    (0, 0, "LEFT", ("NONE", 0), "BREAK_WORD", 1, 1, 0, 2200, 0, 700, 160),
)
# (type, name, engName, paraPr, charPr, nextStyle or None = itself)
_STYLES = (
    ("PARA", "바탕글", "Normal", 3, 0, None),
    ("PARA", "본문", "Body", 11, 0, None),
    ("PARA", "개요 1", "Outline 1", 10, 0, None),
    ("PARA", "개요 2", "Outline 2", 9, 0, None),
    ("PARA", "개요 3", "Outline 3", 8, 0, None),
    ("PARA", "개요 4", "Outline 4", 7, 0, None),
    ("PARA", "개요 5", "Outline 5", 6, 0, None),
    ("PARA", "개요 6", "Outline 6", 5, 0, None),
    ("PARA", "개요 7", "Outline 7", 4, 0, None),
    ("CHAR", "쪽 번호", "Page Number", 0, 1, 0),
    ("PARA", "머리말", "Header", 2, 2, None),
    ("PARA", "각주", "Footnote", 1, 3, None),
    ("PARA", "미주", "Endnote", 1, 3, None),
    ("PARA", "메모", "Memo", 0, 4, None),
    ("PARA", "차례 제목", "TOC Heading", 12, 5, None),
    ("PARA", "차례 1", "TOC 1", 13, 6, None),
    ("PARA", "차례 2", "TOC 2", 14, 6, None),
    ("PARA", "차례 3", "TOC 3", 15, 6, None),
)
_PARA_HEADS = (("DIGIT", "^1.", 0), ("HANGUL_SYLLABLE", "^2.", 0), ("DIGIT", "^3)", 0),
               ("HANGUL_SYLLABLE", "^4)", 0), ("DIGIT", "(^5)", 0), ("HANGUL_SYLLABLE", "(^6)", 0),
               ("CIRCLED_DIGIT", "^7", 1))

_BODY_STYLE_ID = 0          # style "Normal" ...
_BODY_PARA_PR_ID = 3        # ... its paragraph shape
_BODY_CHAR_PR_ID = 0        # ... its character shape (10 pt -> line height 1000)


def _lang_attrs(value):
    return " ".join('%s="%s"' % (a, value) for a in _LANG_ATTRS)


def _header_xml():
    o = [_XML_DECL, '<hh:head ', _NS, ' version="1.4" secCnt="1">',
         '<hh:beginNum page="1" footnote="1" endnote="1" pic="1" tbl="1" equation="1"/><hh:refList>']
    # fonts
    type_info = ('<hh:typeInfo familyType="FCAT_GOTHIC" weight="8" proportion="4" contrast="0" '
                 'strokeVariation="1" armStyle="1" letterform="1" midline="1" xHeight="1"/>')
    o.append('<hh:fontfaces itemCnt="%d">' % len(_LANGS))
    for lang in _LANGS:
        o.append('<hh:fontface lang="%s" fontCnt="%d">' % (lang, len(_FONTS)))
        for i, face in enumerate(_FONTS):
            o.append('<hh:font id="%d" face="%s" type="TTF" isEmbedded="0">%s</hh:font>' % (i, face, type_info))
        o.append('</hh:fontface>')
    o.append('</hh:fontfaces>')
    # Border fills 1 and 2: no border, no fill.  Hangul's own blank document gives borderFill 2 a
    # <hc:fillBrush><hc:winBrush faceColor="none" hatchColor="#FF000000" alpha="0"/></hc:fillBrush> child;
    # Hangul 2018 (10.0.0.5060) paints that as a SOLID BLACK paragraph background (a black bar on
    # every anchor line, verified on screen), so the fill brush is deliberately left out.
    border = ('<hh:slash type="NONE" Crooked="0" isCounter="0"/><hh:backSlash type="NONE" Crooked="0" isCounter="0"/>'
              '<hh:leftBorder type="NONE" width="0.1 mm" color="#000000"/><hh:rightBorder type="NONE" width="0.1 mm" color="#000000"/>'
              '<hh:topBorder type="NONE" width="0.1 mm" color="#000000"/><hh:bottomBorder type="NONE" width="0.1 mm" color="#000000"/>'
              '<hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>')
    bf = '<hh:borderFill id="%d" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0">'
    o.append('<hh:borderFills itemCnt="2">' + bf % 1 + border + '</hh:borderFill>' + bf % 2 + border +
             '</hh:borderFill></hh:borderFills>')
    # character shapes
    o.append('<hh:charProperties itemCnt="%d">' % len(_CHAR_PR))
    for i, (height, color, font, spacing) in enumerate(_CHAR_PR):
        o.append('<hh:charPr id="%d" height="%d" textColor="%s" shadeColor="none" useFontSpace="0" useKerning="0" '
                 'symMark="NONE" borderFillIDRef="2">' % (i, height, color))
        o.append('<hh:fontRef %s/><hh:ratio %s/><hh:spacing %s/><hh:relSz %s/><hh:offset %s/>' % (
            _lang_attrs(font), _lang_attrs(100), _lang_attrs(spacing), _lang_attrs(100), _lang_attrs(0)))
        o.append('<hh:underline type="NONE" shape="SOLID" color="#000000"/><hh:strikeout shape="NONE" color="#000000"/>'
                 '<hh:outline type="NONE"/><hh:shadow type="NONE" color="#B2B2B2" offsetX="10" offsetY="10"/></hh:charPr>')
    o.append('</hh:charProperties>')
    o.append('<hh:tabProperties itemCnt="2"><hh:tabPr id="0" autoTabLeft="0" autoTabRight="0"/>'
             '<hh:tabPr id="1" autoTabLeft="1" autoTabRight="0"/></hh:tabProperties>')
    # outline numbering (referenced by secPr/@outlineShapeIDRef)
    o.append('<hh:numberings itemCnt="1"><hh:numbering id="1" start="0">')
    for level, (fmt, text, checkable) in enumerate(_PARA_HEADS, 1):
        o.append('<hh:paraHead start="1" level="%d" align="LEFT" useInstWidth="1" autoIndent="1" widthAdjust="0" '
                 'textOffsetType="PERCENT" textOffset="50" numFormat="%s" charPrIDRef="4294967295" checkable="%d">%s'
                 '</hh:paraHead>' % (level, fmt, checkable, text))
    o.append('</hh:numbering></hh:numberings>')
    # paragraph shapes
    o.append('<hh:paraProperties itemCnt="%d">' % len(_PARA_PR))

    def margin(k, intent, left, prev, nxt, spacing):
        return ('<hh:margin><hc:intent value="%d" unit="HWPUNIT"/><hc:left value="%d" unit="HWPUNIT"/>'
                '<hc:right value="0" unit="HWPUNIT"/><hc:prev value="%d" unit="HWPUNIT"/>'
                '<hc:next value="%d" unit="HWPUNIT"/></hh:margin><hh:lineSpacing type="PERCENT" value="%d" '
                'unit="HWPUNIT"/>' % (intent * k, left * k, prev * k, nxt * k, spacing))

    for i, (tab, condense, align, heading, brk, widow, auto, intent, left, prev, nxt, spacing) in enumerate(_PARA_PR):
        o.append('<hh:paraPr id="%d" tabPrIDRef="%d" condense="%d" fontLineHeight="0" snapToGrid="1" '
                 'suppressLineNumbers="0" checked="0">' % (i, tab, condense))
        o.append('<hh:align horizontal="%s" vertical="BASELINE"/><hh:heading type="%s" idRef="0" level="%d"/>'
                 % (align, heading[0], heading[1]))
        o.append('<hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="%s" widowOrphan="%d" keepWithNext="0" '
                 'keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/><hh:autoSpacing eAsianEng="%d" eAsianNum="%d"/>'
                 % (brk, widow, auto, auto))
        o.append('<hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar">'
                 + margin(1, intent, left, prev, nxt, spacing) + '</hp:case><hp:default>'
                 + margin(2, intent, left, prev, nxt, spacing) + '</hp:default></hp:switch>')
        o.append('<hh:border borderFillIDRef="2" offsetLeft="0" offsetRight="0" offsetTop="0" offsetBottom="0" '
                 'connect="0" ignoreMargin="0"/></hh:paraPr>')
    o.append('</hh:paraProperties>')
    # styles
    o.append('<hh:styles itemCnt="%d">' % len(_STYLES))
    for i, (kind, name, eng, para, char, nxt) in enumerate(_STYLES):
        o.append('<hh:style id="%d" type="%s" name="%s" engName="%s" paraPrIDRef="%d" charPrIDRef="%d" '
                 'nextStyleIDRef="%d" langID="1042" lockForm="0"/>' % (i, kind, name, eng, para, char, i if nxt is None else nxt))
    o.append('</hh:styles></hh:refList>')
    o.append('<hh:compatibleDocument targetProgram="HWP201X"><hh:layoutCompatibility/></hh:compatibleDocument>'
             '<hh:docOption><hh:linkinfo path="" pageInherit="0" footnoteInherit="0"/></hh:docOption>'
             '<hh:trackchageConfig flags="56"/></hh:head>')
    return "".join(o)


# --------------------------------------------------------------------------
# section0.xml
# --------------------------------------------------------------------------
def _sec_pr(paper_w, paper_h, landscape, margin):
    """Section definition control: paper, margins, no header/footer band, no page border.

    paper_w/paper_h are the *sheet* dimensions (short side first when landscape);
    landscape=True rotates the sheet (Hangul writes that as landscape="NARROWLY",
    and "WIDELY" for the normal, un-rotated sheet - the names are counter-intuitive).
    The four margins are given in the visual (already rotated) frame.
    """
    note = ('<hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>'
            '<hp:noteLine length="%s" type="SOLID" width="0.12 mm" color="#000000"/>'
            '<hp:noteSpacing betweenNotes="%d" belowLine="567" aboveLine="850"/>'
            '<hp:numbering type="CONTINUOUS" newNum="1"/><hp:placement place="%s" beneathText="0"/>')
    border = "".join(
        '<hp:pageBorderFill type="%s" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" '
        'fillArea="PAPER"><hp:offset left="1417" right="1417" top="1417" bottom="1417"/></hp:pageBorderFill>' % t
        for t in ("BOTH", "EVEN", "ODD"))
    return (
        '<hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="1134" tabStop="8000" tabStopVal="4000" '
        'tabStopUnit="HWPUNIT" outlineShapeIDRef="1" memoShapeIDRef="0" textVerticalWidthHead="0" masterPageCnt="0">'
        '<hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0"/>'
        '<hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/>'
        '<hp:visibility hideFirstHeader="0" hideFirstFooter="0" hideFirstMasterPage="0" border="SHOW_ALL" '
        'fill="SHOW_ALL" hideFirstPageNum="0" hideFirstEmptyLine="0" showLineNumber="0"/>'
        '<hp:lineNumberShape restartType="0" countBy="0" distance="0" startNumber="0"/>'
        '<hp:pagePr landscape="%s" width="%d" height="%d" gutterType="LEFT_ONLY">'
        '<hp:margin header="0" footer="0" gutter="0" left="%d" right="%d" top="%d" bottom="%d"/></hp:pagePr>'
        '<hp:footNotePr>%s</hp:footNotePr><hp:endNotePr>%s</hp:endNotePr>%s</hp:secPr>'
        '<hp:ctrl><hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="1" sameSz="1" sameGap="0"/></hp:ctrl>'
    ) % ("NARROWLY" if landscape else "WIDELY", paper_w, paper_h, margin, margin, margin, margin,
         note % ("-1", 283, "EACH_COLUMN"), note % ("14692344", 0, "END_OF_DOCUMENT"), border)


def _ratio(a, b):
    s = "%.6f" % (float(a) / float(b))
    s = s.rstrip("0").rstrip(".")
    return s or "0"


def _pic_xml(index, item_id, name, px_w, px_h, x, y, w, h):
    """A floating picture, in front of text, anchored to the paper at (x, y), size w x h (HWPUNIT)."""
    org_w, org_h = px_w * HWPUNIT_PER_PX, px_h * HWPUNIT_PER_PX      # "natural" size at 96 dpi
    inst = 0x03D00000 + 2 * index + 1
    comment = "그림입니다.\n원본 그림의 이름: %s\n" \
              "원본 그림의 크기: 가로 %dpixel, 세로 %dpixel" % (name, px_w, px_h)
    return (
        '<hp:pic id="%d" zOrder="%d" numberingType="PICTURE" textWrap="IN_FRONT_OF_TEXT" textFlow="BOTH_SIDES" lock="0" '
        'dropcapstyle="None" href="" groupLevel="0" instid="%d" reverse="0">'
        '<hp:offset x="0" y="0"/><hp:orgSz width="%d" height="%d"/><hp:curSz width="%d" height="%d"/>'
        '<hp:flip horizontal="0" vertical="0"/>'
        '<hp:rotationInfo angle="0" centerX="%d" centerY="%d" rotateimage="1"/>'
        '<hp:renderingInfo><hc:transMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>'
        '<hc:scaMatrix e1="%s" e2="0" e3="0" e4="0" e5="%s" e6="0"/>'
        '<hc:rotMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/></hp:renderingInfo>'
        '<hp:imgRect><hc:pt0 x="0" y="0"/><hc:pt1 x="%d" y="0"/><hc:pt2 x="%d" y="%d"/><hc:pt3 x="0" y="%d"/></hp:imgRect>'
        '<hp:imgClip left="0" right="%d" top="0" bottom="%d"/>'
        '<hp:inMargin left="0" right="0" top="0" bottom="0"/>'
        '<hp:imgDim dimwidth="%d" dimheight="%d"/>'
        '<hc:img binaryItemIDRef="%s" bright="0" contrast="0" effect="REAL_PIC" alpha="0"/>'
        '<hp:effects/>'
        '<hp:sz width="%d" widthRelTo="ABSOLUTE" height="%d" heightRelTo="ABSOLUTE" protect="0"/>'
        '<hp:pos treatAsChar="0" affectLSpacing="0" flowWithText="0" allowOverlap="1" holdAnchorAndSO="0" '
        'vertRelTo="PAPER" horzRelTo="PAPER" vertAlign="TOP" horzAlign="LEFT" vertOffset="%d" horzOffset="%d"/>'
        '<hp:outMargin left="0" right="0" top="0" bottom="0"/>'
        '<hp:shapeComment>%s</hp:shapeComment></hp:pic>'
    ) % (0x40000000 + inst - 1, index, inst,
         org_w, org_h, w, h, w // 2, h // 2,
         _ratio(w, org_w), _ratio(h, org_h),
         org_w, org_w, org_h, org_h,
         org_w, org_h, org_w, org_h,
         item_id, w, h, y, x, _esc(comment))


def _fit(px_w, px_h, box_w, box_h):
    """Largest w x h (HWPUNIT) with the pixel aspect ratio that fits inside box_w x box_h."""
    if px_w * box_h >= px_h * box_w:            # picture is relatively wider -> width-bound
        w = box_w
        h = max(1, min(box_h, int(round(box_w * px_h / float(px_w)))))
    else:
        h = box_h
        w = max(1, min(box_w, int(round(box_h * px_w / float(px_h)))))
    return w, h


def _section_xml(placed, paper_w, paper_h, landscape, margin, body_w):
    line = ('<hp:linesegarray><hp:lineseg textpos="0" vertpos="0" vertsize="1000" textheight="1000" baseline="850" '
            'spacing="600" horzpos="0" horzsize="%d" flags="393216"/></hp:linesegarray>' % body_w)
    o = [_XML_DECL, '<hs:sec ', _NS, '>']
    for i, pic in enumerate(placed):
        # One (empty) paragraph per page; every paragraph after the first starts a new page.
        o.append('<hp:p id="%d" paraPrIDRef="%d" styleIDRef="%d" pageBreak="%d" columnBreak="0" merged="0">'
                 '<hp:run charPrIDRef="%d">' % (2764991984 if i == 0 else 0, _BODY_PARA_PR_ID, _BODY_STYLE_ID,
                                                0 if i == 0 else 1, _BODY_CHAR_PR_ID))
        if i == 0:
            o.append(_sec_pr(paper_w, paper_h, landscape, margin))
        o.append(pic)
        o.append('<hp:t/></hp:run>' + line + '</hp:p>')
    o.append('</hs:sec>')
    return "".join(o)


# --------------------------------------------------------------------------
# package parts
# --------------------------------------------------------------------------
def _version_xml():
    return (_XML_DECL + '<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" '
            'tagetApplication="WORDPROCESSOR" major="5" minor="0" micro="5" buildNumber="0" os="1" xmlVersion="1.4" '
            'application="%s" appVersion="%s"/>' % (_APP_NAME, _APP_VERSION))


def _settings_xml():
    # caret after the three controls of the first paragraph (secPr, colPr, pic: 8 code units each)
    return (_XML_DECL + '<ha:HWPApplicationSetting xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" '
            'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0">'
            '<ha:CaretPosition listIDRef="0" paraIDRef="0" pos="24"/></ha:HWPApplicationSetting>')


def _container_xml():
    return (_XML_DECL + '<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container" '
            'xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf"><ocf:rootfiles>'
            '<ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>'
            '<ocf:rootfile full-path="Preview/PrvText.txt" media-type="text/plain"/>'
            '</ocf:rootfiles></ocf:container>')


def _manifest_xml():
    return _XML_DECL + '<odf:manifest xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>'


_WEEKDAYS = "월화수목금토일"


def _content_hpf(items, title, creator, when):
    utc = when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hour12 = when.hour % 12 or 12
    korean = "%d년 %d월 %d일 %s요일 %s %d:%02d:%02d" % (
        when.year, when.month, when.day, _WEEKDAYS[when.weekday()],
        "오전" if when.hour < 12 else "오후", hour12, when.minute, when.second)
    o = [_XML_DECL, '<opf:package ', _NS, ' version="" unique-identifier="" id=""><opf:metadata>',
         '<opf:title>%s</opf:title>' % _esc(title) if title else '<opf:title/>',
         '<opf:language>ko</opf:language>',
         '<opf:meta name="creator" content="text">%s</opf:meta>' % _esc(creator),
         '<opf:meta name="subject" content="text"/><opf:meta name="description" content="text"/>',
         '<opf:meta name="lastsaveby" content="text">%s</opf:meta>' % _esc(creator),
         '<opf:meta name="CreatedDate" content="text">%s</opf:meta>' % utc,
         '<opf:meta name="ModifiedDate" content="text">%s</opf:meta>' % utc,
         '<opf:meta name="date" content="text">%s</opf:meta>' % korean,
         '<opf:meta name="keyword" content="text"/></opf:metadata><opf:manifest>',
         '<opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>']
    for item_id, href, media in items:
        o.append('<opf:item id="%s" href="%s" media-type="%s" isEmbeded="1"/>' % (item_id, href, media))
    o.append('<opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>'
             '<opf:item id="settings" href="settings.xml" media-type="application/xml"/></opf:manifest>'
             '<opf:spine><opf:itemref idref="header" linear="yes"/><opf:itemref idref="section0" linear="yes"/>'
             '</opf:spine></opf:package>')
    return "".join(o)


# --------------------------------------------------------------------------
# image helpers (stdlib only)
# --------------------------------------------------------------------------
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MEDIA = {"png": "image/png", "jpg": "image/jpg"}       # Hangul really writes "image/jpg"


def _sniff(data):
    if data[:8] == _PNG_MAGIC:
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    return None


def _image_size(data, kind):
    """Pixel size from the PNG IHDR / JPEG SOFn header; None if it cannot be read."""
    try:
        if kind == "png":
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if data[12:16] == b"IHDR" else None
        pos, n = 2, len(data)
        while pos + 9 < n:
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0xFF, 0x00, 0x01) or 0xD0 <= marker <= 0xD9:
                pos += 2 if marker != 0xFF else 1
                continue
            seg = struct.unpack(">H", data[pos + 2:pos + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[pos + 5:pos + 9])
                return w, h
            pos += 2 + seg
    except (struct.error, IndexError):
        pass
    return None


def _zip_add(zf, name, payload, when, compress):
    info = zipfile.ZipInfo(name, date_time=(max(1980, when.year), when.month, when.day,
                                            when.hour, when.minute, when.second))
    info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    info.create_system = 0                  # same on Windows, Linux and Pyodide
    info.external_attr = 0
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    zf.writestr(info, payload)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def build_hwpx(pages, page_w_mm, page_h_mm, margin_mm=0.0, title="", creator="split_image",
               preview_png=None, created=None):
    """Build a .hwpx with exactly one page per picture and return it as bytes.

    pages       list of dicts: {"data": bytes (PNG or JPEG), "ext": "png" | "jpg",
                "width_px": int, "height_px": int, "name": optional display name}.
                width_px/height_px may be omitted - they are then read from the image header.
    page_w_mm,  size of the page as it is read on screen; page_w_mm > page_h_mm produces a
    page_h_mm   landscape page (stored the Hangul way: portrait sheet + landscape flag).
    margin_mm   uniform page margin; the picture is scaled (aspect ratio kept, up or down)
                to fit the area inside the margins and is centred in it.
    title, creator   written to the package metadata (document summary).
    preview_png optional small PNG used as Preview/PrvImage.png (Explorer / file-dialog thumbnail).
    created     optional datetime (defaults to now) - pass a fixed value for reproducible output.
    """
    pages = list(pages or ())
    if not pages:
        raise HwpxWriterError("pages is empty - at least one image is required")
    try:
        page_w_mm, page_h_mm, margin_mm = float(page_w_mm), float(page_h_mm), float(margin_mm)
    except (TypeError, ValueError):
        raise HwpxWriterError("page size and margin must be numbers (millimetres)")
    if not (page_w_mm > 0 and page_h_mm > 0) or margin_mm < 0:
        raise HwpxWriterError("page size must be positive and the margin must not be negative")

    vis_w, vis_h = _paper_unit(page_w_mm), _paper_unit(page_h_mm)      # page as seen on screen
    margin = mm_to_hwpunit(margin_mm)
    body_w, body_h = vis_w - 2 * margin, vis_h - 2 * margin
    if body_w < 2000 or body_h < 2000:      # < ~7 mm: no room for the anchor line of text
        raise HwpxWriterError("the printable area is too small (page %.1f x %.1f mm, margin %.1f mm)"
                              % (page_w_mm, page_h_mm, margin_mm))
    landscape = vis_w > vis_h
    paper_w, paper_h = (vis_h, vis_w) if landscape else (vis_w, vis_h)

    when = created or _dt.datetime.now().astimezone()
    if when.tzinfo is None:
        when = when.astimezone()

    items, binaries, placed, seen = [], [], [], {}
    for i, page in enumerate(pages):
        data = page.get("data")
        if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < 16:
            raise HwpxWriterError("pages[%d]['data'] must be the bytes of a PNG or JPEG file" % i)
        data = bytes(data)
        kind = _sniff(data)
        if kind is None:
            raise HwpxWriterError("pages[%d] is neither PNG nor JPEG (declared ext=%r)" % (i, page.get("ext")))
        size = _image_size(data, kind) or (0, 0)
        px_w = int(page.get("width_px") or size[0])
        px_h = int(page.get("height_px") or size[1])
        if px_w <= 0 or px_h <= 0:
            raise HwpxWriterError("pages[%d]: width_px/height_px missing and not readable from the image" % i)
        key = hashlib.sha1(data).digest()
        if key not in seen:                 # identical bytes are stored once
            item_id = "image%d" % (len(items) + 1)
            href = "BinData/%s.%s" % (item_id, kind)
            items.append((item_id, href, _MEDIA[kind]))
            binaries.append((href, data))
            seen[key] = item_id
        w, h = _fit(px_w, px_h, body_w, body_h)
        x = margin + (body_w - w) // 2
        y = margin + (body_h - h) // 2
        name = str(page.get("name") or "image%d.%s" % (i + 1, kind))
        placed.append(_pic_xml(i, seen[key], name, px_w, px_h, x, y, w, h))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        # Entry order as written by Hangul; `mimetype` must be first and uncompressed.
        _zip_add(zf, "mimetype", HWPX_MIME, when, False)
        _zip_add(zf, "version.xml", _version_xml(), when, False)
        _zip_add(zf, "Contents/header.xml", _header_xml(), when, True)
        for href, data in binaries:
            _zip_add(zf, href, data, when, False)       # PNG/JPEG are already compressed
        _zip_add(zf, "Contents/section0.xml",
                 _section_xml(placed, paper_w, paper_h, landscape, margin, body_w), when, True)
        _zip_add(zf, "Preview/PrvText.txt", "\r\n", when, True)
        _zip_add(zf, "settings.xml", _settings_xml(), when, True)
        if preview_png:
            _zip_add(zf, "Preview/PrvImage.png", bytes(preview_png), when, False)
        _zip_add(zf, "META-INF/container.xml", _container_xml(), when, True)
        _zip_add(zf, "Contents/content.hpf", _content_hpf(items, title, creator, when), when, True)
        _zip_add(zf, "META-INF/manifest.xml", _manifest_xml(), when, True)
    return buf.getvalue()
