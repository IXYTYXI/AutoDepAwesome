<script setup>
import { ref } from "vue";
import guide from "../data/reimbursement.json";
import TripleArrow from "./TripleArrow.vue";
import TextLinks from "./TextLinks.vue";
const open = ref(false);
let opener;
function show(event) {
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
    title="AI 工具报销"
    description="报销规则与常见问题"
    :content="{ onCloseAutoFocus: restoreFocus }"
    :ui="{
      content: 'news-reader expense-reader',
      overlay: 'news-reader-overlay',
    }"
  >
    <template #content>
      <article class="expense-sheet">
        <header class="expense-header">
          <div>
            <span class="expense-eyebrow"
              ><Icon name="receipt-text" />常用服务</span
            >
            <h2>AI 工具报销<span>.</span></h2>
          </div>
          <button
            type="button"
            class="expense-close"
            aria-label="关闭报销指引"
            @click="open = false"
          >
            <Icon name="x" />
          </button>
        </header>
        <div class="expense-scroll">
          <details class="expense-overview" open>
            <summary>
              <span class="overview-mark" aria-hidden="true"
                ><Icon name="notebook-pen"
              /></span>
              <span class="overview-caption"
                ><strong>一页版结论</strong
                ><span>{{ guide.overview.length }} 项报销要点</span></span
              >
              <span class="overview-arrow"
                ><TripleArrow compact direction="down"
              /></span>
            </summary>
            <dl>
              <div v-for="row in guide.overview" :key="row.question">
                <dt>{{ row.question }}</dt>
                <dd>
                  <TextLinks :text="row.answer" link-label="打开飞书链接" />
                </dd>
              </div>
            </dl>
          </details>
          <h3 class="expense-faq-title">
            常见问题<span>{{ guide.items.length }}</span>
          </h3>
          <div class="expense-faq">
            <details
              v-for="(item, index) in guide.items"
              :key="item.question"
              class="expense-question"
            >
              <summary>
                <span class="expense-number">{{
                  String(index + 1).padStart(2, "0")
                }}</span>
                <h3>{{ item.question }}</h3>
                <Icon name="plus" />
              </summary>
              <p>
                <TextLinks
                  :text="item.answer"
                  :link-label="
                    item.answer.includes('shrcnqFF2DE85xr8rOj67xbFCpg')
                      ? '补交表单'
                      : '打开飞书链接'
                  "
                />
              </p>
            </details>
          </div>
          <p class="expense-notice">{{ guide.notice }}</p>
        </div>
        <footer class="expense-footer">
          <a
            :href="guide.source"
            target="_blank"
            rel="noopener noreferrer"
            class="expense-document"
            >查看完整说明 <TripleArrow compact /></a
          ><a
            :href="guide.form"
            target="_blank"
            rel="noopener noreferrer"
            class="expense-submit"
            ><img src="/assets/feishu.svg" alt="" />去提交 AI 报销 <TripleArrow
          /></a>
        </footer>
      </article>
    </template>
  </UModal>
</template>
<style scoped>
.expense-sheet {
  display: flex;
  flex-direction: column;
  min-height: 0;
  max-height: inherit;
  color: #25415e;
}
.expense-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 28px 32px 22px;
  background: linear-gradient(120deg, #e8f1fd, #f9fbfe 80%);
  border-bottom: 1px solid #dce7f2;
  flex-shrink: 0;
}
.expense-eyebrow {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #6f88a4;
}
.expense-eyebrow .ui-icon {
  width: 16px;
  height: 16px;
}
.expense-header h2 {
  font-size: 30px;
  letter-spacing: -0.8px;
  font-weight: 650;
  margin: 10px 0 0;
}
.expense-header h2 span {
  color: #3178ce;
}
.expense-close {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 1px solid #d8e3ef;
  border-radius: 50%;
  background: white;
  color: #617b98;
  cursor: pointer;
  transition: transform 0.2s;
}
.expense-close:hover {
  transform: rotate(90deg);
}
.expense-scroll {
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 24px 32px;
}
.expense-overview {
  position: relative;
  background: linear-gradient(120deg, #e7f0fc, #f7faff 72%);
  border: 1px solid #cddff2;
  border-radius: 20px;
  overflow: hidden;
  box-shadow:
    0 8px 22px -18px #315d9380,
    inset 0 1px 0 #fff;
  transition:
    border-color 0.22s,
    box-shadow 0.22s;
}
.expense-overview:hover {
  border-color: #9fbee1;
  box-shadow:
    0 12px 26px -18px #315d9390,
    inset 0 1px 0 #fff;
}
.expense-overview summary {
  position: relative;
  display: flex;
  align-items: center;
  gap: 15px;
  padding: 21px;
  list-style: none;
  cursor: pointer;
  background: radial-gradient(ellipse at 95% 0%, #bad6f63b, transparent 60%);
}
.overview-mark {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 46px;
  height: 50px;
  border: 1px solid #fff;
  border-radius: 13px;
  color: #386da5;
  background: linear-gradient(145deg, #fff, #dceafb);
  box-shadow: 0 5px 12px -6px #5079a65c;
  transform: rotate(-5deg);
  transition: transform 0.22s;
}
.overview-mark .ui-icon {
  width: 24px;
  height: 24px;
}
.expense-overview summary:hover .overview-mark {
  transform: rotate(0);
}
.overview-caption {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}
.overview-caption strong {
  font-size: 18px;
  font-weight: 600;
  color: #294c73;
}
.overview-caption > span {
  font-size: 12px;
  font-weight: 400;
  color: #7b92ad;
  letter-spacing: 0.5px;
}
.overview-arrow {
  margin-left: auto;
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  color: #6182a7;
  transition: color 0.22s;
}
.overview-arrow :deep(.triple-arrow) {
  width: 28px;
  height: 28px;
}
.expense-overview summary:hover .overview-arrow {
  color: #2e639b;
}
.expense-overview[open] .overview-arrow :deep(.triple-arrow) {
  --arrow-rotation: -90deg;
}
@media (max-width: 420px) {
  .expense-overview summary {
    gap: 11px;
    padding: 16px;
  }
  .overview-mark {
    width: 36px;
    height: 42px;
  }
  .overview-caption strong {
    font-size: 17px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .expense-overview,
  .overview-mark,
  .overview-arrow,
  .overview-arrow :deep(.triple-arrow) {
    transition: none;
  }
}
.expense-overview dl {
  padding: 0 20px 12px;
  margin: 0;
}
.expense-overview dl > div {
  display: grid;
  grid-template-columns: 130px minmax(0, 1fr);
  gap: 18px;
  padding: 13px 0;
  border-top: 1px solid #d4e3f2;
  line-height: 1.8;
  font-size: 14px;
}
.expense-overview dt {
  color: #71859c;
}
.expense-overview dd {
  margin: 0;
  color: #294969;
  overflow-wrap: anywhere;
}
.expense-faq-title {
  display: flex;
  gap: 12px;
  align-items: center;
  font-size: 21px;
  margin: 30px 0 12px;
  font-weight: 600;
}
.expense-faq-title > span {
  font-size: 12px;
  color: #8b9eb5;
  font-weight: 400;
}
.expense-faq {
  background: #fff;
  border: 1px solid #e1e9f3;
  border-radius: 18px;
  padding: 0 20px;
}
.expense-question {
  border-bottom: 1px solid #e4edf6;
}
.expense-question:last-child {
  border: 0;
}
.expense-question summary {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 21px 0;
  cursor: pointer;
  list-style: none;
}
summary::-webkit-details-marker {
  display: none;
}
.expense-number {
  color: #88a4c4;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.expense-question h3 {
  flex: 1;
  font-size: 17px;
  line-height: 1.6;
  font-weight: 550;
  margin: 0;
}
.expense-question .ui-icon {
  width: 17px;
  height: 17px;
  flex-shrink: 0;
  transition: transform 0.2s;
}
.expense-question[open] summary > .ui-icon {
  transform: rotate(45deg);
}
.expense-question p {
  margin: 0 0 24px 28px;
  white-space: pre-wrap;
  font-size: 15px;
  line-height: 1.9;
  color: #596f88;
  overflow-wrap: anywhere;
  animation: answer-in 0.22s ease;
}
.expense-scroll a {
  color: #296fbb;
  text-decoration: underline;
  text-underline-offset: 3px;
}
.expense-scroll a .ui-icon {
  width: 13px;
  height: 13px;
  display: inline;
}
.expense-notice {
  color: #7c8c9f;
  font-size: 12px;
  line-height: 1.8;
  margin: 22px 2px 0;
}
.expense-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 20px 32px;
  flex-shrink: 0;
  border-top: 1px solid #dce7f2;
  background: #edf3fb;
}
.expense-document {
  display: flex;
  gap: 6px;
  align-items: center;
  color: #718aa5;
  font-size: 12px;
}
.expense-document .ui-icon {
  width: 14px;
  height: 14px;
}
.expense-submit {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 13px 18px;
  border-radius: 12px;
  color: white;
  background: linear-gradient(120deg, #244c80, #336ca8);
  font-size: 14px;
  box-shadow:
    0 8px 18px -9px #244c8099,
    inset 0 1px 0 #fff3;
  transition: transform 0.2s;
}
.expense-submit:hover {
  transform: translateY(-2px);
}
.expense-submit img {
  width: 22px;
  height: 22px;
  background: white;
  border-radius: 6px;
  padding: 3px;
}
.expense-sheet a:focus-visible,
.expense-sheet button:focus-visible,
summary:focus-visible {
  outline: 3px solid #6499dd;
  outline-offset: 3px;
}
@keyframes answer-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
  to {
    opacity: 1;
    transform: none;
  }
}
@media (max-width: 600px) {
  .expense-header,
  .expense-scroll {
    padding: 20px;
  }
  .expense-header h2 {
    font-size: 26px;
  }
  .expense-footer {
    padding: 16px 20px;
    flex-wrap: wrap;
  }
  .expense-submit {
    justify-content: center;
    flex: 1;
  }
  .expense-overview dl > div {
    grid-template-columns: 1fr;
    gap: 3px;
  }
  .expense-question h3 {
    font-size: 16px;
  }
  .expense-document {
    font-size: 11px;
  }
  .expense-question p {
    margin-left: 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .expense-question p {
    animation: none;
  }
  .expense-submit,
  .expense-close,
  .ui-icon {
    transition: none;
  }
}
</style>
