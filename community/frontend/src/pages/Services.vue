<script setup>
import { ref } from "vue";
import PageHeading from "../components/PageHeading.vue";
import PageBreadcrumb from "../components/PageBreadcrumb.vue";
import ReimbursementDialog from "../components/ReimbursementDialog.vue";
import TextLinks from "../components/TextLinks.vue";
import toolChannels from "../data/tool-channels.json";
const reimbursement = ref(null);
const expandedChannel = ref(null);
function toggleChannel(id) {
  expandedChannel.value = expandedChannel.value === id ? null : id;
}
</script>

<template>
  <main class="portal-page wrap" id="main-content">
    <PageBreadcrumb current="常用服务" />
    <div class="page-title"><PageHeading>常用服务</PageHeading></div>
    <section class="service-group" aria-labelledby="tools-heading">
      <div class="service-group-heading">
        <h2 id="tools-heading"><Icon name="receipt-text" />工具报销</h2>
        <span class="group-divider" aria-hidden="true"></span>
        <span class="group-caption">每人每月仅可选择一个渠道</span>
      </div>
      <div class="services-grid tools-grid">
        <article
          v-for="channel in toolChannels.channels"
          :key="channel.id"
          :id="channel.id"
          class="service-card tool-card"
          :class="`service-card--${channel.id}`"
        >
          <div class="service-card-top">
            <span class="service-label"
              ><b>{{ channel.number }}</b
              >工具支持</span
            >
            <span class="service-art" aria-hidden="true"
              ><span class="art-orbit"></span
              ><span class="art-tile"><Icon :name="channel.icon" /></span
              ><span class="art-spark"><Icon name="sparkles" /></span
            ></span>
          </div>
          <div class="tool-title">
            <h3>{{ channel.title }}</h3>
            <span v-if="channel.recommended" class="recommend-tag"
              ><Icon name="thumbs-up" />推荐</span
            >
          </div>
          <p class="tool-description">{{ channel.description }}</p>
          <div
            class="channel-allowance"
            :class="{ 'channel-allowance--plan': channel.id === 'official' }"
          >
            <strong>{{ channel.amount }}</strong
            ><span>{{ channel.unit }}</span>
          </div>
          <button
            class="channel-toggle"
            type="button"
            :aria-expanded="expandedChannel === channel.id"
            :aria-controls="`channel-rules-${channel.id}`"
            :aria-label="`${channel.title}渠道规则`"
            @click="toggleChannel(channel.id)"
          >
            <span>渠道规则</span>
            <TripleArrow
              compact
              :direction="expandedChannel === channel.id ? 'up' : 'down'"
            />
          </button>
          <div
            :id="`channel-rules-${channel.id}`"
            class="channel-disclosure"
            :class="{ 'is-open': expandedChannel === channel.id }"
            :inert="expandedChannel !== channel.id"
            :aria-hidden="expandedChannel !== channel.id"
          >
            <div class="channel-disclosure-inner">
              <dl class="channel-rules">
                <div v-for="row in channel.rows" :key="row.label">
                  <dt>{{ row.label }}</dt>
                  <dd><TextLinks :text="row.text" link-label="注册入口" /></dd>
                </div>
              </dl>
            </div>
          </div>
          <div class="service-card-footer">
            <button
              v-if="channel.id === 'expense'"
              type="button"
              class="service-action"
              aria-haspopup="dialog"
              @click="reimbursement.show($event)"
            >
              {{ channel.action }}<TripleArrow compact />
            </button>
            <a
              v-else
              :href="channel.href"
              target="_blank"
              rel="noopener noreferrer"
              class="service-action"
              >{{ channel.action }}<TripleArrow compact
            /></a>
          </div>
        </article>
      </div>
    </section>
    <section class="service-group" aria-labelledby="other-heading">
      <div class="service-group-heading">
        <h2 id="other-heading"><Icon name="layers" />其他服务</h2>
        <span class="group-divider" aria-hidden="true"></span>
      </div>
      <div class="services-grid other-grid">
        <section id="points" class="service-card service-card--points">
          <div class="service-card-top">
            <span class="service-label"><b>04</b>激励</span>
            <span class="service-art service-art--reward" aria-hidden="true">
              <span class="art-orbit"></span
              ><span class="art-tile"><Icon name="gift" /></span>
              <span class="art-spark"><Icon name="sparkles" /></span>
            </span>
          </div>
          <h2>AI 激励计划</h2>
          <p>AI激励政策正在积极规划中...</p>
          <div class="service-card-footer">
            <span class="incentive-hint">
              <button
                type="button"
                class="service-action service-action--disabled"
                aria-disabled="true"
                aria-describedby="incentive-tooltip"
              >
                查看激励 <TripleArrow compact />
              </button>
              <span
                id="incentive-tooltip"
                class="service-tooltip"
                role="tooltip"
                >AI激励政策正在积极规划中...</span
              >
            </span>
          </div>
        </section>
        <section id="officer" class="service-card service-card--people">
          <div class="service-card-top">
            <span class="service-label"><b>05</b>支持</span>
            <span class="service-art service-art--people" aria-hidden="true">
              <span class="art-orbit"></span
              ><span class="art-tile"><Icon name="users" /></span>
              <span class="art-spark"><Icon name="sparkles" /></span>
            </span>
          </div>
          <h2>联系团队 AI 领航团</h2>
          <p>组织效能团队与各业务团队 AI 领航团。</p>
          <div class="service-card-footer">
            <AppLink class="service-action" href="officers.html"
              >查看 AI 领航团 <TripleArrow compact
            /></AppLink>
          </div>
        </section>
      </div>
    </section>
    <ReimbursementDialog ref="reimbursement" />
  </main>
</template>

<style scoped>
.services-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 24px;
  align-items: stretch;
}
.service-card {
  --accent: #3b76ab;
  --tint: #c8e0f7;
  position: relative;
  isolation: isolate;
  display: grid;
  grid-template-rows: 104px auto 1fr 64px;
  min-width: 0;
  min-height: 354px;
  padding: 28px 30px 16px;
  text-align: left;
  font: inherit;
  color: #20364d;
  border: 1px solid #ffffff;
  border-radius: 28px;
  background:
    radial-gradient(ellipse at 100% 0%, var(--tint), transparent 66%),
    linear-gradient(145deg, #fffffffa, #f8fbfbed);
  box-shadow:
    0 4px 8px #35577404,
    0 20px 40px -28px #294b6d42,
    inset 0 0 0 1px #ffffff80;
  transition:
    transform 0.28s ease,
    box-shadow 0.28s ease;
}
.service-card::before {
  content: "";
  position: absolute;
  z-index: -1;
  inset: 0;
  border-radius: inherit;
  pointer-events: none;
  background: repeating-linear-gradient(
    125deg,
    transparent 0 27px,
    #ffffff40 28px,
    transparent 29px 55px
  );
  mask-image: linear-gradient(220deg, #000, transparent 65%);
}
.service-card--points {
  --accent: #797599;
  --tint: #dddcef;
}
.service-card--people {
  --accent: #377d80;
  --tint: #cbe5e5;
}
.service-card:hover {
  transform: translateY(-4px);
  box-shadow:
    0 8px 16px #35577408,
    0 26px 44px -26px #294b6d48;
}

.service-card-top {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
}
.service-label {
  display: inline-flex;
  align-items: center;
  gap: 12px;
  color: var(--accent);
  font-size: 12px;
  letter-spacing: 0.06em;
  padding-top: 8px;
}
.service-label b {
  font-weight: 500;
  font-size: 14px;
  letter-spacing: 0.1em;
}
.service-label b::after {
  content: "";
  display: inline-block;
  width: 1px;
  height: 11px;
  background: currentColor;
  opacity: 0.3;
  margin-left: 12px;
  transform: rotate(20deg);
}
.service-art {
  position: relative;
  width: 100px;
  height: 88px;
  margin-top: -4px;
  color: var(--accent);
}
.art-orbit {
  position: absolute;
  width: 98px;
  height: 74px;
  top: 9px;
  left: -2px;
  border: 1px solid #ffffffd9;
  border-radius: 50%;
  transform: rotate(-32deg);
}
.art-orbit::after {
  content: "";
  position: absolute;
  inset: 8px -8px;
  border: 1px solid currentColor;
  opacity: 0.13;
  border-radius: 50%;
}
.art-tile {
  position: absolute;
  top: 6px;
  left: 19px;
  display: grid;
  place-items: center;
  width: 62px;
  height: 68px;
  border: 1px solid #fff;
  border-radius: 17px;
  background: linear-gradient(140deg, #ffffffee, #ffffff55);
  transform: rotate(9deg);
  box-shadow:
    3px 9px 20px -10px var(--accent),
    inset 0 0 0 3px #ffffff33;
  transition: transform 0.3s ease;
}
.art-tile .ui-icon {
  width: 29px;
  height: 29px;
  stroke-width: 1.3;
}
.art-spark {
  position: absolute;
  right: 0;
  top: 0;
  color: var(--accent);
}
.art-spark .ui-icon {
  width: 18px;
  height: 18px;
}
.service-art--reward .art-tile {
  transform: rotate(-9deg);
  border-radius: 50%;
  width: 66px;
  height: 66px;
}
.service-art--people .art-tile {
  transform: rotate(5deg);
}
.service-card:hover .art-tile {
  transform: translateY(-3px) rotate(0deg);
}
.service-card h2 {
  margin: 0;
  font-size: clamp(20px, 1.65vw, 26px);
  line-height: 1.4;
  font-weight: 650;
  letter-spacing: -0.035em;
}
.service-card p {
  margin: 17px 0 30px;
  color: #6e8195;
  font-size: 14px;
  line-height: 1.85;
}
.service-card-footer {
  display: flex;
  align-items: center;
  border-top: 1px solid #849eb929;
  min-width: 0;
}
.service-action {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  min-height: 44px;
  font: inherit;
  font-size: 14px;
  font-weight: 550;
  color: var(--accent);
  text-decoration: none;
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
}
.service-card--expense .service-action,
.service-card--people .service-action {
  width: 100%;
}
.service-action--disabled {
  color: #939ba9;
  cursor: not-allowed;
}
.incentive-hint {
  position: relative;
  display: inline-flex;
  width: 100%;
}
.incentive-hint .service-action {
  width: 100%;
}
.service-tooltip {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 0;
  z-index: 5;
  width: max-content;
  max-width: min(280px, 75vw);
  padding: 12px 16px;
  border: 1px solid #ffffff36;
  border-radius: 12px;
  background: #283b52;
  color: #fff;
  box-shadow: 0 10px 28px #20364d26;
  font-size: 12px;
  font-weight: 400;
  line-height: 1.6;
  pointer-events: none;
  opacity: 0;
  visibility: hidden;
  transform: translateY(5px);
  transition:
    opacity 0.18s,
    transform 0.18s,
    visibility 0.18s;
}
.incentive-hint:hover .service-tooltip,
.incentive-hint:focus-within .service-tooltip {
  visibility: visible;
  opacity: 1;
  transform: translateY(0);
}
.service-card--expense:focus-visible,
.service-action:focus-visible {
  outline: 3px solid #4f8dde;
  outline-offset: 4px;
}
@media (max-width: 1100px) {
  .services-grid {
    gap: 16px;
  }
  .service-card {
    padding: 24px 22px 12px;
    grid-template-rows: 100px 58px 1fr 64px;
  }
}
@media (max-width: 760px) {
  .services-grid {
    grid-template-columns: 1fr;
    gap: 20px;
  }
  .service-card {
    padding: 24px 26px 12px;
    min-height: 300px;
    grid-template-rows: 88px auto 1fr 60px;
  }
  .service-card h2 {
    font-size: 23px;
  }
  .service-card p {
    margin-bottom: 24px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .service-card,
  .art-tile,
  .service-tooltip {
    transition: none;
  }
  .service-card:hover,
  .service-card:hover .art-tile {
    transform: none;
  }
}

.service-group + .service-group {
  margin-top: 56px;
}
.service-group-heading {
  display: flex;
  align-items: center;
  gap: 20px;
  margin-bottom: 24px;
}
.service-group-heading h2 {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  margin: 0;
  color: #274460;
  font-size: 20px;
  font-weight: 650;
  white-space: nowrap;
}
.service-group-heading h2 .ui-icon {
  width: 20px;
  height: 20px;
  color: #5485b7;
}
.group-caption {
  font-size: 12px;
  color: #61788f;
}
.group-divider {
  height: 1px;
  min-width: 24px;
  flex: 1;
  background: linear-gradient(90deg, #4a91e2bb, #82c7da70, #bec4e82e);
  position: relative;
}
.group-divider::before {
  content: "";
  position: absolute;
  left: 0;
  top: -1px;
  width: 54px;
  height: 3px;
  background: linear-gradient(90deg, #4c8ddb, transparent);
  border-radius: 3px;
}
.other-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.other-grid .service-card {
  min-height: 420px;
}
.service-card--relay {
  --accent: #576a9e;
  --tint: #d8e0f5;
}
.service-card--official {
  --accent: #287e72;
  --tint: #cce8df;
}
.tools-grid {
  align-items: start;
}
.tool-card {
  display: flex;
  flex-direction: column;
  padding: 24px 24px 14px;
}
.tool-card .service-card-top {
  min-height: 91px;
}
.tool-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  min-height: 35px;
}
.tool-title h3 {
  margin: 0;
  font-size: clamp(21px, 1.65vw, 26px);
  font-weight: 650;
  letter-spacing: -0.035em;
  line-height: 1.4;
}
.tool-card .tool-description {
  font-size: 13px;
  margin: 10px 0 20px;
}
.recommend-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: #e2f4eb;
  border: 1px solid #acd8c4;
  color: #2d7960;
  font-size: 11px;
  font-weight: 600;
  padding: 4px 8px;
  border-radius: 7px;
  box-shadow: inset 0 1px 0 #fff9;
}
.recommend-tag .ui-icon {
  width: 13px;
  height: 13px;
}
.channel-allowance {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 16px 0 20px;
  border-top: 1px solid #ffffffc9;
  border-bottom: 1px solid #849eb929;
  color: var(--accent);
}
.channel-allowance {
  min-height: 79px;
}
.channel-allowance strong {
  font-size: 42px;
  font-weight: 600;
  letter-spacing: -2px;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}
.channel-allowance--plan strong {
  font-size: clamp(22px, 2vw, 27px);
  letter-spacing: -1px;
}
.channel-allowance span {
  font-size: 11px;
}
.channel-rules {
  margin: 8px 0 22px;
  font-size: 13px;
  line-height: 1.85;
}
.channel-rules > div {
  display: grid;
  grid-template-columns: 66px minmax(0, 1fr);
  gap: 12px;
  padding: 13px 0;
  border-bottom: 1px solid #849eb91b;
}
.channel-rules > div:last-child {
  border-bottom: 0;
}
.channel-rules dt {
  color: #718397;
  font-size: 12px;
}
.channel-rules dd {
  margin: 0;
  color: #3f556b;
  overflow-wrap: anywhere;
}
.tool-card .service-card-footer {
  margin-top: auto;
  min-height: 66px;
}
.tool-card .service-action {
  width: 100%;
  gap: 12px;
}
@media (max-width: 1100px) and (min-width: 761px) {
  .tool-card {
    padding: 22px 18px 12px;
  }
  .channel-rules > div {
    grid-template-columns: 1fr;
    gap: 3px;
  }
  .tool-title {
    align-items: flex-start;
    min-height: 66px;
    flex-direction: column;
    gap: 4px;
  }
  .tool-card .tool-description {
    min-height: 48px;
  }
}
@media (max-width: 760px) {
  .other-grid {
    grid-template-columns: 1fr;
  }
  .service-group-heading {
    flex-wrap: wrap;
    gap: 12px;
  }
  .group-caption {
    width: 100%;
  }
  .service-group + .service-group {
    margin-top: 40px;
  }
  .tool-card {
    padding: 24px 24px 12px;
  }
}

.channel-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 54px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--accent);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.channel-toggle:hover {
  color: #1b4b73;
}
.channel-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: 4px;
}
.channel-disclosure {
  display: grid;
  grid-template-rows: 0fr;
  opacity: 0;
  transition:
    grid-template-rows 0.32s ease,
    opacity 0.22s ease;
}
.channel-disclosure.is-open {
  grid-template-rows: 1fr;
  opacity: 1;
}
.channel-disclosure-inner {
  overflow: hidden;
  min-height: 0;
}
@media (prefers-reduced-motion: reduce) {
  .channel-disclosure {
    transition: none;
  }
}
</style>
