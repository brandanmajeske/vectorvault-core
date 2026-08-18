"""Markdown chunking + frontmatter parsing for the memory ingester (pure, no AWS)."""

from __future__ import annotations

from vectorvault.ingest import (
    chunk_markdown,
    file_to_chunks,
    first_paragraph_summary,
    parse_frontmatter,
)


def test_parse_frontmatter_strips_block_and_pulls_description():
    text = '---\nname: foo\ndescription: "A useful note about X"\nmetadata:\n  type: project\n---\n# Body\ncontent here'
    desc, body = parse_frontmatter(text)
    assert desc == "A useful note about X"
    assert body.startswith("# Body")
    assert "name: foo" not in body


def test_parse_frontmatter_absent_returns_text_unchanged():
    desc, body = parse_frontmatter("# just a heading\nno frontmatter")
    assert desc is None
    assert body == "# just a heading\nno frontmatter"


def test_chunk_markdown_splits_by_headings():
    text = "## Alpha\napple\n## Beta\nbanana\n### Beta-sub\nberry"
    chunks = chunk_markdown(text)
    assert [c.heading for c in chunks] == ["Alpha", "Beta", "Beta-sub"]
    assert chunks[0].content.startswith("## Alpha")
    assert all(c.is_section for c in chunks)


def test_chunk_markdown_no_headings_is_one_whole_file_chunk():
    chunks = chunk_markdown("just prose, no headings at all")
    assert len(chunks) == 1
    assert chunks[0].heading == "(whole file)"
    assert not chunks[0].is_section


def test_chunk_markdown_preamble_becomes_intro():
    chunks = chunk_markdown("lead-in text\n## First\nbody")
    assert chunks[0].heading == "(intro)"
    assert chunks[1].heading == "First"


def test_chunk_markdown_subsplits_oversize_sections():
    big = "## Big\n" + ("x" * 100 + "\n") * 60  # ~6.1KB body under one heading
    chunks = chunk_markdown(big, max_chars=2000)
    assert len(chunks) >= 3
    assert chunks[0].heading == "Big"
    assert "cont. 2" in chunks[1].heading
    assert all(len(c.content) <= 2000 for c in chunks)


def test_file_to_chunks_combines_frontmatter_and_chunking():
    text = "---\ndescription: sum\n---\n## H1\na\n## H2\nb"
    desc, chunks = file_to_chunks(text)
    assert desc == "sum"
    assert [c.heading for c in chunks] == ["H1", "H2"]


def test_chunk_markdown_empty_is_no_chunks():
    assert chunk_markdown("   \n  ") == []


def test_first_paragraph_summary_skips_blank_leading_lines():
    text = "\n\n# Title line\n\nBody sentence one.\n\nSecond paragraph."
    assert first_paragraph_summary(text) == "Title line"


def test_first_paragraph_summary_falls_back_to_prefix():
    assert first_paragraph_summary("one line only") == "one line only"
