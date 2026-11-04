<script setup>
import { computed, ref } from "vue";
import TripleArrow from "./TripleArrow.vue";
import NewsStatus from "./NewsStatus.vue";
import { imageUrl, newsDate } from "../services/urls.js";
import NewsDialog from "./NewsDialog.vue";
const reader = ref(null);
const props = defineProps({ state: Object });
defineEmits(["retry"]);
const items = computed(() =>
  [
    ...props.state.news.filter((item) => item.featured),
    ...props.state.news.filter((item) => !item.featured),
  ].slice(0, 7),
);
const lead = computed(() => items.value[0]);
const tones = {
  "AI 模型": "blue",
  "AI 产品": "cyan",
  行业动态: "amber",
  论文: "violet",
  教程: "green",
  观点: "rose",
};
</script>

<template>
  <section class="home-newsroom" aria-labelledby="home-news-title">
    <header class="news-heading">
      <div class="news-heading-title">
        <h2 id="home-news-title">AI 资讯<span>.</span></h2>
      </div>
      <AppLink href="news.html" class="news-more"
        >探索全部资讯 <TripleArrow
      /></AppLink>
    </header>
    <NewsStatus :state="state" @retry="$emit('retry')" />
    <div v-if="lead" class="news-edition">
      <button
        type="button"
        aria-haspopup="dialog"
        class="news-lead"
        :class="{ 'with-cover': imageUrl(lead.image) }"
        @click="reader.show(lead, $event)"
      >
        <img
          v-if="imageUrl(lead.image)"
          class="news-lead-cover"
          :src="imageUrl(lead.image)"
          alt=""
          loading="lazy"
        />
        <div v-else class="news-lead-art" aria-hidden="true">
          <i></i><i></i><i></i><i></i><i></i>
        </div>
        <div class="news-lead-top">
          <span>{{ lead.category }}</span
          ><span class="news-lead-label">{{
            lead.featured ? "特别推荐" : "精选阅读"
          }}</span>
        </div>
        <div class="news-lead-copy">
          <h3>{{ lead.title }}</h3>
          <p v-if="lead.summary">{{ lead.summary }}</p>
        </div>
        <div class="news-lead-bottom">
          <div>
            <span class="news-source">{{ lead.source }}</span
            ><time>{{ newsDate(lead) }}</time>
          </div>
          <span class="news-lead-open" aria-hidden="true"><TripleArrow /></span>
        </div>
      </button>
      <div v-if="items.length > 1" class="edition-briefs">
        <button
          type="button"
          aria-haspopup="dialog"
          v-for="(item, index) in items.slice(1)"
          :key="item.id"
          class="edition-brief"
          @click="reader.show(item, $event)"
        >
          <div class="edition-brief-top">
            <span
              class="news-topic"
              :data-tone="tones[item.category] || 'blue'"
              >{{ item.category }}</span
            >
            <span class="news-index" aria-hidden="true">{{
              String(index + 2).padStart(2, "0")
            }}</span>
          </div>
          <h3>{{ item.title }}</h3>
          <div class="edition-brief-bottom">
            <span class="news-source">{{ item.source }}</span
            ><time>{{ newsDate(item) }}</time
            ><TripleArrow />
          </div>
        </button>
      </div>
    </div>
    <div v-else-if="!state.newsError" class="news-empty">暂无资讯</div>
    <NewsDialog ref="reader" />
  </section>
</template>

<style scoped>
.news-lead,
.edition-brief {
  text-align: left;
  font-family: inherit;
  cursor: pointer;
  width: 100%;
}
.news-lead {
  border: 0;
}
.home-newsroom {
  margin: 38px 0 48px;
}
.news-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 20px;
}
.news-heading-title {
  display: flex;
  align-items: center;
  gap: 12px;
}
.news-heading-mark {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border: 1px solid #ffffffd9;
  border-radius: 12px;
  background: linear-gradient(145deg, #ffffff, #d9e9ff);
  color: #2a67d9;
  box-shadow: 0 3px 10px #456eac13;
}
.news-heading-mark .ui-icon {
  width: 19px;
  height: 19px;
}
.news-heading h2 {
  margin: 0;
  color: #172d49;
  font-size: clamp(27px, 2.6vw, 36px);
  font-weight: 650;
  letter-spacing: -1px;
  line-height: 1.15;
}
.news-heading h2 span {
  color: #2870ed;
}
.news-more {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: #355773;
  padding: 8px 0;
}
.news-more .ui-icon {
  width: 17px;
  height: 17px;
  transition: transform 0.2s;
}
.news-more:hover {
  color: #1e60d4;
}
.news-more:hover .ui-icon {
  transform: translate(2px, -2px);
}
.news-edition {
  display: grid;
  grid-template-columns: minmax(0, 0.94fr) minmax(0, 1.85fr);
  gap: 10px;
  padding: 9px;
  border: 1px solid #ffffffed;
  border-radius: 27px;
  background: #f8fbfff2;
  box-shadow:
    0 1px 2px #34577b0a,
    0 16px 40px -22px #31537e38,
    inset 0 0 0 1px #d7e3f34d;
}
.news-lead {
  position: relative;
  isolation: isolate;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 360px;
  padding: 26px;
  border-radius: 19px;
  overflow: hidden;
  color: white;
  background: linear-gradient(140deg, #153659 0%, #154c99 58%, #347ce8 100%);
  box-shadow: inset 0 0 0 1px #ffffff26;
}
.news-lead::after {
  content: "";
  position: absolute;
  inset: 0;
  z-index: -1;
  background: linear-gradient(165deg, transparent, #102c5366);
  pointer-events: none;
}
.news-lead-cover {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  z-index: -3;
  transition: transform 0.6s;
}
.news-lead.with-cover::after {
  background: linear-gradient(180deg, #071b3659, #071b36d9 55%, #071b36f2);
}
.news-lead-art {
  position: absolute;
  width: 290px;
  height: 290px;
  top: -95px;
  right: -140px;
  transform: rotate(-28deg);
  z-index: -2;
  pointer-events: none;
  transition: transform 0.7s;
}
.news-lead-art i {
  position: absolute;
  inset: 0;
  border: 1px solid #aee9ff55;
  border-radius: 55px;
  background: linear-gradient(135deg, #b7efff1a, transparent 70%);
}
.news-lead-art i:nth-child(2) {
  inset: 20px;
}
.news-lead-art i:nth-child(3) {
  inset: 40px;
}
.news-lead-art i:nth-child(4) {
  inset: 60px;
}
.news-lead-art i:nth-child(5) {
  inset: 80px;
}
.news-lead-top {
  display: flex;
  gap: 10px;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
}
.news-lead-top > span:first-child {
  background: #ffffff18;
  border: 1px solid #ffffff38;
  padding: 4px 10px;
  border-radius: 6px;
  color: #f4faff;
}
.news-lead-label {
  color: #c8def8;
  letter-spacing: 1px;
}
.news-lead-copy {
  margin: auto 0;
  padding: 26px 0 24px;
}
.news-lead h3 {
  margin: 0;
  font-size: clamp(22px, 1.8vw, 28px);
  font-weight: 620;
  line-height: 1.45;
  letter-spacing: -0.5px;
  color: #fff;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.news-lead p {
  margin: 15px 0 0;
  font-size: 12px;
  line-height: 1.9;
  color: #c7daf2;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.news-lead-bottom {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-top: 17px;
  border-top: 1px solid #ffffff24;
}
.news-lead-bottom > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11px;
  color: #dceaff;
}
.news-lead-bottom time {
  color: #a7c5ea;
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.news-source {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.news-lead-open {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 35px;
  height: 35px;
  border: 1px solid #ffffff66;
  border-radius: 50%;
  background: #ffffff10;
  transition:
    background 0.2s,
    transform 0.2s;
}
.news-lead-open .ui-icon {
  width: 17px;
  height: 17px;
}
.edition-briefs {
  min-width: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  padding: 4px 10px;
}
.edition-brief {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 10px;
  position: relative;
  padding: 17px 18px;
  border-bottom: 1px solid #e0e7ef;
  transition: background 0.2s;
}
.edition-brief:nth-child(odd) {
  border-right: 1px solid #e0e7ef;
}
.edition-brief:nth-last-child(-n + 2) {
  border-bottom: 0;
}
.edition-brief-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.news-topic {
  --topic: #366ab6;
  --wash: #e9f0fc;
  font-size: 10px;
  font-weight: 550;
  color: var(--topic);
  background: var(--wash);
  padding: 3px 7px;
  border-radius: 4px;
}
.news-topic[data-tone="cyan"] {
  --topic: #147489;
  --wash: #e4f3f8;
}
.news-topic[data-tone="amber"] {
  --topic: #926323;
  --wash: #f7efdf;
}
.news-topic[data-tone="violet"] {
  --topic: #70529b;
  --wash: #efebf7;
}
.news-topic[data-tone="green"] {
  --topic: #28705b;
  --wash: #e5f2ec;
}
.news-topic[data-tone="rose"] {
  --topic: #9a526c;
  --wash: #f6eaf0;
}
.news-index {
  color: #abb9ca;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  letter-spacing: 1px;
}
.edition-brief h3 {
  margin: 0;
  color: #253850;
  font-size: 15px;
  line-height: 1.6;
  font-weight: 580;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.edition-brief-bottom {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-top: auto;
  color: #75869a;
  font-size: 10px;
}
.edition-brief-bottom .news-source {
  flex: 1;
}
.edition-brief-bottom time {
  flex-shrink: 0;
  font-variant-numeric: tabular-nums;
}
.edition-brief-bottom .ui-icon {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
  color: #9aaec6;
  transition:
    transform 0.2s,
    color 0.2s;
}
.news-empty {
  padding: 36px;
  text-align: center;
  color: #73859b;
  border: 1px solid #fff;
  border-radius: 24px;
  background: #ffffffc9;
}
.news-lead:focus-visible,
.edition-brief:focus-visible,
.news-more:focus-visible {
  outline: 3px solid #3981e8;
  outline-offset: 3px;
  z-index: 1;
}
@media (hover: hover) {
  .news-lead:hover .news-lead-art {
    transform: rotate(-21deg) translate(-4px, 4px);
  }
  .news-lead:hover .news-lead-cover {
    transform: scale(1.04);
  }
  .news-lead:hover .news-lead-open {
    background: #ffffff30;
    transform: translate(2px, -2px);
  }
  .edition-brief:hover {
    background: #ebf2fc99;
  }
  .edition-brief:hover h3 {
    color: #1e60b9;
  }
  .edition-brief:hover .ui-icon {
    color: #2167c9;
    transform: translate(2px, -2px);
  }
}
@media (min-width: 1600px) {
  .edition-brief h3 {
    font-size: 17px;
  }
  .edition-brief {
    padding: 20px 24px;
  }
}
@media (max-width: 1100px) {
  .news-edition {
    grid-template-columns: 1fr;
  }
  .news-lead {
    min-height: 230px;
    padding: 24px 28px;
  }
  .news-lead-copy {
    max-width: 85%;
    padding: 18px 0;
  }
  .news-lead h3 {
    font-size: 24px;
    -webkit-line-clamp: 2;
  }
  .news-lead p {
    -webkit-line-clamp: 1;
    margin-top: 9px;
  }
  .news-lead-bottom > div {
    flex-direction: row;
    gap: 12px;
    align-items: center;
  }
  .news-lead-art {
    right: -55px;
    top: -70px;
  }
  .edition-brief {
    padding: 17px 18px;
    min-height: 126px;
  }
}
@media (max-width: 600px) {
  .home-newsroom {
    margin: 28px 0 36px;
  }
  .news-heading {
    margin-bottom: 16px;
  }
  .news-heading h2 {
    font-size: 27px;
  }
  .news-heading-mark {
    width: 31px;
    height: 31px;
    border-radius: 10px;
  }
  .news-more {
    font-size: 12px;
    gap: 6px;
  }
  .news-edition {
    border-radius: 22px;
    padding: 6px;
    gap: 4px;
  }
  .news-lead {
    padding: 20px;
    min-height: 250px;
    border-radius: 16px;
  }
  .news-lead-copy {
    max-width: 100%;
  }
  .news-lead h3 {
    font-size: 22px;
    -webkit-line-clamp: 3;
  }
  .edition-briefs {
    grid-template-columns: 1fr;
    padding: 0 10px;
  }
  .edition-brief {
    padding: 16px 5px;
    min-height: 112px;
    gap: 8px;
  }
  .edition-brief:nth-child(odd) {
    border-right: 0;
  }
  .edition-brief:nth-last-child(2) {
    border-bottom: 1px solid #e0e7ef;
  }
  .edition-brief:last-child {
    border-bottom: 0;
  }
  .edition-brief h3 {
    font-size: 15px;
  }
  .edition-brief-bottom {
    font-size: 11px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .home-newsroom * {
    transition: none !important;
  }
}

/* Soft blue-to-white surfaces, with space instead of hard rules between stories. */
.news-more {
  border-bottom: 1px solid #8eaed2;
  padding-bottom: 9px;
  gap: 7px;
}
.news-edition {
  gap: 12px;
  padding: 12px;
  background: linear-gradient(
    105deg,
    #cce3ffed 0%,
    #e9f3ffed 43%,
    #ffffffef 100%
  );
  border-color: #ffffffed;
  box-shadow:
    0 20px 45px -30px #2e58976b,
    inset 0 1px 0 #fff;
}
.news-lead {
  background: linear-gradient(115deg, #acd0ff, #e5f2ff);
  color: #1b477d;
  box-shadow:
    inset 0 0 0 1px #ffffff80,
    0 6px 20px #2b68a00a;
}
.news-lead::after {
  background: linear-gradient(120deg, #fff0, #ffffff40);
}
.news-lead h3 {
  color: #153b6c;
}
.news-lead p {
  color: #4a6d92;
}
.news-lead-art i {
  border-color: #3887de29;
  background: linear-gradient(135deg, #ffffff35, transparent 70%);
}
.news-lead-label {
  color: #4872a4;
}
.news-lead-top > span:first-child {
  color: #225d9f;
  background: #ffffff80;
  border-color: #ffffffa8;
}
.news-lead-bottom {
  border-color: #628db72b;
}
.news-lead-bottom > div {
  color: #476c94;
}
.news-lead-bottom time {
  color: #6382a4;
}
.news-lead-open {
  width: auto;
  height: auto;
  border: 0;
  background: none;
  border-radius: 0;
  color: #246ed2;
}
.news-lead.with-cover h3 {
  color: white;
}
.news-lead.with-cover p,
.news-lead.with-cover .news-lead-bottom > div,
.news-lead.with-cover .news-lead-bottom time,
.news-lead.with-cover .news-lead-label,
.news-lead.with-cover .news-lead-open {
  color: #e1efff;
}
.edition-briefs {
  gap: 8px;
  padding: 0;
}
.edition-brief {
  border: 0 !important;
  border-radius: 15px;
  padding: 18px 20px;
  background: linear-gradient(100deg, #f0f7ff80, #ffffffc9);
  box-shadow:
    inset 0 1px 0 #ffffffb3,
    0 2px 8px #31588603;
}
.edition-brief-bottom :deep(.triple-arrow) {
  width: 39px;
  color: #3977c5;
}
.edition-brief-bottom :deep(.triple-arrow .ui-icon) {
  width: 18px;
  height: 18px;
  margin-left: -6px;
  color: inherit;
}
.edition-brief-bottom :deep(.triple-arrow .ui-icon:first-child) {
  margin-left: 0;
}
@media (hover: hover) {
  .edition-brief:hover {
    background: linear-gradient(105deg, #dcecff, #fff);
    box-shadow:
      0 5px 15px #4a80bc10,
      inset 0 1px 0 white;
  }
  .news-lead:hover .news-lead-open {
    background: none;
  }
}
@media (max-width: 600px) {
  .news-edition {
    padding: 8px;
    gap: 8px;
  }
  .edition-briefs {
    padding: 0;
  }
  .edition-brief {
    padding: 16px;
  }
  .news-more {
    font-size: 11px;
    gap: 2px;
  }
}
</style>
