<script setup>
import PageBreadcrumb from "../components/PageBreadcrumb.vue";
import { useRoute } from "vue-router";
import { computed, watchEffect } from "vue";
import { safeUrl, imageUrl, newsDate } from "../services/urls.js";
import { defaultBanner } from "../services/defaults.js";
import NewsStatus from "../components/NewsStatus.vue";
import CaseDetail from "../components/CaseDetail.vue";
import CaseStatus from "../components/CaseStatus.vue";
const props = defineProps({ state: Object });
defineEmits(["retry"]);
const params = new URLSearchParams(
    useRoute().fullPath.split("?")[1]?.split("#")[0] || "",
  ),
  type = params.get("type"),
  id = params.get("id");
const item = computed(() =>
  type === "event"
    ? props.state.banner || defaultBanner
    : type === "case"
      ? props.state.cases.find((c) => c.id === id)
      : type === "news"
        ? props.state.news.find((n) => n.id === id)
        : null,
);
const back =
    type === "case"
      ? "cases.html"
      : type === "news"
        ? "news.html"
        : "index.html",
  label = type === "case" ? "案例库" : type === "news" ? "AI 资讯" : "首页";
const related = computed(() =>
  type === "case"
    ? props.state.cases.filter((c) => c.id !== id).slice(0, 2)
    : [],
);
watchEffect(
  () =>
    (document.title =
      (item.value?.title || (item.value ? "社区精选" : "内容未找到")) +
      " — OWS Community"),
);
</script>
<template>
  <main class="portal-page detail-page wrap" id="main-content">
    <div id="detail-content" aria-live="polite">
      <NewsStatus
        v-if="type === 'news'"
        :state="state"
        @retry="$emit('retry')"
      />
      <CaseStatus
        v-if="type === 'case'"
        :state="state"
        @retry="$emit('retry')"
      /><template
        v-if="
          item &&
          !(type === 'case' && state.casesError) &&
          !(type === 'news' && state.newsError)
        "
        ><PageBreadcrumb
          :parents="back !== 'index.html' ? [{ label, href: back }] : []"
          :current="type === 'event' ? '活动详情' : '内容详情'" />
        <CaseDetail v-if="type === 'case'" :item="item" :related="related" />
        <div v-else class="article-layout">
          <article class="article">
            <div class="article-meta">
              <span>{{
                type === "event"
                  ? item.kicker || "活动"
                  : item.category || "资讯"
              }}</span
              ><small>{{
                type === "event"
                  ? "活动预览"
                  : item.local
                    ? "本机发布"
                    : item.remote
                      ? type === "news"
                        ? newsDate(item)
                        : item.product_type
                      : "示例资讯"
              }}</small>
            </div>
            <h1 v-if="item.title">{{ item.title }}</h1>
            <div v-if="item.source" class="article-author">
              {{ item.source }}
            </div>
            <div v-if="imageUrl(item.image)" class="detail-banner">
              <img :src="imageUrl(item.image)" :alt="item.title" />
            </div>
            <div class="article-body">
              <p
                v-for="(text, index) in String(
                  item.body || item.summary || '',
                ).split(/\n\s*\n/)"
                :key="index"
              >
                {{ text }}
              </p>
            </div>
            <div
              v-if="type === 'event' && !safeUrl(item.url)"
              class="event-pending"
            >
              活动议程与参与链接待补充
            </div>
            <AppLink
              v-if="safeUrl(item.url)"
              class="primary article-original"
              :href="safeUrl(item.url)"
              target="_blank"
              rel="noopener noreferrer"
              >{{ type === "event" ? "打开活动链接" : "查看原始链接" }}
              <TripleArrow compact /></AppLink
            ><AppLink class="article-back" :href="back"
              ><TripleArrow compact direction="left" /> 返回{{ label }}</AppLink
            >
          </article>
          <aside class="article-aside">
            <h2>快捷入口</h2>
            <template
              ><AppLink href="cases.html"
                ><strong>案例库</strong
                ><span><TripleArrow compact /></span></AppLink
              ><AppLink
                href="https://onionhub.yc345.tv/"
                target="_blank"
                rel="noopener noreferrer"
                ><strong>SkillHub</strong
                ><span><TripleArrow compact /></span></AppLink></template
            ><p class="incentive-pending">AI激励政策正在积极规划中...</p>
          </aside></div
      ></template>
      <div
        v-else-if="!state.casesError && !state.newsError"
        class="detail-missing"
      >
        <span>404</span>
        <h1>内容未找到</h1>
        <p>内容可能已移除，或链接不完整。</p>
        <AppLink class="primary" :href="back">返回{{ label }}</AppLink>
      </div>
    </div>
  </main>
</template>

<style scoped>
.incentive-pending {
  margin-top: 24px;
  color: #78899c;
  font-size: 13px;
  line-height: 1.8;
}
</style>
