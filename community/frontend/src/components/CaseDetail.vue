<script setup>
import { computed } from "vue";
import CaseCover from "./CaseCover.vue";
import TripleArrow from "./TripleArrow.vue";
import { detailUrl, safeUrl } from "../services/urls.js";
const props = defineProps({
  item: { type: Object, required: true },
  related: { type: Array, default: () => [] },
});
const facts = computed(() =>
  [
    ["所属团队", props.item.team],
    ["案例作者", props.item.author],
    [
      "团队 AI 官",
      props.item.ai_officers?.map((person) => person.name).join("、"),
    ],
    ["产物类型", props.item.product_type],
    ["案例来源", props.item.origin],
    ["访问权限", props.item.access],
  ].filter(([, value]) => value),
);
</script>
<template>
  <div class="case-story-layout">
    <article class="case-story">
      <div class="story-cover">
        <CaseCover :item="item" variant="detail" />
        <div class="story-cover-shade" aria-hidden="true"></div>
        <div class="story-cover-label">
          <span></span>{{ item.team || "社区实践" }}
        </div>
        <span v-if="item.product_type" class="story-format">{{
          item.product_type
        }}</span>
      </div>
      <div class="story-content">
        <div v-if="item.tags?.length" class="story-tags">
          <span v-for="tag in item.tags" :key="tag">{{ tag }}</span>
        </div>
        <h1>{{ item.title }}</h1>
        <p v-if="item.summary" class="story-summary">{{ item.summary }}</p>
        <dl class="story-facts">
          <div v-for="[label, value] in facts" :key="label">
            <dt>{{ label }}</dt>
            <dd>{{ value }}</dd>
          </div>
        </dl>
        <div class="story-actions">
          <AppLink
            v-if="safeUrl(item.url)"
            :href="safeUrl(item.url)"
            target="_blank"
            rel="noopener noreferrer"
            class="story-visit"
            >前往飞书了解案例 <TripleArrow
          /></AppLink>
          <AppLink
            v-if="safeUrl(item.contact_url)"
            :href="safeUrl(item.contact_url)"
            target="_blank"
            rel="noopener noreferrer"
            class="story-contact"
            ><span class="feishu-mark"
              ><img src="/assets/feishu.svg" alt="" /></span
            >联系作者 <TripleArrow compact
           /></AppLink>
          <AppLink
            v-for="person in item.ai_officers || []"
            :key="person.chat_url"
            :href="safeUrl(person.chat_url)"
            target="_blank"
            rel="noopener noreferrer"
            class="story-contact story-officer"
            :aria-label="`联系 AI 官 ${person.name}（打开飞书聊天）`"
          >
            <span class="feishu-mark"
              ><img src="/assets/feishu.svg" alt=""
            /></span>
            联系 AI 官<span v-if="item.ai_officers.length > 1"
              >· {{ person.name }}</span
            ><TripleArrow compact  />
          </AppLink>
        </div>
      </div>
    </article>
    <aside class="story-related" aria-labelledby="related-case-title">
      <header>
        <h2 id="related-case-title">继续探索<span>.</span></h2>
        <span>相关案例</span>
      </header>
      <AppLink
        v-for="(entry, index) in related"
        :key="entry.id"
        :href="detailUrl('case', entry.id)"
        class="related-story"
      >
        <div class="related-image">
          <CaseCover :item="entry" /><span class="related-number"
            >0{{ index + 1 }}</span
          >
        </div>
        <div class="related-content">
          <span class="related-team">{{ entry.team || entry.category }}</span>
          <h3>{{ entry.title }}</h3>
          <p v-if="entry.summary">{{ entry.summary }}</p>
          <footer>
            <span>{{ entry.author }}</span
            ><TripleArrow />
          </footer>
        </div>
      </AppLink>
      <AppLink href="cases.html" class="story-all"
        >探索全部案例 <TripleArrow
      /></AppLink>
    </aside>
  </div>
</template>
<style scoped>
.case-story-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 30px;
  align-items: start;
  padding-bottom: 48px;
}
.case-story {
  min-width: 0;
  overflow: hidden;
  border: 1px solid #fff;
  border-radius: 30px;
  background: #f9fbfef5;
  box-shadow:
    0 18px 52px -30px #34557860,
    0 2px 5px #3455780a;
}
.story-cover {
  position: relative;
  overflow: hidden;
  background: #10223c;
}
.story-cover :deep(.case-media) {
  aspect-ratio: 2.05;
  max-height: 420px;
}
.story-cover :deep(img) {
  height: 100%;
  object-fit: cover;
}
.story-cover-shade {
  position: absolute;
  inset: 0;
  background: linear-gradient(180deg, #07172c38, transparent 55%, #07172c66);
  pointer-events: none;
}
.story-cover-label {
  position: absolute;
  top: 24px;
  left: 28px;
  display: flex;
  gap: 9px;
  align-items: center;
  font-size: 12px;
  color: #fff;
  text-shadow: 0 1px 4px #0009;
}
.story-cover-label span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #dbedff;
  box-shadow: 0 0 0 4px #fff2;
}
.story-format {
  position: absolute;
  bottom: 22px;
  left: 28px;
  font-size: 11px;
  color: white;
  border: 1px solid #fff5;
  background: #142b4466;
  backdrop-filter: blur(10px);
  border-radius: 7px;
  padding: 5px 10px;
}
.story-content {
  padding: 32px 36px 36px;
}
.story-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-bottom: 14px;
}
.story-tags span {
  font-size: 11px;
  color: #476c99;
  background: #e8eff8;
  padding: 4px 9px;
  border-radius: 5px;
}
.story-content h1 {
  margin: 0;
  font-size: clamp(28px, 3vw, 40px);
  color: #162d48;
  line-height: 1.35;
  letter-spacing: -1px;
  font-weight: 650;
  overflow-wrap: anywhere;
}
.story-summary {
  margin: 22px 0 28px;
  border-left: 3px solid #5488d6;
  padding: 1px 0 1px 18px;
  font-size: 17px;
  line-height: 1.9;
  color: #49607a;
  white-space: pre-line;
  overflow-wrap: anywhere;
}
.story-facts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 22px 16px;
  padding: 24px 0;
  margin: 24px 0 0;
  border-top: 1px solid #dce5ee;
}
.story-facts dt {
  font-size: 11px;
  color: #8090a3;
  margin-bottom: 7px;
}
.story-facts dd {
  margin: 0;
  color: #354d68;
  font-size: 13px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
.story-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding-top: 8px;
}
.story-visit,
.story-contact {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 14px;
  min-height: 50px;
  border-radius: 14px;
  padding: 0 20px;
  font-size: 13px;
  font-weight: 550;
  transition:
    transform 0.22s,
    box-shadow 0.22s,
    background 0.22s;
}
.story-visit {
  color: white;
  background: linear-gradient(120deg, #143f80, #2268d3);
  border: 1px solid #3071ce;
  box-shadow:
    inset 0 1px 0 #ffffff35,
    0 6px 18px #215cbc20;
}
.story-visit :deep(.triple-arrow) {
  color: #e0eeff;
}
.story-contact {
  color: #284967;
  border: 1px solid #d4e1ef;
  background: linear-gradient(120deg, #fff, #eef5fc);
  padding-left: 9px;
  gap: 10px;
}
.feishu-mark {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  background: white;
  border-radius: 9px;
  box-shadow: 0 2px 6px #3d7da00d;
}
.feishu-mark img {
  width: 24px;
  height: 24px;
}
.story-contact .ui-icon {
  width: 15px;
  height: 15px;
  margin-left: 6px;
  color: #6f8ba6;
}
.story-related {
  min-width: 0;
}
.story-related header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin: 3px 0 18px;
}
.story-related h2 {
  margin: 0;
  color: #233e5b;
  font-size: 22px;
  font-weight: 650;
  letter-spacing: -0.5px;
}
.story-related h2 span {
  color: #2c73d8;
}
.story-related header > span {
  color: #7a8da3;
  font-size: 11px;
}
.related-story {
  display: block;
  border-radius: 21px;
  border: 1px solid #ffffffed;
  background: #f9fcfff0;
  overflow: hidden;
  margin-bottom: 18px;
  box-shadow: 0 8px 24px -18px #28497077;
  transition:
    transform 0.25s,
    box-shadow 0.25s;
}
.related-image {
  position: relative;
  overflow: hidden;
}
.related-image :deep(.case-media) {
  aspect-ratio: 1.9;
}
.related-image :deep(img) {
  object-fit: cover;
  transition: transform 0.5s;
}
.related-number {
  position: absolute;
  top: 12px;
  left: 14px;
  color: #fff;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  padding: 3px 7px;
  border: 1px solid #fff5;
  background: #112d4c66;
  border-radius: 5px;
}
.related-content {
  padding: 17px 19px 12px;
}
.related-team {
  font-size: 10px;
  color: #6a839d;
}
.related-content h3 {
  font-size: 18px;
  color: #233e5b;
  margin: 7px 0;
  line-height: 1.5;
  font-weight: 620;
}
.related-content p {
  margin: 8px 0 16px;
  color: #6b7f95;
  font-size: 12px;
  line-height: 1.8;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.related-content footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-top: 1px solid #e1e8ef;
  padding: 10px 0 0;
  color: #73879b;
  font-size: 11px;
}
.related-content :deep(.triple-arrow) {
  color: #477cb7;
}
.story-all {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 20px 6px;
  padding-bottom: 10px;
  border-bottom: 1px solid #9fb5cc;
  font-size: 12px;
  color: #4b7299;
}
.story-visit:focus-visible,
.story-contact:focus-visible,
.related-story:focus-visible,
.story-all:focus-visible {
  outline: 3px solid #4086e3;
  outline-offset: 4px;
}
@media (hover: hover) {
  .story-visit:hover,
  .story-contact:hover {
    transform: translateY(-2px);
    box-shadow: 0 9px 22px #215cbc25;
  }
  .related-story:hover {
    transform: translateY(-3px);
    box-shadow: 0 14px 28px -18px #28497088;
  }
  .related-story:hover img {
    transform: scale(1.04);
  }
}
@media (max-width: 1000px) {
  .case-story-layout {
    grid-template-columns: minmax(0, 1fr) 250px;
    gap: 20px;
  }
  .story-content {
    padding: 26px;
  }
  .story-actions {
    gap: 10px;
  }
  .story-visit,
  .story-contact {
    font-size: 12px;
  }
}
@media (max-width: 760px) {
  .case-story-layout {
    grid-template-columns: 1fr;
    gap: 36px;
  }
  .story-related {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 14px;
  }
  .story-related header,
  .story-all {
    grid-column: 1/-1;
    margin: 0;
  }
  .related-story {
    margin: 0;
  }
  .related-content {
    padding: 14px;
  }
  .related-content h3 {
    font-size: 16px;
  }
  .story-all {
    margin-top: 6px;
  }
}
@media (max-width: 480px) {
  .case-story {
    border-radius: 23px;
  }
  .story-cover :deep(.case-media) {
    aspect-ratio: 1.6;
  }
  .story-content {
    padding: 24px 20px;
  }
  .story-summary {
    font-size: 15px;
    margin: 20px 0;
  }
  .story-facts {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .story-visit {
    flex: 1 1 100%;
  }
  .story-contact {
    flex: 1;
  }
  .story-cover-label {
    top: 18px;
    left: 20px;
  }
  .story-format {
    left: 20px;
    bottom: 16px;
  }
  .story-related {
    gap: 10px;
  }
  .related-content p {
    display: none;
  }
  .related-content h3 {
    font-size: 15px;
  }
}
@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    transition: none !important;
  }
}
</style>
