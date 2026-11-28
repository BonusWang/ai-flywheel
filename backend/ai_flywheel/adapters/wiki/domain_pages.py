"""Project-domain pages: built from the Wiki's template, completed by lossless increments.

A domain page (a table structure, a service card, a project configuration page) is not
written once; it is completed over many sessions. It therefore has two write shapes:

* ``create`` assembles a new page from the planner's draft and the page template the
  Wiki's deposit contract names for that domain;
* ``update`` merges an increment into the page that exists.

The merge is additive by construction. Every line of the page is kept, in its order, and
only ``updated`` changes in the frontmatter. The one exception is the template's
placeholder line 「无」: it stood for an empty section and gives way when that section
receives its first content. Content the target already holds is not written twice: a row
the target table already has, or a line or block the target section already has, is
dropped and counted, never duplicated. The unit of "already there" is that target and
nothing wider — two sections of one page may well hold the same row or the same line
(a parameter every environment shares, a note two tables both need), so a line another
section holds is never a reason to drop it here.

Everything here is a pure function of its inputs: no model call, no clock, no file access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from ai_flywheel.adapters.wiki.schema import split_frontmatter
from ai_flywheel.adapters.wiki.validator import _section_name
from ai_flywheel.core.markdown_utils import trim_blank_lines

# The line the Wiki's templates prescribe for a section that has no content yet.
PLACEHOLDER = "无"

# The frontmatter keys in the order a Wiki page conventionally lists them.
_PAGE_FIELD_ORDER = (
    "title",
    "description",
    "type",
    "project_id",
    "knowledge_kind",
    "status",
    "load_policy",
    "default_load",
    "created",
    "updated",
    "sources",
)

# A table page is named after one table; these mark a name that lists several.
_TABLE_LIST_SEPARATORS_RE = re.compile(r"[,，、;；+|&/]")

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_ROW_RE = re.compile(r"^\s*\|")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+")
_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")
_UPDATED_LINE_RE = re.compile(r"^([ \t]*updated:[ \t]*)(\S+)(.*)$")
_CREATED_LINE_RE = re.compile(r"^[ \t]*created:")


@dataclass
class MergeResult:
    """Outcome of merging an increment into a domain page."""

    merged: str = ""  # the whole page after the merge; unchanged when nothing was added
    added_text: str = ""  # exactly what the merge adds, grouped by section (the approval preview)
    added: int = 0  # lines added to the page
    duplicates: int = 0  # rows and blocks the page already held
    placeholders: list[str] = field(default_factory=list)  # sections whose 「无」 gave way
    problems: list[str] = field(default_factory=list)  # why the increment cannot be merged

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def has_additions(self) -> bool:
        return self.ok and self.added > 0


@dataclass
class _Heading:
    index: int
    level: int
    text: str


@dataclass
class _Section:
    name: str
    start: int  # the heading line
    end: int  # one past the section's last line (the next heading's index, or the end)


@dataclass
class _Table:
    start: int  # first row
    end: int  # one past the last row
    header: list[str]


@dataclass
class _Chunk:
    kind: str  # "rows" | "text" | "code"
    lines: list[str]
    header: list[str] | None = None  # rows: the header the increment declared before them


@dataclass
class _IncrementSection:
    name: str
    chunks: list[_Chunk]


def is_single_table_name(stem: str) -> bool:
    """Whether a page name names one table: no separator that would list several."""
    return bool(stem.strip()) and not _TABLE_LIST_SEPARATORS_RE.search(stem)


def _meaningful_lines(text: str) -> list[str]:
    """The lines of a page that a merge must keep: not blank, not a date, not a placeholder."""
    return [
        line.rstrip()
        for line in text.replace("\r\n", "\n").split("\n")
        if line.strip() and line.strip() != PLACEHOLDER and not _UPDATED_LINE_RE.match(line)
    ]


def preserves_existing(existing: str, merged: str) -> bool:
    """Whether ``merged`` still carries every meaningful line of ``existing``, in order.

    The lossless guarantee is checked on the result instead of being trusted from the
    merge algorithm: blank lines, the ``updated`` date and a placeholder line that gave
    way are the only lines a merge may leave out.
    """
    kept = iter(_meaningful_lines(merged))
    return all(any(candidate == line for candidate in kept) for line in _meaningful_lines(existing))


def _cells(row: str) -> list[str]:
    """The cells of a table row, trimmed. An escaped pipe belongs to its cell."""
    core = row.strip()
    if core.startswith("|"):
        core = core[1:]
    if core.endswith("|") and not core.endswith("\\|"):
        core = core[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", core)]


def _is_separator(row: str) -> bool:
    cells = _cells(row)
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(cell) for cell in cells)


def _row_key(cells: list[str]) -> str:
    """A row regardless of the whitespace its cells are padded with."""
    return "|".join(" ".join(cell.split()) for cell in cells)


def _frontmatter_end(lines: list[str]) -> int:
    """Index of the line that closes the frontmatter, or -1 when there is none."""
    if not lines or lines[0].lstrip("\ufeff").strip() != "---":
        return -1
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index
    return -1


def _fence_mask(lines: list[str]) -> list[bool]:
    """Which lines sit inside a fenced code block, the fence lines included."""
    mask = [False] * len(lines)
    fence: str | None = None
    for index, line in enumerate(lines):
        mark = _FENCE_RE.match(line)
        if fence is not None:
            mask[index] = True
            if mark and mark.group(1)[0] == fence:
                fence = None
        elif mark:
            fence = mark.group(1)[0]
            mask[index] = True
    return mask


def _headings(lines: list[str], mask: list[bool]) -> list[_Heading]:
    """ATX headings outside fenced code blocks."""
    found: list[_Heading] = []
    for index, line in enumerate(lines):
        if mask[index]:
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            found.append(_Heading(index, len(heading.group(1)), heading.group(2)))
    return found


def _sections(lines: list[str], mask: list[bool]) -> list[_Section]:
    second_level = [heading for heading in _headings(lines, mask) if heading.level == 2]
    return [
        _Section(
            _section_name(heading.text),
            heading.index,
            second_level[position + 1].index if position + 1 < len(second_level) else len(lines),
        )
        for position, heading in enumerate(second_level)
    ]


def _tables(lines: list[str], mask: list[bool], start: int, end: int) -> list[_Table]:
    """Tables inside a line range: runs of consecutive rows outside fenced code."""
    tables: list[_Table] = []
    index = start
    while index < end:
        if mask[index] or not _ROW_RE.match(lines[index]):
            index += 1
            continue
        run_end = index
        while run_end < end and not mask[run_end] and _ROW_RE.match(lines[run_end]):
            run_end += 1
        if run_end - index >= 2:
            tables.append(_Table(index, run_end, _cells(lines[index])))
        index = run_end
    return tables


def _code_blocks(lines: list[str], mask: list[bool]) -> set[str]:
    blocks: set[str] = set()
    start: int | None = None
    for index in range(len(lines) + 1):
        inside = index < len(lines) and mask[index]
        if inside and start is None:
            start = index
        elif not inside and start is not None:
            blocks.add("\n".join(line.strip() for line in lines[start:index]))
            start = None
    return blocks


@dataclass
class _Scope:
    """What one target already holds: the rows of its table, its own lines, its fenced blocks."""

    rows: set[str]
    text_lines: set[str]
    code_blocks: set[str]


def _scope(body: list[str], mask: list[bool], section: _Section | None, table: _Table | None) -> _Scope:
    """What the place an increment section is merged into already holds.

    That place is the target section, and for rows the table they join: the section's
    last table. Nothing wider. A whole-page index would read a row another section holds
    as one this section already has, and drop it.
    """
    rows = {_row_key(_cells(body[index])) for index in range(table.start, table.end)} if table is not None else set()
    if section is None:
        # A section the page does not have yet: whatever it will hold is new.
        return _Scope(rows, set(), set())
    text_lines: set[str] = set()
    for index in range(section.start + 1, section.end):
        if not mask[index] and not _ROW_RE.match(body[index]) and body[index].strip():
            text_lines.add(body[index].strip())
    inside = mask[section.start + 1 : section.end]
    return _Scope(rows, text_lines, _code_blocks(body[section.start + 1 : section.end], inside))


def _content_end(lines: list[str], start: int, end: int) -> int:
    """One past the last non-blank line of a range (``start`` when the range is blank)."""
    index = end - 1
    while index >= start and not lines[index].strip():
        index -= 1
    return index + 1


def _draft_layout(lines: list[str], mask: list[bool]) -> tuple[list[str], dict[str, list[str]]]:
    """(lines before the first H2, sections by base name) of a draft body, H1 lines left out."""
    headings = _headings(lines, mask)
    titles = {heading.index for heading in headings if heading.level == 1}
    second_level = [heading for heading in headings if heading.level == 2]
    first = second_level[0].index if second_level else len(lines)
    preamble = [line.rstrip() for index, line in enumerate(lines[:first]) if line.strip() and index not in titles]
    sections: dict[str, list[str]] = {}
    for position, heading in enumerate(second_level):
        end = second_level[position + 1].index if position + 1 < len(second_level) else len(lines)
        chunk = trim_blank_lines([line.rstrip() for line in lines[heading.index + 1 : end]])
        sections.setdefault(_section_name(heading.text), []).extend(chunk)
    return preamble, sections


def _parse_increment(increment: str) -> tuple[list[_IncrementSection], str]:
    """The sections of an increment, or a problem that says why it cannot be read.

    An increment is organised as ``## section`` followed by only what is new there. A
    frontmatter block and a level-1 title are ignored: they describe the page, which
    keeps its own.
    """
    lines = (increment or "").replace("\r\n", "\n").split("\n")
    close = _frontmatter_end(lines)
    if close >= 0:
        lines = lines[close + 1 :]
    mask = _fence_mask(lines)
    headings = _headings(lines, mask)
    second_level = [heading for heading in headings if heading.level == 2]
    titles = {heading.index for heading in headings if heading.level == 1}
    first = second_level[0].index if second_level else len(lines)
    if any(line.strip() and not mask[index] and index not in titles for index, line in enumerate(lines[:first])):
        return [], "增量在小节之前带有正文：增量只按「## 小节名 + 该小节的新增内容」组织"
    sections: list[_IncrementSection] = []
    for position, heading in enumerate(second_level):
        end = second_level[position + 1].index if position + 1 < len(second_level) else len(lines)
        chunks: list[_Chunk] = []
        index = heading.index + 1
        while index < end:
            line = lines[index]
            if not line.strip() or index in titles:
                index += 1
                continue
            if mask[index]:  # a fenced block moves as a whole
                block_end = index + 1
                while block_end < end and mask[block_end]:
                    block_end += 1
                chunks.append(_Chunk("code", [item.rstrip() for item in lines[index:block_end]]))
                index = block_end
                continue
            if _ROW_RE.match(line):
                if _is_separator(line):
                    index += 1
                    continue
                header = None
                if index + 1 < end and not mask[index + 1] and _is_separator(lines[index + 1]):
                    header = _cells(line)
                    index += 2
                rows: list[str] = []
                while index < end and not mask[index] and _ROW_RE.match(lines[index]) and not _is_separator(lines[index]):
                    rows.append(lines[index].strip())
                    index += 1
                chunks.append(_Chunk("rows", rows, header=header))
                continue
            block: list[str] = []
            while index < end and lines[index].strip() and not mask[index] and not _ROW_RE.match(lines[index]):
                block.append(lines[index].rstrip())
                index += 1
            chunks.append(_Chunk("text", block))
        sections.append(_IncrementSection(_section_name(heading.text), chunks))
    return sections, ""


def _render_row(cells: list[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"


def _match_section(sections: list[_Section], name: str) -> _Section | None:
    """The page section an increment section names: exactly, or by a unique name prefix.

    A hand-written page may call a template section by a shorter name (字段语义 for
    字段语义表); a unique prefix on either side is the same section. An ambiguous or
    missing name is not a match.
    """
    exact = [section for section in sections if section.name == name]
    if exact:
        return exact[0]
    near = [
        section
        for section in sections
        if len(section.name) >= 2
        and len(name) >= 2
        and (section.name.startswith(name) or name.startswith(section.name))
    ]
    return near[0] if len(near) == 1 else None


def _placeholder_index(lines: list[str], section: _Section) -> int:
    """The line holding 「无」 when it is all a section has, else -1."""
    content = [index for index in range(section.start + 1, section.end) if lines[index].strip()]
    if len(content) == 1 and lines[content[0]].strip() == PLACEHOLDER:
        return content[0]
    return -1


def _needs_blank(body: list[str], end: int, blocks: list[list[str]]) -> bool:
    """Whether text added at ``end`` opens with a blank line.

    A list item continues the list the section ends with without one; anything else is
    a new block.
    """
    return not (blocks and _LIST_ITEM_RE.match(body[end - 1]) and _LIST_ITEM_RE.match(blocks[0][0]))


def _bump_updated(lines: list[str], close: int, today: str) -> None:
    """Set ``updated`` in the frontmatter, leaving every other character of it alone."""
    for index in range(1, close):
        match = _UPDATED_LINE_RE.match(lines[index])
        if match:
            if match.group(2) != today:
                lines[index] = f"{match.group(1)}{today}{match.group(3)}"
            return
    created = next((index for index in range(1, close) if _CREATED_LINE_RE.match(lines[index])), close - 1)
    lines.insert(created + 1, f"updated: {today}")


def merge_domain_increment(
    existing: str, increment: str, *, template_sections: Any = (), today: str = ""
) -> MergeResult:
    """Merge an increment into an existing domain page without losing anything it holds.

    ``template_sections`` are the section names the page's template declares: an
    increment may fill a section the page does not have yet only when the template
    declares it. ``today`` (``YYYY-MM-DD``) becomes the page's ``updated`` date when the
    merge adds something; without it the frontmatter is left untouched, as the plan-stage
    dry check needs.

    The result carries problems when the increment cannot be merged as it stands: an
    unknown section, a row whose columns do not fit the section's table, or content
    outside any section. Nothing is guessed to make it fit.
    """
    lines = (existing or "").replace("\r\n", "\n").split("\n")
    close = _frontmatter_end(lines)
    if close < 0:
        return MergeResult(problems=["目标页没有 frontmatter，无法按领域页合并"])
    sections, problem = _parse_increment(increment)
    if problem:
        return MergeResult(problems=[problem])
    if not sections:
        return MergeResult(problems=["增量为空：没有可并入的小节"])

    body = lines[close + 1 :]
    mask = _fence_mask(body)
    page_sections = _sections(body, mask)
    template_index = {_section_name(name): position for position, name in enumerate(template_sections or ())}

    result = MergeResult()
    inserts: dict[int, list[str]] = {}  # body line -> lines inserted before it
    replacements: dict[int, list[str]] = {}  # a placeholder line -> what takes its place
    new_sections: list[tuple[int, int, list[str]]] = []  # (line inserted before, template position, lines)
    preview: list[tuple[str, list[str]]] = []
    started_tables: dict[str, list[str]] = {}  # header of a table this merge starts, by section
    seen: set[str] = set()

    def fresh_rows(chunk: _Chunk, table: _Table | None, label: str, held: set[str]) -> list[str] | None:
        """Rows of the chunk the target table does not hold yet; None once a problem is recorded.

        ``held`` is the set of row keys that table holds; a row kept here joins it, so a
        later chunk of the same section cannot add the same row again.
        """
        current = table.header if table is not None else started_tables.get(label)
        header = chunk.header if chunk.header is not None else current
        if header is None:
            result.problems.append(f"小节「{label}」在目标页中没有表格，增量行需要先给表头行与分隔行")
            return None
        if chunk.header is not None and current is not None and _row_key(current) != _row_key(chunk.header):
            result.problems.append(
                f"小节「{label}」的增量表头（{' | '.join(chunk.header)}）与目标页表头（{' | '.join(current)}）不一致"
            )
            return None
        fresh: list[str] = []
        for row in chunk.lines:
            cells = _cells(row)
            if len(cells) != len(header):
                result.problems.append(
                    f"小节「{label}」的增量行有 {len(cells)} 列，该小节的表头有 {len(header)} 列：{row}"
                )
                return None
            key = _row_key(cells)
            if key in held:
                result.duplicates += 1
                continue
            held.add(key)
            fresh.append(row)
        if fresh and chunk.header is not None and current is None:
            started_tables[label] = list(chunk.header)
        return fresh

    def fresh_block(chunk: _Chunk, held_text: set[str], held_code: set[str]) -> list[str]:
        """The lines of the chunk the target section does not hold yet; the ones it holds are counted."""
        if chunk.kind == "code":
            if "\n".join(line.strip() for line in chunk.lines) in held_code:
                result.duplicates += 1
                return []
            return list(chunk.lines)
        # A placeholder line in an increment states that the section has nothing; it is never
        # content, so it is never added — a section the page lacks does not gain one either.
        fresh = [line for line in chunk.lines if line.strip() != PLACEHOLDER and line.strip() not in held_text]
        result.duplicates += len(chunk.lines) - len(fresh)
        return fresh

    for section in sections:
        if section.name in seen:
            result.problems.append(f"增量中小节「{section.name}」出现了多次")
            break
        seen.add(section.name)
        target = _match_section(page_sections, section.name)
        if target is None and section.name not in template_index:
            result.problems.append(f"小节「{section.name}」不在目标页中，也不是页面模板声明的小节")
            break
        tables = _tables(body, mask, target.start + 1, target.end) if target is not None else []
        table = tables[-1] if tables else None
        # Read per section, and for rows per table: what another section holds is not a duplicate here.
        held = _scope(body, mask, target, table)
        head: list[str] = []  # header and separator when the merge starts the section's table
        rows: list[str] = []
        blocks: list[list[str]] = []
        for chunk in section.chunks:
            if not chunk.lines:
                continue
            if chunk.kind != "rows":
                block = fresh_block(chunk, held.text_lines, held.code_blocks)
                if block:
                    blocks.append(block)
                continue
            fresh = fresh_rows(chunk, table, section.name, held.rows)
            if result.problems:
                break
            if table is None and fresh and not head and section.name in started_tables:
                head = [_render_row(started_tables[section.name]), "|" + "---|" * len(started_tables[section.name])]
            rows.extend(fresh)
        if result.problems or not (rows or blocks):
            continue

        # What the section gains, in the order it will show it: the table, then the text.
        text = [line for position, block in enumerate(blocks) for line in ([""] if position else []) + block]
        inner = head + rows + ([""] + text if text and (head or rows) else text)
        preview.append((section.name, inner))
        result.added += len([line for line in inner if line.strip()])
        if target is None:
            following = next(
                (
                    other.start
                    for other in page_sections
                    if template_index.get(other.name, len(template_index)) > template_index[section.name]
                ),
                None,
            )
            content = [f"## {section.name}", "", *inner, ""]
            if following is None:
                inserts.setdefault(_content_end(body, 0, len(body)), []).extend(["", *content])
            else:
                new_sections.append((following, template_index[section.name], content))
            continue
        end = _content_end(body, target.start + 1, target.end)
        placeholder = _placeholder_index(body, target)
        if placeholder >= 0:
            replacements[placeholder] = inner
            result.placeholders.append(target.name)
        elif table is not None:
            if table.end == end:  # the table is the section's last content: text follows the new rows
                inserts.setdefault(table.end, []).extend(inner)
            else:
                inserts.setdefault(table.end, []).extend(rows)
                inserts.setdefault(end, []).extend(([""] if _needs_blank(body, end, blocks) else []) + text)
        elif end > target.start + 1:  # the section has content of its own
            lead = [] if not head and not rows and not _needs_blank(body, end, blocks) else [""]
            inserts.setdefault(end, []).extend(lead + inner)
        else:  # an empty section: content takes the place the template left for it
            blank_after_heading = target.end > target.start + 1 and not body[target.start + 1].strip()
            inserts.setdefault(target.start + 2 if blank_after_heading else target.start + 1, []).extend(
                ([] if blank_after_heading else [""]) + inner + [""]
            )

    if result.problems:
        return result
    for at, _, content in sorted(new_sections, key=lambda item: item[1]):  # template order
        inserts.setdefault(at, []).extend(content)
    for index in sorted(set(inserts) | set(replacements), reverse=True):
        if index in replacements:
            body[index : index + 1] = replacements[index]
        else:
            body[index:index] = inserts[index]
    if result.added and today:
        _bump_updated(lines, close, today)
        close = _frontmatter_end(lines)
    result.merged = "\n".join(lines[: close + 1] + body)
    result.added_text = "\n".join(
        line for name, shown in preview for line in (f"## {name}", "", *shown, "")
    ).rstrip("\n")
    return result


def page_section_sketch(text: str) -> dict[str, list[str] | None]:
    """What a planner needs to write an increment a page can merge: section -> table header.

    ``None`` marks a section that holds text or list lines rather than a table. Only
    structure is read: the header row of each table, never one of its data rows. A run of
    rows without a separator row under its first line has no header to show, so it counts
    as text: nothing a page states about itself leaves it this way.
    """
    _, body = split_frontmatter(text or "")
    lines = body.replace("\r\n", "\n").split("\n")
    mask = _fence_mask(lines)
    sketch: dict[str, list[str] | None] = {}
    for section in _sections(lines, mask):
        headed = [
            table
            for table in _tables(lines, mask, section.start + 1, section.end)
            if table.start + 1 < table.end and _is_separator(lines[table.start + 1])
        ]
        sketch.setdefault(section.name, list(headed[-1].header) if headed else None)
    return sketch


def _draft_frontmatter(content: str) -> tuple[dict[str, Any], str, str]:
    """(frontmatter mapping or {}, body, problem) of a planner's draft."""
    fm_text, body = split_frontmatter((content or "").replace("\r\n", "\n"))
    if not fm_text:
        return {}, body, ""
    try:
        loaded = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, body, "草稿的 frontmatter 不是合法 YAML"
    if not isinstance(loaded, dict):
        return {}, body, "草稿的 frontmatter 不是映射"
    return loaded, body, ""


def build_domain_page(
    *,
    title: str,
    summary: str,
    content: str,
    page_type: str,
    project_id: str,
    template: dict[str, Any],
    today: str,
    source_sessions: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Assemble a new domain page from the planner's draft and the domain's page template.

    The draft supplies the knowledge: its frontmatter values and its text. The template
    supplies the shape: the page type, the knowledge kind, the load policy and the body
    sections in their declared order; a section the draft leaves out gets the template's
    placeholder, never an invented fact. Fields the engine can know are set by the engine
    (project, dates, provenance); a draft's own values for them are not used.

    Returns ``(page, problems)``; with problems the page is empty.
    """
    draft, body, problem = _draft_frontmatter(content)
    if problem:
        return "", [problem]
    lines = body.split("\n")
    mask = _fence_mask(lines)
    titles = [heading for heading in _headings(lines, mask) if heading.level == 1]
    if len(titles) > 1:
        return "", ["草稿含多个一级标题"]
    named = next(
        (
            str(value)
            for value in (draft.get("title"), titles[0].text if titles else "", title)
            if str(value or "").strip()
        ),
        "",
    )
    page_title = " ".join(named.split())
    preamble, supplied = _draft_layout(lines, mask)
    template_sections = [_section_name(name) for name in template.get("body_sections") or []]

    page_body = [f"# {page_title}", ""]
    if template_sections:
        unknown = [name for name in supplied if name not in template_sections]
        if unknown:
            return "", [f"草稿小节「{unknown[0]}」不是模板 {template.get('id')} 声明的小节"]
        page_body += [*preamble, ""] if preamble else []
        for name in template_sections:
            page_body += [f"## {name}", "", *(supplied.get(name) or [PLACEHOLDER]), ""]
    else:
        title_lines = {heading.index for heading in titles}
        page_body += trim_blank_lines([line.rstrip() for index, line in enumerate(lines) if index not in title_lines])

    load_policy = str(template.get("load_policy") or draft.get("load_policy") or "").strip()
    engine_set: dict[str, Any] = {
        "title": page_title,
        "type": page_type,
        "project_id": project_id,
        "created": today,
        "updated": today,
    }
    if template.get("knowledge_kind") or draft.get("knowledge_kind"):
        engine_set["knowledge_kind"] = template.get("knowledge_kind") or draft.get("knowledge_kind")
    if load_policy:
        engine_set["load_policy"] = load_policy
        engine_set["default_load"] = load_policy == "always"
    # The draft's key order is the Wiki's convention whenever the planner followed an example.
    frontmatter: dict[str, Any] = {key: engine_set.get(key, value) for key, value in draft.items()}
    frontmatter.update({key: engine_set[key] for key in _PAGE_FIELD_ORDER if key in engine_set})
    if not frontmatter.get("description"):
        # The plan's summary, else the page's own title: the contract requires a description,
        # and a title is the one line about the page the draft has already stated.
        frontmatter["description"] = " ".join(str(summary or "").split())[:200] or page_title
    frontmatter.setdefault("status", "active")
    if not frontmatter.get("sources"):
        frontmatter["sources"] = [f"ai-flywheel session: {key}" for key in (source_sessions or [])[:5]] or [
            f"ai-flywheel batch {today}"
        ]
    fm_yaml = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False, width=1000).strip()
    return f"---\n{fm_yaml}\n---\n\n" + "\n".join(page_body).rstrip("\n") + "\n", []
