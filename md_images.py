"""Locate local Markdown/HTML images without rewriting unrelated content."""

from __future__ import annotations

from dataclasses import dataclass
import html
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class ImageRef:
    start: int
    end: int
    source: str
    # A reference-style image needs its whole syntax rewritten; normal images
    # only replace the URL so alt text, title and HTML attributes are kept.
    reference_alt: str | None = None
    reference_title: str = ''

    def replacement(self, url: str) -> str:
        if self.reference_alt is not None:
            return f"![{self.reference_alt}]({url}{self.reference_title})"
        return html.escape(url, quote=True)


def _masked_markdown(text: str) -> str:
    """Keep offsets while hiding fenced/inline code and HTML comments."""
    chars = list(text)

    def mask(start: int, end: int) -> None:
        for index in range(start, end):
            if chars[index] not in "\r\n":
                chars[index] = " "

    fence_char = ""
    fence_length = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        match = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence_char:
            mask(offset, offset + len(line))
            if (match and match.group(1)[0] == fence_char
                    and len(match.group(1)) >= fence_length
                    and not match.group(2).strip()):
                fence_char = ""
        elif match:
            fence_char, fence_length = match.group(1)[0], len(match.group(1))
            mask(offset, offset + len(line))
        offset += len(line)

    masked = "".join(chars)
    for match in re.finditer(r"<!--[\s\S]*?(?:-->|\Z)", masked):
        mask(*match.span())

    # An inline code span is delimited by equal-length backtick runs. Unmatched
    # runs are left alone, as they are ordinary Markdown text.
    masked = "".join(chars)
    index = 0
    while index < len(masked):
        if masked[index] != "`":
            index += 1
            continue
        run_end = index + 1
        while run_end < len(masked) and masked[run_end] == "`":
            run_end += 1
        width = run_end - index
        closing = re.search(r"(?<!`)`{" + str(width) + r"}(?!`)", masked[run_end:])
        if closing:
            end = run_end + closing.end()
            mask(index, end)
            index = end
        else:
            index = run_end
    return "".join(chars)


def _label(value: str) -> str:
    return " ".join(value.split()).casefold()


def _definitions(text: str, visible: str) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    pattern = re.compile(
        r'''(?m)^ {0,3}\[([^\]\r\n]+)\]:[ \t]*(?:<([^>\r\n]+)>|([^\s]+))'''
        r'''(?P<title>[ \t]+(?:"[^"\r\n]*"|'[^'\r\n]*'|\([^()\r\n]*\)))?'''
    )
    for match in pattern.finditer(visible):
        start, end = match.span(2) if match.group(2) is not None else match.span(3)
        title = text[slice(*match.span('title'))] if match.group('title') else ''
        result.setdefault(_label(match.group(1)), (text[start:end], title))
    return result


def _find_bracket_end(text: str, start: int) -> int:
    depth = 1
    pos = start
    while pos < len(text):
        char = text[pos]
        if char == "\\" and pos + 1 < len(text) and text[pos + 1] in "[]\\":
            pos += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return pos
        if char in "\r\n":
            return -1
        pos += 1
    return -1


def _inline_destination(text: str, start: int) -> tuple[int, int, int] | None:
    """Return the URL span and position after the closing parenthesis."""
    pos = start
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    if pos >= len(text):
        return None
    if text[pos] == "<":
        end = text.find(">", pos + 1)
        if end < 0:
            return None
        closing = text.find(")", end + 1)
        if closing < 0 or "\n" in text[end + 1:closing]:
            return None
        return pos + 1, end, closing + 1

    dest_start = pos
    depth = 0
    while pos < len(text):
        char = text[pos]
        if char in "\r\n":
            return None
        if char == "\\" and pos + 1 < len(text) and text[pos + 1] in "()":
            pos += 2
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                raw_end = pos
                raw = text[dest_start:raw_end]
                # Typora permits literal spaces in paths. Only remove a
                # trailing quoted Markdown title, not an internal space.
                title = re.search(r'''[ \t]+(?:"[^"\r\n]*"|'[^'\r\n]*')[ \t]*$''', raw)
                if title:
                    raw_end = dest_start + title.start()
                while raw_end > dest_start and text[raw_end - 1] in " \t":
                    raw_end -= 1
                return dest_start, raw_end, pos + 1
            depth -= 1
        pos += 1
    return None


def find_images(text: str) -> list[ImageRef]:
    visible = _masked_markdown(text)
    definitions = _definitions(text, visible)
    refs: list[ImageRef] = []
    pos = 0
    while pos < len(visible):
        start = visible.find("![", pos)
        if start < 0:
            break
        if start and visible[start - 1] == "\\":
            pos = start + 2
            continue
        end_alt = _find_bracket_end(visible, start + 2)
        if end_alt < 0:
            pos = start + 2
            continue
        alt = text[start + 2:end_alt]
        after = end_alt + 1
        if after < len(visible) and visible[after] == "(":
            parsed = _inline_destination(visible, after + 1)
            if parsed:
                src_start, src_end, after_image = parsed
                if src_start < src_end:
                    refs.append(ImageRef(src_start, src_end, text[src_start:src_end]))
                pos = after_image
                continue
        elif after < len(visible) and visible[after] == "[":
            end_label = _find_bracket_end(visible, after + 1)
            if end_label >= 0:
                name = visible[after + 1:end_label] or alt
                definition = definitions.get(_label(name))
                if definition:
                    refs.append(ImageRef(start, end_label + 1, definition[0], alt, definition[1]))
                pos = end_label + 1
                continue
        elif _label(alt) in definitions:
            src, title = definitions[_label(alt)]
            refs.append(ImageRef(start, end_alt + 1, src, alt, title))
        pos = end_alt + 1

    # Inline HTML images are common when Typora specifies width/height.
    html_pattern = re.compile(
        r'''<img\b[^>]*?\bsrc\s*=\s*(?:(?P<q>["'])(?P<quoted>.*?)(?P=q)|(?P<bare>[^\s>]+))''',
        re.IGNORECASE | re.DOTALL,
    )
    for match in html_pattern.finditer(visible):
        group = "quoted" if match.group("q") else "bare"
        src_start, src_end = match.span(group)
        refs.append(ImageRef(src_start, src_end, text[src_start:src_end]))
    return sorted(refs, key=lambda ref: (ref.start, ref.end))


def local_path(source: str, md_path: Path) -> Path | None:
    """Return a resolved local path, or None for remote/embedded resources."""
    value = html.unescape(source.strip())
    if not value or value.startswith("#"):
        return None
    windows_drive = bool(re.match(r"^[a-zA-Z]:[\\/]", value))
    if value.lower().startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            value = f"//{parsed.netloc}{parsed.path}"
        else:
            value = parsed.path
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", value):
                value = value[1:]
    elif value.startswith("//") or (not windows_drive and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value)):
        return None
    value = unquote(value)
    # In Windows paths, a backslash before a parenthesized directory is a
    # separator, not a Markdown escape (e.g. C:\\(notes)\\image.png).
    if os.name != 'nt' or not ('\\' in value and ('/' not in value or windows_drive)):
        value = re.sub(r"\\([()])", r"\1", value)
    path = Path(value)
    if not path.is_absolute():
        path = md_path.parent / path
    return path.resolve(strict=False)


def rewrite(text: str, replacements: list[tuple[ImageRef, str]]) -> str:
    last = 0
    parts: list[str] = []
    for ref, url in sorted(replacements, key=lambda pair: pair[0].start):
        if ref.start < last:
            raise ValueError("图片标记重叠，无法安全替换")
        parts.extend((text[last:ref.start], ref.replacement(url)))
        last = ref.end
    parts.append(text[last:])
    return "".join(parts)
