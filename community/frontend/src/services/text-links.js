// Plain text only: Chinese punctuation and prose must never become part of a URL.
export function textLinks(text = "") {
  const parts = [];
  const pattern =
    /https?:\/\/[^\s<>"'\u3000-\u303f\uff00-\uffef\u3400-\u9fff]+/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    const url = match[0].replace(/[.,;!?)]+$/, "");
    if (!url) continue;
    let valid = false;
    try {
      valid = Boolean(new URL(url).hostname);
    } catch {
      /* Leave malformed URLs as text. */
    }
    if (!valid) continue;
    if (match.index > cursor)
      parts.push({ text: text.slice(cursor, match.index) });
    parts.push({ text: url, href: url });
    cursor = match.index + url.length;
  }
  if (cursor < text.length) parts.push({ text: text.slice(cursor) });
  return parts;
}
