export const safeUrl = (value) => {
  try {
    const u = new URL(value);
    return ["http:", "https:"].includes(u.protocol) ? u.href : "";
  } catch {
    return "";
  }
};
export const imageUrl = (value) =>
  /^data:image\/(png|jpeg|webp);base64,/.test(value || "")
    ? value
    : /^\/api\/v1\/(banner|officers|cases|news)\/media\/[a-f0-9]{64}$/.test(value || "")
      ? value
      : safeUrl(value);
export const detailUrl = (type, id = "") =>
  `detail.html?type=${encodeURIComponent(type)}${id ? "&id=" + encodeURIComponent(id) : ""}`;

// Published cases only load media through the authenticated backend.
export const caseImageUrl = (item) => item?.remote
  ? (/^\/api\/v1\/cases\/media\/[a-f0-9]{64}$/.test(item.image || '') ? item.image : '')
  : imageUrl(item?.image);

export const newsDate = (item) => {
  const value = item.published_at || item.discovered_at;
  return value ? new Date(value).toLocaleDateString("zh-CN") : "";
};
