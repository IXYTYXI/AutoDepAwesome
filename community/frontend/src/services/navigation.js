// Keep navigation within Vue Router while preserving native external links.
export function internalDestination(href, currentPath, origin) {
  if (!href) return null;
  try {
    const url = new URL(href, new URL(currentPath, origin));
    return url.origin === origin ? url.pathname + url.search + url.hash : null;
  } catch {
    return null;
  }
}
