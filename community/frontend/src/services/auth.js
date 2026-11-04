import { shallowRef } from "vue";
export const user = shallowRef(null);
let csrf = "";
export function safeNext(value) {
  try {
    const u = new URL(value, location.origin);
    return u.origin === location.origin &&
      /^\/(index|cases|news|skills|services|profile|detail|officers|deploy)\.html$/.test(
        u.pathname,
      )
      ? u.pathname + u.search + u.hash
      : "/index.html";
  } catch {
    return "/index.html";
  }
}
function login() {
  location.replace(
    "/login.html?" +
      new URLSearchParams({
        next: safeNext(location.pathname + location.search + location.hash),
        auto: "1",
      }),
  );
}
async function refresh() {
  const r = await fetch("/api/v1/auth/session", {
    credentials: "same-origin",
    cache: "no-store",
  });
  const b = await r.json();
  if (!r.ok) {
    user.value = null;
    csrf = "";
    if (r.status === 401 && location.pathname != "/login.html") login();
    throw new Error(b.message || "登录状态检查失败");
  }
  user.value = b.data.user;
  csrf = b.data.csrf_token;
  return user.value;
}
async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  if (!["GET", "HEAD"].includes((options.method || "GET").toUpperCase()))
    headers.set("X-CSRF-Token", csrf);
  const r = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  const b = await r.json();
  if (r.status === 401) {
    user.value = null;
    csrf = "";
    login();
  }
  if (!r.ok) {
    const error = new Error(b.message || "请求失败");
    error.status = r.status;
    error.code = b.code;
    error.details = b.error?.details || [];
    throw error;
  }
  return b.data;
}
async function logout() {
  await request("/api/v1/auth/logout", { method: "POST" });
  user.value = null;
  csrf = "";
  location.replace("/login.html?logged_out=1");
}
export const auth = {
  refresh,
  request,
  logout,
  safeNext,
  get user() {
    return user.value;
  },
};
