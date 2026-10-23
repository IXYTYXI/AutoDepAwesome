"""Render an allowlisted Feishu Docx XML snapshot as static handbook HTML.

Usage: python scripts/render_feishu_protocol.py input.json output.html
The input is the JSON returned by `lark-cli docs +fetch --detail with-ids`.
"""

from __future__ import annotations

from html import escape
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


DOC_ID = "DSUwdxpXhot6fQx2YticNCtWnHe"
ALLOWED_LINK = re.compile(
    r"(?:https://guanghe\.feishu\.cn/docx/[A-Za-z0-9]+(?:#doxcn[A-Za-z0-9]+)?"
    r"|https://guanghe\.feishu\.cn/drive/folder/[A-Za-z0-9]+"
    r"|https://awesome\.guanghexinzhi\.cn/api-access)\Z"
)


def text(element: ET.Element) -> str:
    result = escape(element.text or "")
    for child in element:
        if child.tag == "br":
            result += "\n"
        elif child.tag == "p":
            result += text(child)
        elif child.tag == "a":
            href = child.get("href", "")
            if not ALLOWED_LINK.fullmatch(href):
                raise ValueError("unexpected document link")
            if href.startswith(f"https://guanghe.feishu.cn/docx/{DOC_ID}#"):
                fragment = href.split("#", 1)[1]
                result += f'<a href="#{escape(fragment, quote=True)}">'
            else:
                result += f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">'
            result += text(child) + "</a>"
        else:
            raise ValueError(f"unsupported inline element: {child.tag}")
        result += escape(child.tail or "")
    return result


def plain(element: ET.Element) -> str:
    return (element.text or "") + "".join(
        ("\n" if child.tag == "br" else plain(child)) + (child.tail or "")
        for child in element
    )


def render(element: ET.Element) -> str:
    tag = element.tag
    if tag == "title":
        return "<h1>" + text(element) + "</h1>"
    if tag in ("h1", "h2"):
        level = "h2" if tag == "h1" else "h3"
        block_id = element.get("id", "")
        if block_id and not re.fullmatch(r"doxcn[A-Za-z0-9]+", block_id):
            raise ValueError("invalid heading anchor")
        anchor = f' id="{block_id}"' if block_id else ""
        return f"<{level}{anchor}>{text(element)}</{level}>"
    if tag == "p":
        return "<p>" + text(element) + "</p>"
    if tag == "checkbox":
        if element.get("done") not in ("true", "false"):
            raise ValueError("invalid checkbox")
        mark = "☑" if element.get("done") == "true" else "☐"
        return '<p class="manual-check-item">' + mark + " " + text(element) + "</p>"
    if tag == "pre":
        if len(element) != 1 or element[0].tag != "code":
            raise ValueError("invalid code block")
        language = element.get("lang", "text")
        if not re.fullmatch(r"[a-z0-9-]{1,20}", language):
            raise ValueError("invalid code language")
        caption = element.get("caption", "").strip()
        label = f'<p class="manual-code-caption">{escape(caption)}</p>' if caption else ""
        return label + f'<pre><code class="language-{language}">{escape(plain(element[0]))}</code></pre>'
    if tag == "table":
        rows = []
        for part in element:
            if part.tag == "colgroup":
                continue
            if part.tag not in ("thead", "tbody"):
                raise ValueError("invalid table section")
            cells = []
            for row in part:
                if row.tag != "tr":
                    raise ValueError("invalid table row")
                items = []
                for cell in row:
                    if cell.tag not in ("th", "td"):
                        raise ValueError("invalid table cell")
                    items.append(f"<{cell.tag}>{text(cell)}</{cell.tag}>")
                cells.append("<tr>" + "".join(items) + "</tr>")
            rows.append(f"<{part.tag}>" + "".join(cells) + f"</{part.tag}>")
        return "<table>" + "".join(rows) + "</table>"
    raise ValueError(f"unsupported document block: {tag}")


def main() -> None:
    source = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    document = source["data"]["document"]
    if not source.get("ok") or document["document_id"] != DOC_ID:
        raise ValueError("wrong Feishu document")
    blocks = ET.fromstring("<root>" + document["content"] + "</root>")
    if len(blocks) == 0 or blocks[0].tag != "title":
        raise ValueError("missing document title")
    if len(blocks.findall("h1")) != 11:
        raise ValueError("incomplete technical protocol")
    output = "\n".join(render(block) for block in blocks) + "\n"
    Path(sys.argv[2]).write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
