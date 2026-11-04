<script setup>
import PageHeading from "../components/PageHeading.vue";
import PageBreadcrumb from "../components/PageBreadcrumb.vue";
import { ref } from "vue";
import { useCatalog } from "../composables/useCatalog.js";
import FilterResults from "../components/FilterResults.vue";
import NewsStatus from "../components/NewsStatus.vue";
import NewsDialog from "../components/NewsDialog.vue";
import TripleArrow from "../components/TripleArrow.vue";
import Pagination from "../components/Pagination.vue";
import { imageUrl, newsDate } from "../services/urls.js";
const props = defineProps({ state: Object, user: Object });
const emit = defineEmits(["retry"]);
const { category, number, pageSize, categories, matches, visible } =
  useCatalog(props);
const reader = ref(null);
</script>
<template>
  <main class="portal-page wrap news-journal" id="main-content">
    <PageBreadcrumb current="AI 资讯" />
    <header class="journal-heading">
      <PageHeading>AI 资讯<span>.</span></PageHeading>
      <div class="journal-count">
        <span class="journal-live"></span>{{ matches.length }} 条资讯<span
          class="count-divider"
        ></span
        >持续更新
      </div>
    </header>
    <NewsStatus :state="state" @retry="emit('retry')" />
    <nav class="journal-categories" aria-label="资讯分类">
      <button
        v-for="value in categories"
        :key="value"
        type="button"
        :class="{ selected: category === value }"
        :aria-pressed="category === value"
        @click="
          category = value;
          number = 1;
        "
      >
        {{ value }}<span v-if="category === value" class="category-dot"></span>
      </button>
    </nav>
    <FilterResults :filter-key="`${category}:${number}`">
      <div id="news-results" class="journal-grid">
        <button
          v-for="(item, index) in visible"
          :key="item.id"
          type="button"
          class="journal-story"
          :class="{
            'journal-feature': index === 0,
            'has-image': imageUrl(item.image),
          }"
          aria-haspopup="dialog"
          @click="reader.show(item, $event)"
        >
          <div v-if="index === 0" class="journal-art" aria-hidden="true">
            <img
              v-if="imageUrl(item.image)"
              :src="imageUrl(item.image)"
              alt=""
            /><template v-else
              ><i></i><i></i><i></i><i></i><i></i><i></i
              ><span class="art-orbit"></span
            ></template>
          </div>
          <div class="journal-story-top">
            <span class="journal-topic">{{ item.category || "AI 动态" }}</span
            ><span class="journal-number">{{
              String((number - 1) * pageSize + index + 1).padStart(2, "0")
            }}</span>
          </div>
          <div class="journal-story-copy">
            <img
              v-if="index > 0 && imageUrl(item.image)"
              class="journal-thumbnail"
              :src="imageUrl(item.image)"
              alt=""
              loading="lazy"
            />
            <h2>{{ item.title }}</h2>
            <p v-if="item.summary">{{ item.summary }}</p>
          </div>
          <div class="journal-story-bottom">
            <span>{{ item.source }}</span
            ><time>{{ newsDate(item) }}</time
            ><TripleArrow />
          </div>
        </button>
        <div v-if="!matches.length && !state.newsError" class="catalog-empty">
          <h2>暂无相关资讯</h2>
        </div>
      </div>
    </FilterResults>
    <Pagination
      :count="matches.length"
      :page-size="pageSize"
      v-model="number"
    />
    <NewsDialog ref="reader" />
  </main>
</template>
<style scoped>
.news-journal {
  padding-bottom: 50px;
}
.journal-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin: 28px 0;
}
.journal-heading h1 > span {
  color: #2a72db;
}
.journal-count {
  display: flex;
  align-items: center;
  gap: 9px;
  color: #758da5;
  font-size: 12px;
}
.journal-live {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #5e94cc;
  box-shadow: 0 0 0 4px #77a6d51a;
}
.count-divider {
  height: 12px;
  width: 1px;
  background: #bbcede;
  margin: 0 3px;
}
.journal-categories {
  display: flex;
  gap: 7px;
  padding: 8px;
  border-radius: 18px;
  border: 1px solid #fff;
  background: #f8fbffcc;
  box-shadow: 0 6px 18px -15px #34577870;
  margin-bottom: 26px;
  overflow-x: auto;
}
.journal-categories button {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 10px 18px;
  border: 0;
  border-radius: 11px;
  background: transparent;
  color: #687e95;
  font-size: 13px;
  cursor: pointer;
  transition:
    background 0.2s,
    color 0.2s;
}
.journal-categories button.selected {
  color: #25599b;
  background: #e0ecfb;
  box-shadow: inset 0 1px 0 #fff;
  font-weight: 600;
}
.category-dot {
  width: 6px;
  height: 6px;
  flex-shrink: 0;
  box-shadow:
    0 0 0 4px #28b98216,
    0 0 9px #28b98255;
  border-radius: 50%;
  background: #20b881;
}
#news-results.journal-grid {
  min-width: 0;
  border: 0;
  padding: 0;
  border-radius: 0;
  background: none;
}
.journal-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}
.journal-story {
  display: flex;
  flex-direction: column;
  position: relative;
  isolation: isolate;
  text-align: left;
  font-family: inherit;
  min-width: 0;
  width: 100%;
  box-sizing: border-box;
  max-width: 100%;
  padding: 25px 28px 20px;
  border: 1px solid #fff;
  background: linear-gradient(120deg, #f4f9fff2, #ffffffed);
  border-radius: 24px;
  box-shadow:
    0 6px 25px -20px #31587d66,
    inset 0 1px 0 #fff;
  cursor: pointer;
  overflow: hidden;
  transition:
    transform 0.25s,
    box-shadow 0.25s,
    border-color 0.25s;
}
.journal-story-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 17px;
}
.journal-topic {
  color: #5682b2;
  font-size: 11px;
  font-weight: 550;
}
.journal-number {
  font-size: 11px;
  color: #a4b5c7;
  letter-spacing: 1px;
  font-variant-numeric: tabular-nums;
}
.journal-story-copy {
  min-width: 0;
  overflow-wrap: anywhere;
  flex: 1;
}
.journal-story h2 {
  margin: 0;
  font-size: 20px;
  color: #24405e;
  line-height: 1.55;
  letter-spacing: -0.3px;
  font-weight: 620;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.journal-story p {
  font-size: 13px;
  color: #7a8da1;
  line-height: 1.85;
  margin: 13px 0 22px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.journal-story-bottom {
  display: flex;
  align-items: center;
  gap: 14px;
  padding-top: 14px;
  border-top: 1px solid #dce7f1;
  color: #8b9db0;
  font-size: 11px;
}
.journal-story-bottom > span {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  max-width: 50%;
}
.journal-story-bottom time {
  flex-shrink: 0;
}
.journal-story-bottom :deep(.triple-arrow) {
  margin-left: auto;
  color: #5688bd;
}
.journal-thumbnail {
  width: 86px;
  height: 86px;
  float: right;
  object-fit: cover;
  margin: 0 0 14px 18px;
  border-radius: 12px;
}
.journal-feature {
  grid-column: 1/-1;
  min-height: 290px;
  padding: 30px 36px 24px;
  color: #fff;
  border-color: #ffffff85;
  background: linear-gradient(115deg, #142b48, #254c7b 68%, #3d719f);
  box-shadow: 0 16px 36px -24px #183c7799;
}
.journal-feature .journal-story-copy {
  min-width: 0;
  overflow-wrap: anywhere;
  width: 76%;
  max-width: 800px;
}
.journal-feature h2 {
  font-size: clamp(24px, 2.6vw, 32px);
  color: #f4f8ff;
  line-height: 1.5;
  -webkit-line-clamp: 2;
}
.journal-feature p {
  color: #bbcee3;
  max-width: 700px;
}
.journal-feature .journal-topic {
  color: #d5e7fa;
  padding: 5px 10px;
  border: 1px solid #ffffff30;
  border-radius: 6px;
  background: #ffffff0d;
}
.journal-feature .journal-number {
  color: #b6d1ed;
}
.journal-feature .journal-story-bottom {
  border-color: #ffffff26;
  color: #a6c4e3;
}
.journal-feature .journal-story-bottom :deep(.triple-arrow) {
  color: #d0e6ff;
}
.journal-art {
  position: absolute;
  inset: 0;
  overflow: hidden;
  z-index: -1;
  pointer-events: none;
}
.journal-art i {
  position: absolute;
  width: 300px;
  height: 300px;
  border: 1px solid #b2dcff30;
  border-radius: 50%;
  right: -10px;
  top: -100px;
  background: linear-gradient(160deg, #b5d8ff10, transparent 75%);
  transform: rotate(-28deg);
}
.journal-art i:nth-child(2) {
  width: 350px;
  height: 350px;
  right: -40px;
  top: -80px;
}
.journal-art i:nth-child(3) {
  width: 400px;
  height: 400px;
  right: -70px;
  top: -60px;
}
.journal-art i:nth-child(4) {
  width: 450px;
  height: 450px;
  right: -100px;
  top: -40px;
}
.journal-art i:nth-child(5) {
  width: 500px;
  height: 500px;
  right: -130px;
  top: -20px;
}
.journal-art i:nth-child(6) {
  width: 550px;
  height: 550px;
  right: -160px;
  top: 0;
}
.art-orbit {
  position: absolute;
  right: 105px;
  top: 75px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #d3ecff;
  box-shadow: 0 0 20px #91d3ff;
}
.journal-art img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  opacity: 0.35;
  mask-image: linear-gradient(90deg, transparent, #000);
}
.catalog-empty {
  grid-column: 1/-1;
}
.journal-story:focus-visible,
.journal-categories button:focus-visible {
  outline: 3px solid #4c8cda;
  outline-offset: 3px;
}
@media (hover: hover) {
  .journal-story:hover {
    transform: translateY(-3px);
    box-shadow: 0 17px 36px -23px #31587d77;
    border-color: #c3d9ee;
  }
  .journal-categories button:hover {
    background: #e8f0fa;
    color: #2b6099;
  }
}
@media (max-width: 760px) {
  .journal-grid {
    gap: 14px;
  }
  .journal-story {
    padding: 22px;
  }
  .journal-feature .journal-story-copy {
    min-width: 0;
    overflow-wrap: anywhere;
    width: 90%;
  }
  .journal-story h2 {
    font-size: 18px;
  }
  .journal-feature h2 {
    font-size: 25px;
  }
  .journal-thumbnail {
    width: 65px;
    height: 65px;
    margin-left: 10px;
  }
}
@media (max-width: 600px) {
  .journal-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 13px;
    margin: 24px 0;
  }
  .journal-categories {
    border-radius: 15px;
    gap: 3px;
    margin-bottom: 20px;
  }
  .journal-categories button {
    padding: 9px 14px;
    font-size: 12px;
  }
  .journal-grid {
    grid-template-columns: 1fr;
  }
  .journal-feature {
    min-height: 300px;
  }
  .journal-feature .journal-story-copy {
    min-width: 0;
    overflow-wrap: anywhere;
    width: 100%;
  }
  .journal-art {
    opacity: 0.45;
  }
  .journal-story {
    border-radius: 21px;
  }
  .journal-story-bottom {
    gap: 10px;
  }
}
@media (prefers-reduced-motion: reduce) {
  * {
    transition: none !important;
  }
}
</style>
