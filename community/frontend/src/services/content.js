/* Published content always comes from the authenticated backend. */
import { auth } from "./auth.js";

const key = () =>
  "ows-community-local-v1:" + (auth.user?.user_id || "anonymous");
function load() {
  let state = { cases: [], news: [], banner: null };
  try {
    const saved = JSON.parse(localStorage.getItem(key()));
    if (saved && Array.isArray(saved.cases) && Array.isArray(saved.news))
      state = saved;
  } catch {}
  return {
    ...state,
    cases: state.cases.filter((c) => c.local).map((c) => ({ ...c })),
    news: [],
    banner: null,
    faq: [],
  };
}
function save(state) {
  localStorage.setItem(key(), JSON.stringify(state));
  return state;
}
function adapt(item) {
  return {
    ...item,
    remote: true,
    category: item.tags[0] || item.product_type || "其他",
    origin: item.source,
    source: [item.author, item.team].filter(Boolean).join(" · "),
    body: item.summary,
  };
}
async function getContent({
  page = document.body.dataset.page,
  search = location.search,
} = {}) {
  const state = load();
  const params = new URLSearchParams(search);
  const needsCases =
    ["home", "cases", "skills"].includes(page) ||
    (page === "detail" && params.get("type") === "case");
  const needsBanner = page === "home" || (page === "detail" && params.get("type") === "event");
  const bannerRequest = needsBanner
    ? auth.request("/api/v1/banner").then(data => {
        state.banner = data.item;
        state.bannerStale = data.stale;
        state.bannerUpdatedAt = data.updated_at;
      }).catch(error => {
        state.bannerError = error.message || "Banner 暂时无法加载";
      })
    : Promise.resolve();
  const needsNews = ["home", "news"].includes(page) ||
    (page === "detail" && params.get("type") === "news");
  const newsRequest = needsNews
    ? auth.request("/api/v1/news").then(data => {
        state.news = data.items.map(item => ({ ...item, remote: true, body: item.summary }));
        state.newsStale = data.stale;
        state.newsUpdatedAt = data.updated_at;
      }).catch(error => {
        state.newsError = error.message || "资讯暂时无法加载";
      })
    : Promise.resolve();
  const faqRequest = page === "home" ? auth.request("/api/v1/faq").then(data => {
    state.faq = data.items;
  }).catch(error => { state.faqError = error.message || "常见问题暂时无法加载"; }) : Promise.resolve();
  const casesRequest = needsCases ? auth.request("/api/v1/cases").then(data => {
    state.cases = [...data.items.map(adapt), ...state.cases];
    state.casesStale = data.stale;
    state.casesUpdatedAt = data.updated_at;
  }).catch(error => {
    state.casesError = error.message || "案例暂时无法加载";
  }) : Promise.resolve();
  await Promise.all([bannerRequest, newsRequest, casesRequest, faqRequest]);
  return state;
}
export const repository = {
  getContent,
  createCase: async (item) => {
    const state = load();
    state.cases.unshift({ ...item, id: crypto.randomUUID(), local: true });
    save(state);
    return getContent();
  },
};
