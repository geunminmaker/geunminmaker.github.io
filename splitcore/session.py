"""The conversation between the UI and the engine.

One :class:`Session` lives for as long as the app is open.  The UI adds the
files the user picked, shows the candidates, asks for layouts and finally asks
for an export.  All return values are plain dict / list / bytes so they cross
the Web Worker boundary (and print nicely in the CLI).
"""
from __future__ import annotations

from typing import Iterator

from . import imaging
from .errors import SplitError
from .export import FORMATS, export, normalize_format, page_for
from .export.base import OutputFile, Progress
from .grids import SPLIT_COUNTS, layouts_for
from .loaders import load_file
from .sources import Source
from .splitter import SplitPlan, make_plan, output_stem

#: Never show more candidates than this from one batch (a 500-page PDF ...).
MAX_SOURCES = 300


class Session:
    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}
        self._order: list[str] = []
        self._counter = 0

    # ------------------------------------------------------------ files
    def clear(self) -> None:
        for source in self._sources.values():
            source.release()
            close = getattr(source, "close", None)
            if close:
                close()
        self._sources.clear()
        self._order.clear()
        # ids start over: the page replays the same files into a fresh engine after a restart
        # and must get the same ids again
        self._counter = 0

    def add_file(self, name: str, data: bytes, password: str | None = None) -> dict:
        """Load one picked file.  Returns the candidates found in it."""
        sources = load_file(name, data, password)
        if not sources:
            raise SplitError("NO_IMAGE")
        room = MAX_SOURCES - len(self._order)
        # a loader that stopped early (PDF with more pages than MAX_PAGES) says how many there were
        total = max(len(sources), getattr(sources[0], "origin_total", 0) or 0)
        truncated = len(sources) > room or total > len(sources)
        added = []
        for source in sources[:max(0, room)]:
            self._counter += 1
            source.id = f"s{self._counter}"
            self._sources[source.id] = source
            self._order.append(source.id)
            added.append(source.meta())
        return {"file": name, "sources": added, "truncated": truncated, "total": total}

    def sources(self) -> list[dict]:
        return [self._sources[i].meta() for i in self._order]

    def get(self, source_id: str) -> Source:
        try:
            return self._sources[source_id]
        except KeyError:
            raise SplitError("NOT_FOUND", detail=f"id={source_id!r}") from None

    def focus(self, source_id: str) -> None:
        """Keep the pixels of the chosen picture, free everything else."""
        for key, source in self._sources.items():
            if key != source_id:
                source.release()

    # ------------------------------------------------------------ pictures
    def thumbnail(self, source_id: str, max_px: int = 360) -> tuple[bytes, str]:
        source = self.get(source_id)
        thumb = source.thumbnail(int(max_px))
        return imaging.encode_preview(thumb, int(max_px), quality=80)

    def preview(self, source_id: str, max_px: int = 1600) -> tuple[bytes, str]:
        source = self.get(source_id)
        self.focus(source_id)
        return imaging.encode_preview(source.thumbnail(int(max_px)), int(max_px), quality=88)

    # ------------------------------------------------------------ planning
    def layouts(self, source_id: str, count: int) -> list[dict]:
        width, height = self.get(source_id).size
        return layouts_for(int(count), width, height)

    def all_layouts(self, source_id: str) -> dict:
        """Layouts for every offered split count (one round trip for the UI)."""
        width, height = self.get(source_id).size
        return {str(n): layouts_for(n, width, height) for n in SPLIT_COUNTS}

    def _plan(self, source: Source, rows: int, cols: int, options: dict | None = None) -> SplitPlan:
        width, height = source.size
        limit = imaging.MAX_TILE_PIXELS
        try:  # a device with little memory may ask for smaller tiles
            wanted = int((options or {}).get("max_pixels") or 0)
        except (TypeError, ValueError):
            wanted = 0
        if wanted > 0:
            limit = max(1_000_000, min(limit, wanted))
        return make_plan(width, height, rows, cols, limit)

    def describe(self, source_id: str, rows: int, cols: int, fmt: str | None = None,
                 options: dict | None = None) -> dict:
        """What an export would produce (sizes, page, file names) without doing it."""
        source = self.get(source_id)
        plan = self._plan(source, rows, cols, options)
        info = {"plan": plan.to_dict(), "stem": output_stem(source.origin, plan, source.label)}
        if fmt:
            key = normalize_format(fmt)
            info["format"] = key
            info["kind"] = FORMATS[key]["kind"]
            info["vector"] = bool(key == "pdf" and source.vector)
            if FORMATS[key]["kind"] == "document":
                # the same call as the export makes, so the two cannot drift apart
                info["page"] = page_for(source, plan, key, options).to_dict()
        return info

    # ------------------------------------------------------------ export
    def export(self, source_id: str, rows: int, cols: int, fmt: str,
               options: dict | None = None, progress: Progress = None) -> Iterator[OutputFile]:
        source = self.get(source_id)
        self.focus(source_id)
        plan = self._plan(source, rows, cols, options)
        try:
            yield from export(source, plan, fmt, options, progress)
        except SplitError:
            raise
        except MemoryError:
            raise SplitError("OUT_OF_MEMORY") from None
        except Exception as exc:
            raise SplitError("INTERNAL", detail=f"{type(exc).__name__}: {exc}") from exc
