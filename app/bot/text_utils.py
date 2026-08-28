"""Helpers for building Telegram HTML messages safely.

Every dynamic value (channel names, AI titles, Notion text, exception text)
must go through esc() before being interpolated into a parse_mode=HTML
message — otherwise a single '<' or '&' in the value makes Telegram reject
the whole message with "can't parse entities".
"""

import html
from html.parser import HTMLParser


def esc(value) -> str:
    """Escape arbitrary text for interpolation into a parse_mode=HTML message."""
    return html.escape(str(value), quote=False)


class _HtmlToPlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_data(self, data):
        if data:
            self.parts.append(data)


def html_to_plain_text(value: str) -> str:
    """Collapse Telegram-flavoured HTML (message.html_text) to its visible text.

    Used for previews and button labels where formatting tags would either
    show up literally or, worse, get cut in half by truncation. Tag-free text
    passes through unchanged apart from entity decoding (&amp; -> &).
    """
    if not value:
        return ""
    parser = _HtmlToPlainText()
    parser.feed(value)
    parser.close()
    return "".join(parser.parts)


def truncate(value: str, limit: int) -> str:
    """Length-cap plain text. Never call this on HTML — cutting at a fixed
    offset can split a tag or entity; convert with html_to_plain_text first."""
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"
