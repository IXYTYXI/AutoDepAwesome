import { createRouter, createWebHistory } from "vue-router";
import { reactive } from "vue";
import { auth } from "./services/auth.js";
import { repository } from "./services/content.js";

export const navigation = reactive({ pending: "", error: "" });
const definitions = [
  ["index", "home", "Home", "首页"],
  ["cases", "cases", "Cases", "案例库"],
  ["news", "news", "News", "AI 资讯"],
  ["skills", "skills", "Skills", "SkillHub"],
  ["deploy", "deploy", "Deploy", "一键部署"],
  ["services", "services", "Services", "常用服务"],
  ["profile", "profile", "Profile", "个人空间"],
  ["detail", "detail", "Detail", "详情"],
  ["officers", "officers", "Officers", "AI 领航团"],
];
const pages = import.meta.glob("./pages/*.vue");
export const router = createRouter({
  history: createWebHistory(),
  routes: definitions.map(([file, page, component, title]) => ({
    path: `/${file}.html`,
    ...(page === "home" ? { alias: "/" } : {}),
    name: page,
    component: pages[`./pages/${component}.vue`],
    meta: { page, title },
  })),
  scrollBehavior(to, from, saved) {
    if (to.path === from.path && to.name !== "detail" && !saved && to.hash === from.hash) return false;
    return new Promise((resolve) =>
      setTimeout(
        () => {
          if (saved) return resolve(saved);
          if (to.hash) {
            const target = document.getElementById(
              decodeURIComponent(to.hash.slice(1)),
            );
            if (target) return resolve({ el: target, top: 110 });
          }
          resolve({ top: 0 });
        },
        matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 160,
      ),
    );
  },
});
router.beforeEach(async (to, from) => {
  if (to.path === from.path && from.meta.content && ["cases", "news"].includes(to.name)) {
    to.meta.content = from.meta.content;
    return;
  }
  navigation.pending = to.path;
  navigation.error = "";
  try {
    // Every menu navigation still verifies the server session.
    await auth.refresh();
    to.meta.content = await repository.getContent({
      page: to.meta.page,
      search: new URL(to.fullPath, location.origin).search,
      });
  } catch (error) {
    navigation.error = error.message || "页面暂时无法加载，请重试。";
    navigation.pending = "";
    // Initial errors must render a retry state; later errors retain the current page.
    if (from.name) return false;
  }
});
router.afterEach((to, from, failure) => {
  navigation.pending = "";
  if (failure) return;
  document.body.dataset.page = to.meta.page;
  document.body.className =
    to.meta.page === "profile" ? "profile-page" : "portal-body";
  document.title = `${to.meta.title} — OWS Community`;
});
router.onError((error) => {
  navigation.pending = "";
  navigation.error = error.message || "页面加载失败，请刷新重试。";
});
