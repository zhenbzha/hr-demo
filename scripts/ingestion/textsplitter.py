"""Split document text and tables into passages with source page numbers."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator

import tiktoken

from .page import Chunk, ContentBlock, Page

TOKENIZER = tiktoken.get_encoding("cl100k_base")
EMBEDDING_TOKEN_LIMIT = 8191
TABLE_PATTERN = re.compile(
    r"(?:<figure\b[^>]*>\s*)?<table\b.*?</table>(?:\s*</figure>)?",
    re.IGNORECASE | re.DOTALL,
)
ROW_PATTERN = re.compile(r"<tr\b[^>]*>.*?</tr>", re.IGNORECASE | re.DOTALL)
ROWSPAN_PATTERN = re.compile(r'\browspan=["\'](\d+)["\']', re.IGNORECASE)
TABLE_SECTION_PATTERN = re.compile(r"</?(?:thead|tbody|tfoot)\b[^>]*>", re.IGNORECASE)
HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+([^\n]+)")


def token_count(text: str) -> int:
    """Count tokens, treating tokenizer special-token strings as literal text."""
    return len(TOKENIZER.encode(text, disallowed_special=()))


def _combine(chunks: list[Chunk]) -> Chunk:
    """
    Join passage fragments and retain their distinct source context.

    Parameters
    ----------
    chunks : list of Chunk
        Fragments in reading order. Their text already contains separators.

    Returns
    -------
    Chunk
        Concatenated text with sorted, unique page numbers. Nonempty headings
        are deduplicated in encounter order and joined with ``; `` because
        one packed passage can contain more than one section.
    """
    return Chunk(
        text="".join(chunk.text for chunk in chunks),
        page_numbers=tuple(
            sorted({page for chunk in chunks for page in chunk.page_numbers})
        ),
        heading="; ".join(
            dict.fromkeys(chunk.heading for chunk in chunks if chunk.heading)
        ),
    )


def _layout_blocks(page: Page) -> Iterator[ContentBlock]:
    if page.blocks:
        yield from page.blocks
        return
    cursor = 0
    for table in TABLE_PATTERN.finditer(page.text):
        if cursor < table.start():
            yield ContentBlock(page.text[cursor : table.start()], (page.page_num,))
        yield ContentBlock(table[0], (page.page_num,), "table")
        cursor = table.end()
    if cursor < len(page.text):
        yield ContentBlock(page.text[cursor:], (page.page_num,))


def _connected_rows(rows: list[str]) -> Iterator[list[str]]:
    """Yield contiguous row groups joined by rowspan cells."""
    start = 0
    while start < len(rows):
        end = start + 1
        cursor = start
        while cursor < end:
            rowspans = [int(value) for value in ROWSPAN_PATTERN.findall(rows[cursor])]
            end = max(end, cursor + max(rowspans, default=1))
            cursor += 1
        yield rows[start:end]
        start = end


def _split_table_headers(
    rows: list[str], header_count: int | None
) -> tuple[list[str], list[str]] | None:
    """
    Separate repeatable table headers from body rows when safe.

    Parameters
    ----------
    rows : list of str
        Table rows in source order, including their HTML markup.
    header_count : int or None
        Header-row count from layout metadata. Only None triggers HTML
        inference: consecutive leading rows must contain ``th`` cells,
        without ``td`` cells or row-scoped headers.

    Returns
    -------
    tuple of (list of str, list of str) or None
        Header and body rows, or None when no body remains or a header's
        rowspan crosses into the body. Such tables must stay intact.
    """
    if header_count is None:
        header_count = 0
        for row in rows:
            if not re.search(r"<th\b", row, re.IGNORECASE) or re.search(
                r"<td\b|scope=[\"']row[\"']", row, re.IGNORECASE
            ):
                break
            header_count += 1
    header_rows, rows = rows[:header_count], rows[header_count:]
    header_crosses_body = any(
        index + int(span) > len(header_rows)
        for index, row in enumerate(header_rows)
        for span in ROWSPAN_PATTERN.findall(row)
    )
    if not rows or header_crosses_body:
        return None
    return header_rows, rows


class SentenceTextSplitter:
    """
    Split page content into passages while retaining source page numbers.

    Keep small tables intact. Split larger tables between row groups and
    repeat their column headers. A row group may exceed the target size,
    but exceeding the embedding model's input limit raises ValueError.
    """

    def __init__(
        self,
        max_tokens_per_section: int = 500,
        *,
        measure_input: Callable[[str, str], int] | None = None,
    ) -> None:
        """
        Set the target passage size and the function used to measure it.

        Parameters
        ----------
        max_tokens_per_section : int, default=500
            Target tokens per passage, between 1 and the embedding model's
            input limit. Sets the text-overlap target between passages in
            the same section to 10% of this value.
        measure_input : callable or None, default=None
            Function accepting ``text`` and ``heading`` and returning the
            token count of the full embedding input, including added titles
            and headings. By default, counts tokens in the passage text.
        """
        if not 1 <= max_tokens_per_section <= EMBEDDING_TOKEN_LIMIT:
            raise ValueError(
                "Chunk budget must be between 1 and the embedding token limit"
            )
        self.target_tokens = max_tokens_per_section
        self.measure_input = measure_input or (lambda text, heading: token_count(text))
        self.overlap_tokens = max_tokens_per_section // 10

    def _fits(self, text: str, heading: str, limit: int | None = None) -> bool:
        return self.measure_input(text, heading) <= (limit or self.target_tokens)

    def _split_long_span(self, text: str, heading: str) -> Iterator[str]:
        """Yield token-bounded text, preferring word or sentence boundaries."""
        while text:
            if self._fits(text, heading):
                yield text
                return
            lower, upper = 0, len(text)
            while lower < upper:
                midpoint = (lower + upper + 1) // 2
                if self._fits(text[:midpoint], heading):
                    lower = midpoint
                else:
                    upper = midpoint - 1
            if lower == 0:
                raise ValueError(
                    f"Section context leaves no token budget for content: {heading!r}"
                )
            boundaries = list(re.finditer(r"\s+|[.,;:!?。！？]", text[:lower]))
            split_at = (
                boundaries[-1].end()
                if boundaries and boundaries[-1].end() >= lower // 2
                else lower
            )
            yield text[:split_at]
            text = text[split_at:]

    def _text_chunks(self, block: ContentBlock, heading: str) -> Iterator[Chunk]:
        sentences = re.findall(
            r".+?(?:[.!?]+(?=\s|$)|[。！？]|\n\n|$)", block.text, re.DOTALL
        )
        for sentence in sentences:
            for fragment in self._split_long_span(sentence, heading):
                yield Chunk(fragment, block.page_numbers, heading)

    def _table_chunks(self, block: ContentBlock, heading: str) -> Iterator[Chunk]:
        """
        Keep small tables intact and pack larger tables by connected rows.

        Repeat safely separable headers and preserve the surrounding table
        markup, including captions, in every split. Rows linked by rowspan
        cells stay together; headers spanning body rows keep the table whole.

        Parameters
        ----------
        block : ContentBlock
            Native table HTML, source pages, and optional header-row count.
        heading : str
            Section context included when measuring the embedding input.

        Yields
        ------
        Chunk
            Table passage with its source pages and heading. An indivisible
            row group or table may exceed the target passage size, but not
            the embedding model's input limit.

        Raises
        ------
        ValueError
            An oversized table has no row boundaries, or an indivisible
            table or row group exceeds the embedding input limit.
        """
        if self._fits(block.text, heading):
            yield Chunk(block.text, block.page_numbers, heading)
            return
        row_matches = list(ROW_PATTERN.finditer(block.text))
        if not row_matches:
            raise ValueError("Oversized table has no row boundaries")
        row_sections = _split_table_headers(
            [match[0] for match in row_matches], block.table_header_rows
        )
        if row_sections is None:
            if not self._fits(block.text, heading, EMBEDDING_TOKEN_LIMIT):
                raise ValueError(
                    "Table headers cannot be split safely within the embedding limit; "
                    "revise the source table"
                )
            yield Chunk(block.text, block.page_numbers, heading)
            return
        header_rows, rows = row_sections
        # Remove section wrappers so row-group splits cannot leave unmatched tags.
        opening = TABLE_SECTION_PATTERN.sub("", block.text[: row_matches[0].start()])
        closing = TABLE_SECTION_PATTERN.sub("", block.text[row_matches[-1].end() :])
        header = "".join(header_rows)

        def render(body_rows: list[str]) -> str:
            return opening + header + "".join(body_rows) + closing

        pending: list[str] = []
        for row_group in _connected_rows(rows):
            if pending and not self._fits(render(pending + row_group), heading):
                yield Chunk(render(pending), block.page_numbers, heading)
                pending = []
            if not self._fits(render(row_group), heading, EMBEDDING_TOKEN_LIMIT):
                raise ValueError(
                    f"An indivisible table row group exceeds {EMBEDDING_TOKEN_LIMIT} "
                    f"embedding tokens on pages {block.page_numbers}; revise the source table"
                )
            pending.extend(row_group)
        if pending:
            yield Chunk(render(pending), block.page_numbers, heading)

    def _passages(self, pages: list[Page]) -> Iterator[tuple[Chunk, bool]]:
        """
        Yield text and table fragments with their current section context.

        Carry headings across page boundaries. Explicit block headings,
        including an empty heading, override that context; infer Markdown
        headings only when block heading metadata is absent.

        Parameters
        ----------
        pages : list of Page
            Parsed pages in reading order, with layout blocks when available.

        Yields
        ------
        tuple of (Chunk, bool)
            Fragment and a flag identifying table content. The caller uses
            this flag to keep tables out of repeated text overlap.
        """
        heading = ""
        for page_index, page in enumerate(pages):
            for block in _layout_blocks(page):
                if block.heading is not None:
                    heading = block.heading
                if block.kind == "table":
                    for chunk in self._table_chunks(block, heading):
                        yield chunk, True
                    continue
                text = ("\n" if page_index else "") + block.text
                if block.heading is not None:
                    fragment = ContentBlock(text, block.page_numbers)
                    for chunk in self._text_chunks(fragment, heading):
                        yield chunk, False
                    continue
                for section in re.split(r"(?m)(?=^#{1,6}[ \t]+)", text):
                    match = HEADING_PATTERN.match(section)
                    if match:
                        heading = match[2].strip()
                    fragment = ContentBlock(section, block.page_numbers)
                    for chunk in self._text_chunks(fragment, heading):
                        yield chunk, False

    def _overlap(self, previous: list[Chunk], following: Chunk) -> list[Chunk]:
        """
        Select whole trailing fragments matching the next fragment's heading.

        Stop at the first heading change or budget failure; do not trim
        fragments to fill the overlap allowance. The overlap allowance counts
        text alone, while the combined overlap and following fragment must
        fit the target size with their full embedding context.

        Parameters
        ----------
        previous : list of Chunk
            Candidate trailing text fragments after the most recent table,
            in reading order.
        following : Chunk
            Next fragment, whose heading must match every repeated fragment.

        Returns
        -------
        list of Chunk
            Selected suffix of previous fragments, still in reading order.
        """
        selected: list[Chunk] = []
        for chunk in reversed(previous):
            if chunk.heading != following.heading:
                break
            candidate = [chunk, *selected]
            if (
                token_count("".join(part.text for part in candidate))
                > self.overlap_tokens
            ):
                break
            if not self._fits(
                "".join(part.text for part in [*candidate, following]),
                following.heading,
            ):
                break
            selected = candidate
        return selected

    def split_pages(self, pages: list[Page]) -> Iterator[Chunk]:
        """
        Group text and tables into passages that fit the token limit.

        Retain section headings and source page numbers. Overlap between
        passages repeats trailing text from the same section.

        Parameters
        ----------
        pages : list of Page
            Analyzed pages from one PDF in reading order. Use their text
            and table blocks when available, otherwise parse their
            Markdown and HTML text.

        Yields
        ------
        Chunk
            Passage text, section heading, and source page numbers
            starting at zero.

        Raises
        ------
        ValueError
            Added titles or headings leave no room for passage text,
            or a table cannot fit within the embedding model's input limit.
        """
        pending: list[Chunk] = []
        overlap_start = 0
        for chunk, is_table in self._passages(pages):
            if not chunk.text.strip():
                continue
            adjacent = bool(
                pending and min(chunk.page_numbers) <= max(pending[-1].page_numbers) + 1
            )
            same_section = adjacent and pending[-1].heading == chunk.heading
            combined = _combine([*pending, chunk])
            if pending and (
                not adjacent or not self._fits(combined.text, combined.heading)
            ):
                yield _combine(pending)
                pending = (
                    self._overlap(pending[overlap_start:], chunk)
                    if same_section and not is_table
                    else []
                )
                overlap_start = 0
            pending.append(chunk)
            if is_table:
                overlap_start = len(pending)
        if pending:
            yield _combine(pending)
