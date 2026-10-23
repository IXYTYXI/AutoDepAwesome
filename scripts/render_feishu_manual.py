"""Render a reviewed Feishu document fetch into a safe, bundled read-only page.

Input is JSON from `lark-cli docs +fetch --as user --detail with-ids --json`.
The output is served only by the authenticated /document/content endpoint.
"""

import argparse
import html
import json
from pathlib import Path
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
        return '<pre tabindex="0" aria-label="代码示例"><code>' + html.escape(code_text(node)) + '</code></pre>' + html.escape(node.tail or "")
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
    if tag in {"br", "col"}:
        body = f"<{tag}{attrs}>"
    else:
        content = html.escape(node.text or "") + "".join(render(child) for child in node)
        if node.tag == "checkbox":
            content = ("☑ " if node.attrib.get("checked") == "true" else "□ ") + content
        body = f"<{tag}{attrs}>{content}</{tag}>"
    return body + html.escape(node.tail or "")


def render_document(raw: str) -> str:
    root = ElementTree.fromstring("<root>" + raw + "</root>")
    return "\n".join(render(child) for child in root)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    data = json.loads(args.source.read_text())
    if data.get("ok") is not True:
        raise SystemExit("Feishu fetch did not succeed")
    content = data["data"]["document"]["content"]
    args.target.write_text(render_document(content) + "\n")


if __name__ == "__main__":
    main()
