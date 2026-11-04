<script setup>
import AmbientBackground from "../components/AmbientBackground.vue";
import { ref, onMounted } from "vue";
import { auth } from "../services/auth.js";
const params = new URLSearchParams(location.search),
  next = auth.safeNext(params.get("next") || "/index.html");
const href = "/api/v1/auth/feishu/start?" + new URLSearchParams({ next });
const status = ref("正在检查登录状态…"),
  failed = ref(false),
  dev = ref(false),
  feishu = ref(true),
  key = ref(""),
  saving = ref(false);
const errors = {
  OAUTH_STATE_INVALID: "登录请求已过期，请重新登录。",
  FEISHU_AUTH_FAILED: "授权未完成，请重新登录。",
  FORBIDDEN: "你暂无此应用的访问权限，请联系管理员。",
  FEISHU_NOT_CONFIGURED: "飞书登录尚未配置。",
  UPSTREAM_ERROR: "飞书登录暂不可用，请稍后重试。",
  SERVICE_UNAVAILABLE: "服务暂不可用，请稍后重试。",
};
onMounted(async () => {
  try {
    await auth.refresh();
    location.replace(next);
    return;
  } catch {}
  try {
    const r = await fetch("/api/v1/auth/options", { cache: "no-store" });
    if (!r.ok) throw Error();
    const { data } = await r.json();
    dev.value = data.dev_enabled;
    feishu.value = data.feishu_enabled;
    failed.value = params.has("error");
    status.value = failed.value
      ? errors[params.get("error")] || "登录未完成，请重试。"
      : params.has("logged_out")
        ? "已退出登录。"
        : "";
    if (!feishu.value) status.value = "飞书登录尚未配置。";
    if (
      feishu.value &&
      params.get("auto") === "1" &&
      !failed.value &&
      !params.has("logged_out")
    ) {
      status.value = "正在前往飞书…";
      location.replace(href);
    }
  } catch {
    status.value = "暂时无法连接服务，请刷新重试。";
    failed.value = true;
  }
});
async function submit() {
  saving.value = true;
  try {
    const r = await fetch("/api/v1/auth/dev", {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-Dev-Login-Key": key.value },
    });
    key.value = "";
    const b = await r.json();
    if (!r.ok) throw Error(b.message);
    location.replace(next);
  } catch (e) {
    status.value = e.message;
    failed.value = true;
  } finally {
    saving.value = false;
  }
}
</script>
<template>
  <AmbientBackground />
  <main class="login-panel">
    <div class="login-brand">
      <img
        src="/assets/ows-community-logo.png"
        alt="OWS Community"
        width="2172"
        height="724"
      />
    </div>
    <div class="login-rule"></div>
    <h1>登录社区</h1>
    <p class="login-caption">仅限公司成员访问</p>
    <UButton v-if="feishu" id="feishu-login" block size="xl" :to="href"
      >使用飞书登录 <TripleArrow compact
     /></UButton>
    <p
      id="login-status"
      role="status"
      aria-live="polite"
      :data-error="failed || undefined"
    >
      {{ status }}
    </p>
    <details v-if="dev" id="dev-login">
      <summary>本地开发登录</summary>
      <form id="dev-form" @submit.prevent="submit">
        <label for="dev-key">开发登录密钥</label
        ><UInput
          size="lg"
          id="dev-key"
          v-model="key"
          type="password"
          required
          autocomplete="off"
        /><UButton block size="lg" type="submit" :loading="saving"
          >进入开发环境</UButton
        >
      </form>
    </details>
  </main>
  <footer>OWS Community</footer>
</template>
