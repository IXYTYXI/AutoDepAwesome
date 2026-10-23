"""Render a reviewed Feishu document fetch into a safe, bundled read-only page.

Input is JSON from `lark-cli docs +fetch --as user --detail with-ids --json`.
The output is served only by the authenticated /document/content endpoint.
"""

import argparse
import html
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree


TAGS = {
    "title": "h1", "h1": "h2", "h2": "h3", "h3": "h4", "p": "p",
    "b": "strong", "i": "em", "u": "u", "code": "code", "pre": "pre",
    "table": "table", "thead": "thead", "tbody": "tbody", "tr": "tr",
    "th": "th", "td": "td", "colgroup": "colgroup", "col": "col",
    "br": "br", "checkbox": "span", "callout": "aside", "a": "a",
    "ul": "ul", "ol": "ol", "li": "li",
}
HEADING_ID = re.compile(r"doxcn[A-Za-z0-9]{1,128}\Z")


def heading_id(node: ElementTree.Element) -> str | None:
    value = node.attrib.get("id", "")
    return value if HEADING_ID.fullmatch(value) else None


def safe_href(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    return html.escape(value, quote=True)


def code_text(node: ElementTree.Element) -> str:
    return (node.text or "") + "".join(
        ("\n" if child.tag == "br" else code_text(child)) + (child.tail or "") for child in node)


def render(node: ElementTree.Element) -> str:
    if node.tag == "pre":
        caption = node.attrib.get("caption", "").strip()
        label = f'<p class="manual-code-caption">{html.escape(caption)}</p>' if caption else ""
        return label + '<pre tabindex="0" aria-label="代码示例"><code translate="no">' + html.escape(code_text(node)) + '</code></pre>' + html.escape(node.tail or "")
    tag = TAGS.get(node.tag)
    if tag is None:
        # Preserve text in unsupported blocks, without inheriting their markup.
        return html.escape("".join(node.itertext())) + html.escape(node.tail or "")
    attrs = ""
    if node.tag == "a":
        href = safe_href(node.attrib.get("href", ""))
        if href is None:
            return html.escape("".join(node.itertext())) + html.escape(node.tail or "")
        attrs = f' href="{href}" target="_blank" rel="noopener noreferrer"'
    elif node.tag == "callout":
        attrs = ' class="manual-callout"'
    elif node.tag == "checkbox":
        attrs = ' class="manual-checkbox"'
    elif node.tag in {"title", "h1", "h2", "h3"}:
        anchor = heading_id(node)
        if anchor:
            attrs = f' id="{anchor}" tabindex="-1"'
    elif node.tag == "code":
        attrs = ' translate="no"'
    if tag in {"br", "col"}:
        body = f"<{tag}{attrs}>"
    else:
        content = html.escape(node.text or "") + "".join(render(child) for child in node)
        if node.tag == "checkbox":
            content = ("☑ " if node.attrib.get("checked") == "true" else "□ ") + content
        body = f"<{tag}{attrs}>{content}</{tag}>"
    return body + html.escape(node.tail or "")


def render_document(raw: str, *, toc: bool = False) -> str:
    root = ElementTree.fromstring("<root>" + raw + "</root>")
    # Feishu block IDs can safely serve as stable links; other attributes remain ignored.
    anchors = set()
    for node in root.iter():
        if node.tag not in {"title", "h1", "h2", "h3"}:
            continue
        anchor = heading_id(node)
        if anchor in anchors:
            node.attrib.pop("id", None)
        elif anchor:
            anchors.add(anchor)
    navigation = ""
    if toc:
        sections = [(heading_id(node), "".join(node.itertext()).strip())
                    for node in root if node.tag == "h1" and heading_id(node)]
        if sections:
            items = "".join(f'<li><a href="#{anchor}">{html.escape(label)}</a></li>'
                            for anchor, label in sections)
            navigation = '<nav class="manual-toc" aria-label="本文目录"><p><strong>本文目录</strong></p><ol>' + items + '</ol></nav>'
    blocks = [render(child) for child in root]
    if navigation:
        blocks.insert(1 if len(root) and root[0].tag == "title" else 0, navigation)
    return "\n".join(blocks)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--toc", action="store_true", help="Add a linked table of contents for top-level sections")
    args = parser.parse_args()
    data = json.loads(args.source.read_text())
    if data.get("ok") is not True:
        raise SystemExit("Feishu fetch did not succeed")
    content = data["data"]["document"]["content"]
    args.target.write_text(render_document(content, toc=args.toc) + "\n")


if __name__ == "__main__":
    main()
