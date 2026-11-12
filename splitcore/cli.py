"""Command line front end:  python -m splitcore 사진.jpg

Without options it walks through the same questions as the app (which picture,
how many pieces, which layout, which format).  With options it runs unattended:

    python -m splitcore poster.pdf --pick 2 -n 9 --layout 3x3 -f pdf -o out --yes
"""
from __future__ import annotations

import argparse
import os
import sys

from .errors import SplitError
from .export import FORMATS, normalize_format
from .grids import SPLIT_COUNTS, parse_layout
from .session import Session

YES = {"y", "yes", "네", "예", "ㅇ", "응", "ㅇㅇ", "ok"}
NO = {"n", "no", "아니요", "아니오", "아니", "ㄴ"}


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise SplitError("INTERNAL", "입력이 중단되었어요.") from None


def _confirm(prompt: str, auto: bool) -> bool:
    if auto:
        return True
    while True:
        answer = _ask(f"{prompt} [네/아니요] ").lower()
        if answer in YES:
            return True
        if answer in NO:
            return False


def _choose(prompt: str, valid: list[str], auto_default: str | None = None) -> str:
    while True:
        answer = _ask(prompt).lower()
        if not answer and auto_default is not None:
            return auto_default
        if answer in valid:
            return answer
        print(f"  다음 중에서 골라 주세요: {', '.join(valid)}")


def grid_art(rows: int, cols: int, width: int, height: int) -> str:
    """Small text sketch of the grid with tile numbers."""
    cell_w = max(4, min(8, round(30 * (width / height) / cols)))
    line = "+" + ("-" * cell_w + "+") * cols
    out = [line]
    for r in range(rows):
        cells = "".join(f"{r * cols + c + 1:^{cell_w}}|" for c in range(cols))
        out.append("|" + cells)
        out.append(line)
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m splitcore",
        description="이미지를 N등분하고 각 조각을 원본 크기로 키워 저장합니다.",
    )
    p.add_argument("files", nargs="+", help="이미지 또는 문서 (jpg, png, pdf, docx, pptx, hwp, hwpx ...)")
    p.add_argument("--list", action="store_true", help="파일에서 찾은 이미지 목록만 보여주고 끝냅니다")
    p.add_argument("--pick", type=int, help="여러 이미지가 있을 때 분할할 이미지 번호 (1부터)")
    p.add_argument("-n", "--count", type=int, help=f"분할 수 ({', '.join(map(str, SPLIT_COUNTS))})")
    p.add_argument("--layout", help="분할 도안, 가로x세로 칸 수 (예: 3x2). 생략하면 추천 도안")
    p.add_argument("-f", "--format", help=f"저장 형식 ({', '.join(FORMATS)})")
    p.add_argument("--paper", choices=["original", "a4"], default="original", help="문서 형식의 용지 (기본: 원본 비율)")
    p.add_argument("-o", "--out", default=".", help="저장할 폴더 (기본: 현재 폴더)")
    p.add_argument("--password", help="암호가 걸린 PDF의 암호")
    p.add_argument("-y", "--yes", action="store_true", help="확인 질문 없이 진행")
    return p


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    session = Session()
    candidates: list[dict] = []
    for path in args.files:
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            found = session.add_file(os.path.basename(path), data, args.password)
            candidates.extend(found["sources"])
            if found["truncated"]:
                print(f"  ({path}: 이미지가 너무 많아 앞의 {len(found['sources'])}개만 불러왔어요)")
        except FileNotFoundError:
            print(f"! {path}: 파일을 찾을 수 없어요.", file=sys.stderr)
        except SplitError as exc:
            print(f"! {path}: {exc.message}", file=sys.stderr)
    if not candidates:
        return 2

    if args.list or len(candidates) > 1:
        print("찾은 이미지:")
        for i, c in enumerate(candidates, 1):
            where = f"{c['origin']} · {c['label']}" if c["label"] != c["origin"] else c["origin"]
            print(f"  {i:>3}. {where}  ({c['width']} x {c['height']} px)")
    if args.list:
        return 0

    # 1. which picture
    while True:
        if len(candidates) == 1:
            chosen = candidates[0]
        elif args.pick:
            if not 1 <= args.pick <= len(candidates):
                print(f"! --pick 은 1~{len(candidates)} 사이여야 해요.", file=sys.stderr)
                return 2
            chosen = candidates[args.pick - 1]
        else:
            number = _choose("분할할 이미지 번호: ", [str(i) for i in range(1, len(candidates) + 1)])
            chosen = candidates[int(number) - 1]
        print(f"선택한 이미지: {chosen['origin']} ({chosen['width']} x {chosen['height']} px)")
        if _confirm("이 이미지가 맞나요?", args.yes or bool(args.pick) or False):
            break
        if len(candidates) == 1:
            print("취소했어요.")
            return 1
        args.pick = None

    # 2. how many pieces, which layout
    while True:
        count = args.count
        if count is None:
            count = int(_choose(f"몇 분할할까요? ({', '.join(map(str, SPLIT_COUNTS))}): ",
                                [str(n) for n in SPLIT_COUNTS]))
        try:
            options = session.layouts(chosen["id"], count)
        except SplitError as exc:
            print(f"! {exc.message}", file=sys.stderr)
            return 2
        usable = [o for o in options if o["usable"]]
        if not usable:
            print("! 이미지가 너무 작아서 이만큼 나눌 수 없어요.", file=sys.stderr)
            return 2
        if args.layout:
            wanted = parse_layout(args.layout)
            match = [o for o in usable if (o["rows"], o["cols"]) == (wanted.rows, wanted.cols)]
            if not match:
                print(f"! {args.layout} 도안은 {count}분할에 쓸 수 없어요. 가능한 도안: "
                      f"{', '.join(o['key'] for o in usable)}", file=sys.stderr)
                return 2
            layout = match[0]
        elif len(usable) == 1 or args.yes:
            layout = usable[0]
        else:
            print("분할 도안 (가로 칸 x 세로 칸):")
            for i, o in enumerate(usable, 1):
                tag = "  ← 추천" if o["recommended"] else ""
                print(f"  {i}. {o['key']}{tag}")
            number = _choose("도안 번호 (엔터 = 추천): ", [str(i) for i in range(1, len(usable) + 1)], "1")
            layout = usable[int(number) - 1]
        print(grid_art(layout["rows"], layout["cols"], chosen["width"], chosen["height"]))
        print(f"{count}분할 · 가로 {layout['cols']}칸 x 세로 {layout['rows']}칸 · "
              f"조각 크기 {layout['out_w']} x {layout['out_h']} px")
        if _confirm("이대로 분할할까요?", args.yes):
            break
        if args.count is not None and args.layout:
            print("취소했어요.")
            return 1
        args.count = None
        args.layout = None

    # 3. format
    fmt = args.format
    if fmt is None:
        fmt = _choose(f"저장 형식 ({', '.join(FORMATS)}): ", list(FORMATS) + ["jpeg", "word", "ppt", "hwp"])
    try:
        fmt = normalize_format(fmt)
    except SplitError as exc:
        print(f"! {exc.message}", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)
    written = []

    def progress(done: int, total: int) -> None:
        print(f"\r  조각 만드는 중 {done}/{total}", end="", flush=True)

    try:
        for output in session.export(chosen["id"], layout["rows"], layout["cols"], fmt,
                                     {"paper": args.paper}, progress):
            target = os.path.join(args.out, output.name)
            with open(target, "wb") as handle:
                handle.write(output.data)
            written.append(target)
    except SplitError as exc:
        print(f"\n! {exc.message}" + (f" ({exc.detail})" if exc.detail else ""), file=sys.stderr)
        return 2
    finally:
        session.clear()
    print()
    for path in written:
        print(f"저장: {path}")
    return 0


def main() -> None:
    try:
        sys.exit(run())
    except KeyboardInterrupt:
        print("\n취소했어요.")
        sys.exit(130)
    except SplitError as exc:
        print(f"! {exc.message}", file=sys.stderr)
        sys.exit(2)
