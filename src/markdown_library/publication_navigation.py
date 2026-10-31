"""Local publication navigation, excluded from every public Post projection.

The managed region owns one newline immediately before its start marker. This
separator lets set/strip round-trip bodies with or without a final newline
without trimming any of their original whitespace. Markers are reserved whole
lines; ambiguous, repeated or fenced markers fail closed.
"""

import re


START = "<!-- markdown-library:publication-nav:start -->"
END = "<!-- markdown-library:publication-nav:end -->"
_NAMESPACE = "markdown-library:publication-nav:"
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _span(markdown: str) -> tuple[int, int] | None:
    if not isinstance(markdown, str):
        raise TypeError("publication navigation requires Markdown text")
    start = end = None
    offset = 0
    fence = None
    for line in markdown.splitlines(keepends=True):
        text = line.removesuffix("\n").removesuffix("\r")
        if _NAMESPACE in text:
            if text not in (START, END) or fence is not None:
                raise ValueError("invalid publication navigation marker")
            if text == START:
                if start is not None or end is not None:
                    raise ValueError("repeated publication navigation marker")
                start = offset
            else:
                if start is None or end is not None:
                    raise ValueError("unpaired publication navigation marker")
                end = offset + len(line)
        match = _FENCE.match(text)
        if match:
            run, tail = match.groups()
            if fence is None:
                if run[0] != "`" or "`" not in tail:
                    fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not tail.strip():
                fence = None
        offset += len(line)
    if start is None:
        return None
    if end is None:
        raise ValueError("unclosed publication navigation marker")
    # The immediately preceding newline belongs to the managed separator.
    if start and markdown[start - 1] == "\n":
        start -= 1
        if start and markdown[start - 1] == "\r":
            start -= 1
    return start, end


def strip_publication_navigation(markdown: str) -> str:
    """Remove only the reserved navigation region and its owned separator."""
    span = _span(markdown)
    return markdown if span is None else markdown[:span[0]] + markdown[span[1]:]


def set_publication_navigation(markdown: str, footer_body: str | None) -> str:
    """Append/replace the managed region, or remove it for an empty body.

    All bytes outside the managed region are retained. The body may not contain
    reserved markers, and neither source nor footer may hide markers in a code
    fence. Running this again with the same arguments is a no-op.
    """
    span = _span(markdown)
    if footer_body is not None and not isinstance(footer_body, str):
        raise TypeError("publication navigation body must be text or None")
    if not footer_body:
        return strip_publication_navigation(markdown)
    if _NAMESPACE in footer_body:
        raise ValueError("publication navigation body contains reserved markers")
    ending = "\r\n" if "\r\n" in markdown else "\n"
    body = footer_body
    if not body.endswith("\n"):
        body += ending
    block = ending + START + ending + body + END + ending
    if span is None:
        result = markdown + block
    else:
        result = markdown[:span[0]] + block + markdown[span[1]:]
    _span(result)  # Reject markers swallowed by an unclosed source/footer fence.
    return result
