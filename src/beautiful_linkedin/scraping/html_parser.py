from __future__ import annotations

import re

from bs4 import BeautifulSoup


def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()

    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    visible_lines = [line for line in lines if line]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(visible_lines)).strip()
