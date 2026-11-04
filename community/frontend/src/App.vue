<script setup>
import AmbientBackground from "./components/AmbientBackground.vue";
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { useRoute } from "vue-router";
import { router, navigation } from "./router.js";
import { auth, user } from "./services/auth.js";
import { repository } from "./services/content.js";
import SiteShell from "./components/SiteShell.vue";
import ContentEditor from "./components/ContentEditor.vue";
const route = useRoute();
const activePage = computed(() =>
  route.meta.page === "detail"
    ? route.query.type === "case"
      ? "cases"
      : route.query.type === "event"
        ? "home"
        : "news"
    : route.meta.page,
);
const state = ref({ cases: [], news: [], banner: null }),
  pageRoot = ref(null),
  editor = ref(null),
  toast = ref("");
let timer;
watch(
  () => route.meta.content,
  (content) => {
    if (content) state.value = content;
  },
  { immediate: true },
);
async function retry() {
  state.value = await repository.getContent({
    page: route.meta.page,
    search: new URL(route.fullPath, location.origin).search,
  });
  route.meta.content = state.value;
}
function saved(value) {
  state.value = value;
  toast.value = "已保存到本机浏览器。";
  clearTimeout(timer);
  timer = setTimeout(() => (toast.value = ""), 4000);
}
function focusPage() {
  const main = pageRoot.value?.querySelector("main");
  if (main) {
    main.setAttribute("tabindex", "-1");
    main.focus({ preventScroll: true });
  }
}
const visibility = () => {
  if (!document.hidden)
    auth.refresh().catch((e) => (navigation.error = e.message));
};
const restored = (e) => {
  if (e.persisted) location.reload();
};
onMounted(() => {
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("pageshow", restored);
});
onUnmounted(() => {
  clearTimeout(timer);
  document.removeEventListener("visibilitychange", visibility);
  window.removeEventListener("pageshow", restored);
});
</script>
<template>
  <UApp>
    <AmbientBackground />
    <template v-if="user && route.meta.content">
      <SiteShell :page="activePage" :user="user">
        <div v-if="navigation.error" class="navigation-error wrap" role="alert">
          {{ navigation.error }}
          <button @click="navigation.error = ''">关闭</button>
        </div>
        <RouterView v-slot="{ Component, route: viewRoute }">
          <Transition name="page-switch" mode="out-in" @after-enter="focusPage">
            <div
              :key="
                viewRoute.path +
                (viewRoute.name === 'detail' ? viewRoute.fullPath : '')
              "
              ref="pageRoot"
              class="route-page"
            >
              <component
                :is="Component"
                :state="state"
                :user="user"
                @compose="editor.open($event)"
                @retry="retry"
              />
            </div>
          </Transition>
        </RouterView>
      </SiteShell>
      <ContentEditor ref="editor" :state="state" @saved="saved" />
      <div v-if="toast" class="toast" role="status">{{ toast }}</div>
    </template>
    <div v-else class="auth-failure">
      <p>{{ navigation.error || "正在加载…" }}</p>
      <button v-if="navigation.error" @click="router.go(0)">重新加载</button>
    </div>
  </UApp>
</template>
