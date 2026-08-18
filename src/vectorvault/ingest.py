"""Markdown → memory-chunk helpers for ``scripts/ingest_memory.py``.

Pure functions (no boto3, no pydantic-at-call), so they unit-test without AWS and
can seed a project's accumulated notes/docs into VectorVault shared memory. Splits
each file into section-sized chunks so every chunk embeds to a focused vector
instead of one coarse whole-file embedding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)
_DESCRIPTION = re.compile(r"^description:\s*(.+?)\s*$", re.M)
_HEADING = re.compile(r"^(#{1,3}) +(.+)$", re.M)

DEFAULT_MAX_CHARS = 6000  # ~1.5K tokens/chunk


@dataclass
class Chunk:
    """One retrievable piece of a source file. ``heading`` is ``(whole file)`` /
    ``(intro)`` for headingless spans; real headings feed ``content_summary``."""

    heading: str
    content: str

    @property
    def is_section(self) -> bool:
        return not self.heading.startswith("(")


def parse_frontmatter(text: str) -> tuple[str | None, str]:
    """Return ``(description, body)``. Strips a leading ``--- … ---`` YAML block and
    extracts its ``description:`` (used as ``content_summary``). No YAML dependency —
    only the ``description`` line is read; the rest of the block is dropped."""
    m = _FRONTMATTER.match(text)
    if not m:
        return None, text
    desc_m = _DESCRIPTION.search(m.group(1))
    desc = desc_m.group(1).strip().strip('"').strip("'") if desc_m else None
    return desc, text[m.end():]


def chunk_markdown(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Split markdown into section chunks by ``#``/``##``/``###`` headings.

    A file with no headings becomes a single ``(whole file)`` chunk; any leading text
    before the first heading is an ``(intro)`` chunk; sections longer than
    ``max_chars`` are sub-split so each chunk stays focused.
    """
    text = text.strip()
    if not text:
        return []
    matches = list(_HEADING.finditer(text))
    sections: list[Chunk] = []
    if not matches:
        sections.append(Chunk("(whole file)", text))
    else:
        if matches[0].start() > 0 and text[: matches[0].start()].strip():
            sections.append(Chunk("(intro)", text[: matches[0].start()].strip()))
        for i, m in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append(Chunk(m.group(2).strip(), text[m.start():end].strip()))

    out: list[Chunk] = []
    for sec in sections:
        if not sec.content.strip():
            continue
        if len(sec.content) <= max_chars:
            out.append(sec)
        else:
            for j in range(0, len(sec.content), max_chars):
                tag = sec.heading if j == 0 else f"{sec.heading} (cont. {j // max_chars + 1})"
                out.append(Chunk(tag, sec.content[j:j + max_chars]))
    return out


def file_to_chunks(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> tuple[str | None, list[Chunk]]:
    """Parse frontmatter, then chunk the body. Returns ``(description, chunks)``."""
    desc, body = parse_frontmatter(text)
    return desc, chunk_markdown(body, max_chars)


def first_paragraph_summary(text: str, max_len: int = 400) -> str:
    """First non-empty paragraph, trimmed for ``content_summary`` when no heading/description."""
    text = text.strip()
    if not text:
        return ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        lines = para.splitlines()
        first = re.sub(r"^#+\s*", "", lines[0]).strip()
        if len(lines) == 1:
            body = first
        else:
            rest = " ".join(line.strip() for line in lines[1:] if line.strip())
            body = f"{first} {rest}".strip() if first else rest
        body = re.sub(r"\s+", " ", body).strip()
        if body:
            return body[:max_len]
    return text[:max_len]
