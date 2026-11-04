<script setup>
import { ref } from "vue";
import { safeUrl, imageUrl, newsDate } from "../services/urls.js";
import TripleArrow from "./TripleArrow.vue";
const open = ref(false);
const item = ref(null);
let opener;
function show(entry, event) {
  item.value = entry;
  opener = event?.currentTarget;
  open.value = true;
}
function restoreFocus(event) {
  if (opener?.isConnected) {
    event.preventDefault();
    opener.focus({ preventScroll: true });
  }
  opener = null;
}
defineExpose({ show });
</script>
<template>
  <UModal
    v-model:open="open"
    :title="item?.title || 'AI 资讯'"
    :content="{ onCloseAutoFocus: restoreFocus }"
    :ui="{ content: 'news-reader', overlay: 'news-reader-overlay' }"
  >
    <template #content>
      <article v-if="item" class="reader-sheet">
        <header class="reader-topline">
          <span class="reader-topic"
            ><i></i>{{ item.category || "AI 资讯" }}</span
          ><button
            type="button"
            class="reader-close"
            aria-label="关闭资讯"
            @click="open = false"
          >
            <Icon name="x" />
          </button>
        </header>
        <div class="reader-scroll">
          <div class="reader-byline">
            <span>{{ item.source }}</span
            ><span v-if="item.source && newsDate(item)">/</span
            ><time>{{ newsDate(item) }}</time>
          </div>
          <h2>{{ item.title }}</h2>
          <img
            v-if="imageUrl(item.image)"
            :src="imageUrl(item.image)"
            class="reader-image"
            alt=""
          />
          <div class="reader-summary">
            <span class="reader-rule" aria-hidden="true"></span>
            <p>{{ item.summary || "暂无摘要" }}</p>
          </div>
        </div>
        <footer v-if="safeUrl(item.url)" class="reader-footer">
          <AppLink
            v-if="safeUrl(item.url)"
            :href="safeUrl(item.url)"
            target="_blank"
            rel="noopener noreferrer"
            class="reader-source"
            >阅读原文 <TripleArrow
          /></AppLink>
        </footer>
      </article>
    </template>
  </UModal>
</template>

<style scoped>
.reader-sheet {
  display: flex;
  flex-direction: column;
  min-height: 0;
  max-height: inherit;
  color: #233d59;
}
.reader-topline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 22px 28px 8px;
  flex-shrink: 0;
}
.reader-topic {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  color: #4574a7;
  font-size: 12px;
  font-weight: 550;
}
.reader-topic i {
  width: 6px;
  height: 6px;
  background: #427edd;
  border-radius: 50%;
  box-shadow: 0 0 0 4px #427edd12;
}
.reader-close {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 1px solid #dce6f0;
  border-radius: 50%;
  background: white;
  color: #607a94;
  cursor: pointer;
  transition:
    background 0.2s,
    transform 0.2s;
}
.reader-close:hover {
  background: #e8f0fb;
  transform: rotate(90deg);
}
.reader-close .ui-icon {
  width: 17px;
  height: 17px;
}
.reader-scroll {
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 22px 38px 32px;
}
.reader-byline {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
  color: #8091a4;
}
.reader-scroll h2 {
  margin: 15px 0 25px;
  font-size: clamp(24px, 3.2vw, 32px);
  font-weight: 650;
  line-height: 1.5;
  color: #1b3655;
  letter-spacing: -0.7px;
  overflow-wrap: anywhere;
}
.reader-image {
  width: 100%;
  max-height: 260px;
  object-fit: cover;
  border-radius: 15px;
  margin-bottom: 24px;
}
.reader-rule {
  display: block;
  width: 40px;
  height: 3px;
  background: linear-gradient(90deg, #3a79d0, #a2cff0);
  border-radius: 2px;
  margin-bottom: 20px;
}
.reader-summary p {
  margin: 0;
  white-space: pre-line;
  font-size: 16px;
  line-height: 2;
  color: #4b617a;
  overflow-wrap: anywhere;
}
.reader-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 15px;
  padding: 20px 38px 24px;
  background: linear-gradient(105deg, #eaf2fc, #f5f8fc);
  border-top: 1px solid #e0e8f2;
  flex-shrink: 0;
}
.reader-source {
  display: flex;
  align-items: center;
  gap: 10px;
  border-radius: 12px;
  background: #224f8c;
  color: white;
  padding: 12px 16px;
  font-size: 12px;
  box-shadow:
    inset 0 1px 0 #fff3,
    0 4px 10px #295e991c;
}
.reader-close:focus-visible,
.reader-source:focus-visible {
  outline: 3px solid #5d99e1;
  outline-offset: 3px;
}
@media (max-width: 600px) {
  .reader-topline {
    padding: 18px 20px 6px;
  }
  .reader-scroll {
    padding: 18px 24px 28px;
  }
  .reader-footer {
    padding: 18px 24px;
  }
  .reader-summary p {
    font-size: 15px;
  }
}
</style>
