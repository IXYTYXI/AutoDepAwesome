<script setup>
import { computed, ref } from "vue";
import PageBreadcrumb from "../components/PageBreadcrumb.vue";
import DeploymentActivity from "../components/deployment/DeploymentActivity.vue";
import PageHeading from "../components/PageHeading.vue";
import { useDeployment } from "../composables/useDeployment.js";
const {
  nameFeedback,
  step,
  form,
  touched,
  errors,
  notice,
  next,
  back,
  submit,
  busy,
  job,
  history,
  options,
  viewJob,
  applications,
} = useDeployment();
const steps = ["选择应用", "连接仓库", "项目名称", "确认部署"];
const stepTitle = ref(null);
const wizard = ref(null);
function revealStep() {
  stepTitle.value?.focus({ preventScroll: true });
  wizard.value?.scrollIntoView({
    block: "start",
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
      ? "instant"
      : "smooth",
  });
}
const progress = computed(() => Math.round((step.value / 3) * 100));
const domainName = computed(() => form.name || "项目名称");
function continueStep() {
  if (step.value === 3) submit();
  else next();
}
</script>

<template>
  <main class="portal-page deployment-page wrap" id="main-content">
    <PageBreadcrumb current="一键部署" />
    <header class="deploy-heading">
      <div>
        <PageHeading>一键部署<span class="deployment-dot">.</span></PageHeading>
        <p>从代码仓库，到你的应用。</p>
      </div>
      <button type="button" class="deploy-skill-download" title="即将提供">
        <Icon name="file-down" />下载部署配置 Skill
      </button>
    </header>

    <section class="resource-card" aria-labelledby="resource-title">
      <div class="resource-heading">
        <span class="resource-icon"><Icon name="triangle-alert" /></span>
        <div>
          <span class="resource-eyebrow">部署前请确认</span>
          <h2 id="resource-title">每应用资源上限</h2>
        </div>
        <span class="resource-badge">多个服务按应用合计</span>
      </div>
      <div class="resource-values">
        <div>
          <Icon name="hard-drive" /><span>持久数据</span
          ><strong>50 <small>MiB</small></strong>
        </div>
        <div>
          <Icon name="memory-stick" /><span>运行内存</span
          ><strong>500 <small>MiB</small></strong>
        </div>
        <div class="resource-pool">
          <Icon name="layers" /><span>平台共享存储池</span
          ><strong>64 <small>GiB</small></strong>
        </div>
      </div>
      <p class="resource-warning">
        <Icon name="info" /><span
          >内存按需使用，不固定预占；<strong
            >写满存储会阻止继续写入，内存超限可能终止服务。</strong
          ></span
        >
      </p>
    </section>

    <section ref="wizard" class="deploy-wizard" aria-label="部署配置向导">
      <div class="wizard-progress-heading">
        <span>部署配置</span
        ><span
          >步骤 <b>{{ step + 1 }}</b> / 4</span
        >
      </div>
      <nav class="deploy-steps" aria-label="部署步骤">
        <div
          class="step-track"
          role="progressbar"
          aria-label="配置进度"
          :aria-valuenow="progress"
          aria-valuemin="0"
          aria-valuemax="100"
        >
          <span :style="{ width: progress + '%' }"></span>
        </div>
        <ol>
          <li
            v-for="(label, index) in steps"
            :key="label"
            :class="{ current: step === index, complete: index < step }"
          >
            <button
              type="button"
              :disabled="index > step"
              :aria-current="index === step ? 'step' : undefined"
              :aria-label="
                (index < step ? '返回' : '') +
                '第' +
                (index + 1) +
                '步：' +
                label
              "
              @click="back(index)"
            >
              <span class="step-node"
                ><Icon v-if="index < step" name="check" /><span v-else>{{
                  String(index + 1).padStart(2, "0")
                }}</span></span
              >
              <span class="step-label">{{ label }}</span>
            </button>
          </li>
        </ol>
      </nav>

      <div class="wizard-body">
        <form class="deploy-form" novalidate @submit.prevent="continueStep">
          <Transition
            name="deploy-step"
            mode="out-in"
            @after-enter="revealStep"
          >
            <div :key="step" class="step-content">
              <div class="step-kicker">
                <span>{{ String(step + 1).padStart(2, "0") }}</span
                ><span v-if="step < 3" class="required-chip">必填</span
                ><span v-else class="review-chip">最后一步</span>
              </div>
              <h2 ref="stepTitle" tabindex="-1">{{ steps[step] }}</h2>
              <template v-if="step === 0">
                <p class="step-description">选择本次部署的应用。</p>
                <label for="deploy-application"
                  >应用 <span class="required-star">*</span></label
                >
                <div class="deploy-select">
                  <select
                    id="deploy-application"
                    v-model="form.application"
                    required
                    :aria-invalid="!!errors[0]"
                    aria-describedby="application-error"
                  >
                    <option value="create">创建新应用</option>
                    <option
                      v-for="app in applications"
                      :key="app.appId"
                      :value="app.appId"
                    >
                      更新 {{ app.subdomain }}
                    </option></select
                  ><TripleArrow compact direction="down" />
                </div>
                <p
                  v-if="errors[0]"
                  id="application-error"
                  class="field-error"
                  role="alert"
                >
                  {{ errors[0] }}
                </p>
                <div class="application-note">
                  <span class="application-icon"><Icon name="blocks" /></span>
                  <div>
                    <strong>新的开始</strong>
                    <p>为你的项目创建独立应用与访问地址。</p>
                  </div>
                </div>
              </template>
              <template v-else-if="step === 1">
                <p class="step-description">连接需要部署的代码仓库。</p>
                <label for="deploy-repository"
                  >GitHub / GitLab 仓库地址
                  <span class="required-star">*</span></label
                >
                <div class="deploy-input-wrap">
                  <Icon name="git-branch" /><input
                    id="deploy-repository"
                    v-model="form.repository"
                    type="url"
                    required
                    autocomplete="off"
                    spellcheck="false"
                    placeholder="https://github.com/team/project.git"
                    :aria-invalid="touched[1] && !!errors[1]"
                    aria-describedby="repository-help repository-error"
                    @blur="touched[1] = true"
                  />
                </div>
                <p id="repository-help" class="field-help">
                  默认部署 <code>main</code> 分支，无需填写版本。支持自建 GitLab
                  仓库。
                </p>
                <p id="repository-error" class="field-error" role="alert">
                  {{ touched[1] ? errors[1] : "" }}
                </p>
              </template>
              <template v-else-if="step === 2">
                <p class="step-description">给应用一个简短、好记的访问名称。</p>
                <label for="deploy-name"
                  >项目名称 <span class="required-star">*</span></label
                >
                <div class="deploy-input-wrap name-input">
                  <input
                    id="deploy-name"
                    v-model="form.name"
                    required
                    autocomplete="off"
                    autocapitalize="none"
                    spellcheck="false"
                    placeholder="例如 demo"
                    :aria-invalid="(touched[2] || !!form.name) && !!errors[2]"
                    aria-describedby="name-help name-error domain-preview"
                    @blur="touched[2] = true"
                  /><span :class="{ invalid: form.name.length > 7 }"
                    >{{ form.name.length }} / 7</span
                  >
                </div>
                <p id="name-help" class="field-help">
                  1–7
                  位小写字母、数字或中划线（-），首尾须为字母或数字；不能包含中文、空格或大写字母。名称用于访问域名，须与
                  <code>awesome.yaml</code> 的 <code>subdomain</code> 一致。
                </p>
                <p id="name-error" class="field-error" role="alert">
                  {{ touched[2] || form.name ? errors[2] : "" }}
                </p>
                <p
                  v-if="nameFeedback && !errors[2]"
                  class="field-help"
                  role="status"
                >
                  {{ nameFeedback }}
                </p>
                <div
                  id="domain-preview"
                  class="domain-preview"
                  aria-live="polite"
                >
                  <Icon name="globe" />
                  <div>
                    <span>访问地址</span>
                    <p>
                      <strong :class="{ invalid: form.name && errors[2] }">{{
                        domainName
                      }}</strong
                      ><span>.{{ options.baseDomain }}</span>
                    </p>
                  </div>
                </div>
              </template>
              <template v-else>
                <p class="step-description">检查配置信息，准备部署。</p>
                <dl class="deployment-review">
                  <div>
                    <dt>应用</dt>
                    <dd>
                      {{
                        form.application === "create"
                          ? "创建新应用"
                          : "更新已有应用"
                      }}
                    </dd>
                  </div>
                  <div>
                    <dt>代码仓库</dt>
                    <dd>{{ form.repository.trim() }}</dd>
                  </div>
                  <div>
                    <dt>部署分支</dt>
                    <dd><code>main</code></dd>
                  </div>
                  <div>
                    <dt>项目名称</dt>
                    <dd>{{ form.name }}</dd>
                  </div>
                </dl>
                <div class="domain-preview">
                  <Icon name="globe" />
                  <div>
                    <span>访问地址</span>
                    <p>
                      <strong>{{ domainName }}</strong
                      ><span>.{{ options.baseDomain }}</span>
                    </p>
                  </div>
                </div>
                <p class="preview-mode">
                  <Icon name="info" />部署服务待接入，当前仅预览配置。
                </p>
              </template>
            </div>
          </Transition>
          <div class="wizard-actions">
            <button
              v-if="step > 0"
              type="button"
              class="previous-step"
              @click="back()"
            >
              <TripleArrow compact direction="left" />上一步</button
            ><span v-else class="step-guidance">按步骤完成配置</span
            ><button
              type="submit"
              class="next-step"
              :disabled="
                busy ||
                (step === 3 &&
                  (!options.executionEnabled || (job && !job.terminal)))
              "
            >
              <Icon v-if="step === 3" name="rocket" />{{
                busy ? "正在提交" : step === 3 ? "开始部署" : "下一步"
              }}<TripleArrow v-if="step < 3" compact />
            </button>
          </div>
          <p v-if="!options.executionEnabled" class="deployment-notice">
            部署执行环境待配置，暂未开放提交。
          </p>
          <DeploymentActivity :job="job" :history="history" @select="viewJob" />
          <p v-if="notice" class="deployment-notice" role="status">
            <Icon name="info" />{{ notice }}
          </p>
        </form>

        <aside class="deploy-blueprint" aria-label="应用配置概览">
          <div class="blueprint-top">
            <span class="blueprint-dot"></span>你的应用
          </div>
          <div class="blueprint-art" aria-hidden="true">
            <span class="blueprint-orbit"></span
            ><span class="blueprint-rocket"><Icon name="rocket" /></span
            ><span class="blueprint-star star-one"></span
            ><span class="blueprint-star star-two"></span>
          </div>
          <h3>{{ form.name || "等待命名" }}</h3>
          <p class="blueprint-domain">
            <strong>{{ domainName }}</strong
            ><span>.{{ options.baseDomain }}</span>
          </p>
          <div class="blueprint-divider"></div>
          <dl>
            <div>
              <dt><Icon name="blocks" />应用类型</dt>
              <dd>新应用</dd>
            </div>
            <div>
              <dt><Icon name="git-branch" />部署分支</dt>
              <dd>main</dd>
            </div>
            <div>
              <dt><Icon name="folder" />代码仓库</dt>
              <dd :title="form.repository || undefined">
                {{ !errors[1] ? "已填写" : "待填写" }}
              </dd>
            </div>
          </dl>
          <span class="blueprint-footnote">{{
            step === 3
              ? "配置已就绪 · 服务待接入"
              : "配置进度 " + progress + "%"
          }}</span>
        </aside>
      </div>
    </section>
  </main>
</template>

<style scoped>
.deployment-dot {
  color: #247cf0;
}
.deploy-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 28px;
}
.deploy-heading p {
  color: #71849c;
  font-size: 14px;
  margin: 5px 0 0;
}
.deploy-skill-download {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  border: 1px solid #c8d8eb;
  background: #f8fbffde;
  border-radius: 13px;
  padding: 13px 18px;
  color: #3e608a;
  font-size: 13px;
  box-shadow: 0 4px 12px #2e568a06;
  white-space: nowrap;
}
.deploy-skill-download:hover {
  background: #fff;
  border-color: #9fbbe0;
}
.deploy-skill-download .ui-icon {
  width: 18px;
  height: 18px;
}
.resource-card {
  padding: 22px 28px 18px;
  border: 1px solid #edcea1;
  border-left: 4px solid #d99b3a;
  border-radius: 20px;
  background: linear-gradient(110deg, #fff6e5, #fffdf8 66%, #fffaf0);
  box-shadow: 0 8px 25px #93662309;
  margin-bottom: 28px;
}
.resource-heading {
  display: flex;
  align-items: center;
  gap: 12px;
}
.resource-icon {
  width: 42px;
  height: 42px;
  display: grid;
  place-items: center;
  background: #f8e6c4;
  color: #a06b18;
  border-radius: 12px;
}
.resource-icon .ui-icon {
  width: 21px;
  height: 21px;
}
.resource-eyebrow {
  color: #a77c3c;
  font-size: 10px;
  letter-spacing: 0.08em;
}
.resource-heading h2 {
  margin: 3px 0 0;
  font-size: 17px;
  font-weight: 650;
  color: #70532b;
}
.resource-badge {
  margin-left: auto;
  font-size: 11px;
  color: #8d7654;
}
.resource-values {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  margin: 20px 0 17px;
}
.resource-values > div {
  display: grid;
  grid-template-columns: 20px 1fr;
  align-items: center;
  column-gap: 8px;
  padding: 0 24px;
  border-left: 1px solid #d7b78055;
}
.resource-values > div:first-child {
  padding-left: 0;
  border: 0;
}
.resource-values .ui-icon {
  width: 17px;
  height: 17px;
  color: #aa8959;
}
.resource-values span {
  color: #8b775b;
  font-size: 12px;
}
.resource-values strong {
  grid-column: 2;
  font-size: 29px;
  font-weight: 650;
  color: #60492c;
  line-height: 1.5;
  font-variant-numeric: tabular-nums;
}
.resource-values small {
  font-size: 13px;
  font-weight: 500;
  margin-left: 3px;
  color: #8b775b;
}
.resource-warning {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  border-top: 1px solid #decaa755;
  padding-top: 13px;
  font-size: 12px;
  line-height: 1.7;
  color: #8c775a;
}
.resource-warning .ui-icon {
  flex-shrink: 0;
  width: 15px;
  height: 15px;
  margin-top: 3px;
}
.resource-warning strong {
  color: #946326;
  font-weight: 550;
}
.deploy-wizard {
  scroll-margin-top: var(--header-offset, 120px);
  background: #fcfdfff2;
  border: 1px solid white;
  border-radius: 26px;
  padding: 26px 32px 32px;
  box-shadow: 0 20px 50px -30px #284e7c40;
}
.wizard-progress-heading {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: #8b9ab0;
}
.wizard-progress-heading > span:first-child {
  color: #56708d;
  font-weight: 600;
}
.wizard-progress-heading b {
  color: #2467d9;
  font-weight: 600;
}
.deploy-steps {
  position: relative;
  display: block;
  width: 100%;
  margin: 22px 0 30px;
  padding: 0;
}
.deploy-steps ol {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0;
}
.deploy-steps li {
  text-align: center;
  position: relative;
}
.deploy-steps button {
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 0 8px;
  color: #92a0b4;
}
.deploy-steps button:disabled {
  cursor: not-allowed;
}
.step-node {
  display: grid;
  place-items: center;
  position: relative;
  width: 38px;
  height: 38px;
  border-radius: 13px;
  background: #edf2f9;
  border: 1px solid #dbe4ef;
  font-size: 12px;
  font-weight: 600;
  box-shadow: 0 0 0 7px #fcfdff;
  transition:
    background 0.25s,
    box-shadow 0.25s;
}
.step-node .ui-icon {
  width: 18px;
  height: 18px;
}
.step-label {
  font-size: 13px;
  font-weight: 550;
}
.current button {
  color: #2d64b9;
}
.current .step-node {
  background: linear-gradient(145deg, #4385ef, #2865d5);
  border-color: #3672de;
  color: white;
  box-shadow:
    0 0 0 6px #eef4ff,
    0 5px 13px #2865d526;
}
.complete button {
  color: #557597;
}
.complete .step-node {
  background: #e7f1ff;
  border-color: #bbd4f4;
  color: #3075ce;
}
.step-track {
  position: absolute;
  height: 2px;
  background: #e2e9f2;
  left: 12.5%;
  right: 12.5%;
  top: 19px;
}
.step-track > span {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, #9bbfed, #3676dd);
  transition: width 0.4s ease;
}
.wizard-body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 290px;
  gap: 36px;
  border-top: 1px solid #e5ecf4;
  padding-top: 30px;
}
.deploy-form {
  display: block;
  padding: 0;
  margin: 0;
  min-width: 0;
  background: none;
  border: 0;
}
.step-content {
  min-height: 320px;
}
.step-kicker {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.step-kicker > span:first-child {
  color: #829cbb;
  font-size: 12px;
  letter-spacing: 0.12em;
}
.required-chip {
  font-size: 10px;
  padding: 3px 8px;
  border-radius: 5px;
  color: #b15d47;
  background: #fff0eb;
}
.review-chip {
  font-size: 10px;
  color: #4174b6;
}
.step-content h2 {
  font-size: 24px;
  color: #263f5e;
  font-weight: 600;
  margin: 0 0 7px;
  letter-spacing: -0.03em;
}
.step-content h2:focus {
  outline: none;
}
.step-description {
  color: #8190a4;
  font-size: 13px;
  margin: 0 0 26px;
}
.deploy-form label {
  display: block;
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 550;
  color: #425c7a;
}
.required-star {
  color: #b86751;
  margin-left: 4px;
}
.deploy-select {
  position: relative;
}
.deploy-select select,
.deploy-input-wrap input {
  width: 100%;
  height: 54px;
  border: 1px solid #cbd9eb;
  border-radius: 12px;
  background: #fff;
  font: inherit;
  font-size: 15px;
  color: #294563;
  padding: 0 16px;
  outline: none;
  margin: 0;
}
.deploy-select select {
  appearance: none;
  padding-right: 46px;
  cursor: pointer;
}
.deploy-select > .triple-arrow {
  position: absolute;
  right: 16px;
  top: 14px;
  color: #718cac;
  pointer-events: none;
}
.deploy-select select:focus,
.deploy-input-wrap input:focus {
  border-color: #70a0e2;
  box-shadow: 0 0 0 4px #3781ef12;
}
.deploy-input-wrap {
  position: relative;
}
.deploy-input-wrap > .ui-icon {
  width: 18px;
  height: 18px;
  position: absolute;
  left: 16px;
  top: 18px;
  color: #7d94b0;
}
.deploy-input-wrap > .ui-icon + input {
  padding-left: 46px;
}
.deploy-input-wrap input[aria-invalid="true"] {
  border-color: #cf8e78;
  background: #fffcfa;
}
.deploy-input-wrap input::placeholder {
  color: #a1afc0;
  font-size: 13px;
}
.name-input input {
  padding-right: 65px;
}
.name-input > span {
  position: absolute;
  right: 15px;
  top: 19px;
  color: #99a8bb;
  font-size: 12px;
}
.field-help {
  color: #7c8da3;
  font-size: 12px;
  line-height: 1.85;
  margin: 12px 0 0;
}
.deploy-form code {
  font-size: 12px;
  padding: 2px 5px;
  border-radius: 4px;
  background: #eaf0f8;
  color: #4c6b90;
}
.field-error {
  color: #ad5946;
  min-height: 20px;
  font-size: 12px;
  line-height: 1.6;
  margin: 8px 0 0;
}
.application-note {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 25px;
  padding: 18px;
  border: 1px dashed #ccdaea;
  background: #f4f8fe;
  border-radius: 12px;
}
.application-icon {
  display: grid;
  place-items: center;
  width: 40px;
  height: 40px;
  color: #6288ba;
  background: #e4eefb;
  border-radius: 10px;
}
.application-icon .ui-icon {
  width: 20px;
  height: 20px;
}
.application-note strong {
  color: #58718f;
  font-size: 13px;
  font-weight: 550;
}
.application-note p {
  color: #8a9aaf;
  font-size: 12px;
  margin: 5px 0 0;
}
.domain-preview {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid #d3e2f5;
  border-radius: 12px;
  background: linear-gradient(110deg, #eaf3ff, #f8fbff);
  margin-top: 14px;
}
.domain-preview > .ui-icon {
  width: 21px;
  height: 21px;
  flex-shrink: 0;
  color: #7999c3;
}
.domain-preview > div {
  min-width: 0;
}
.domain-preview > div > span {
  font-size: 10px;
  color: #7b92b0;
}
.domain-preview p {
  margin: 4px 0 0;
  font-size: 14px;
  line-height: 1.6;
  overflow-wrap: anywhere;
  color: #7c8fa8;
}
.domain-preview strong {
  color: #246add;
  font-weight: 650;
}
.name-input .invalid,
.domain-preview .invalid {
  color: #b15d47;
}
.wizard-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-top: 22px;
  margin-top: 22px;
  border-top: 1px solid #e4ebf4;
}
.step-guidance {
  font-size: 12px;
  color: #97a7bb;
}
.previous-step {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: #6e85a2;
  font-size: 13px;
  padding: 10px 0;
}
.next-step {
  display: inline-flex;
  justify-content: center;
  align-items: center;
  gap: 20px;
  min-width: 140px;
  padding: 13px 20px;
  border: 1px solid #2864d5;
  border-radius: 12px;
  background: linear-gradient(135deg, #3a7bef, #2561d2);
  color: white;
  font-size: 14px;
  font-weight: 550;
  box-shadow: 0 5px 14px #2865d526;
}
.next-step:hover {
  color: white;
  background: #245acc;
}
.next-step > .ui-icon {
  width: 18px;
  height: 18px;
}
.deployment-review {
  margin: 0;
}
.deployment-review > div {
  display: grid;
  grid-template-columns: 85px minmax(0, 1fr);
  gap: 12px;
  border-bottom: 1px solid #e8eef6;
  padding: 11px 0;
  font-size: 13px;
}
.deployment-review dt {
  color: #8a9bb0;
}
.deployment-review dd {
  color: #3d5879;
  margin: 0;
  overflow-wrap: anywhere;
}
.preview-mode,
.deployment-notice {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: #8a99ab;
  margin: 15px 0 0;
}
.preview-mode .ui-icon,
.deployment-notice .ui-icon {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
}
.deployment-notice {
  color: #577ca7;
  padding: 12px 14px;
  background: #edf4fd;
  border-radius: 9px;
}
.deploy-blueprint {
  position: relative;
  align-self: stretch;
  overflow: hidden;
  min-width: 0;
  border-radius: 19px;
  padding: 23px;
  background:
    radial-gradient(ellipse at 110% 0%, #466b99, transparent 65%),
    linear-gradient(150deg, #203951, #182d46);
  color: #b5c8e0;
}
.deploy-blueprint::before {
  content: "";
  position: absolute;
  inset: 0;
  opacity: 0.13;
  background-image: radial-gradient(#c8dbff 1px, transparent 1px);
  background-size: 18px 18px;
  pointer-events: none;
  mask-image: linear-gradient(#000, transparent 68%);
}
.blueprint-top {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  position: relative;
}
.blueprint-dot {
  width: 5px;
  height: 5px;
  background: #94bce9;
  border-radius: 50%;
  box-shadow: 0 0 0 4px #7cabe81a;
}
.blueprint-art {
  position: relative;
  display: grid;
  place-items: center;
  height: 140px;
}
.blueprint-orbit {
  position: absolute;
  width: 147px;
  height: 85px;
  border: 1px solid #adcff136;
  border-radius: 50%;
  transform: rotate(-30deg);
  box-shadow:
    0 0 0 15px #a5c8f007,
    0 0 0 30px #a5c8f005;
}
.blueprint-rocket {
  display: grid;
  place-items: center;
  width: 68px;
  height: 68px;
  border-radius: 20px;
  border: 1px solid #d3e5ff40;
  background: linear-gradient(135deg, #bedcff30, #bedcff0a);
  box-shadow:
    0 10px 25px #08182f20,
    inset 0 1px 0 #ffffff25;
  transform: rotate(-8deg);
}
.blueprint-rocket .ui-icon {
  width: 32px;
  height: 32px;
  color: #c7d8ef;
  stroke-width: 1.3;
}
.blueprint-star {
  position: absolute;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: #92b5df;
}
.star-one {
  left: 20px;
  top: 82px;
}
.star-two {
  right: 25px;
  top: 37px;
  width: 3px;
  height: 3px;
}
.deploy-blueprint h3 {
  font-size: 23px;
  font-weight: 550;
  margin: 0;
  color: #e7f0fc;
  overflow-wrap: anywhere;
}
.blueprint-domain {
  font-size: 11px;
  line-height: 1.8;
  overflow-wrap: anywhere;
  margin: 8px 0 22px;
  color: #819bbd;
}
.blueprint-domain strong {
  color: #b3d2ff;
  font-weight: 500;
}
.blueprint-divider {
  height: 1px;
  background: linear-gradient(90deg, #a3c7ee30, transparent);
}
.deploy-blueprint dl {
  margin: 18px 0 25px;
}
.deploy-blueprint dl > div {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 13px 0;
  gap: 10px;
  font-size: 11px;
}
.deploy-blueprint dt {
  display: flex;
  gap: 7px;
  align-items: center;
  color: #8da8c7;
}
.deploy-blueprint dt .ui-icon {
  width: 13px;
  height: 13px;
}
.deploy-blueprint dd {
  margin: 0;
  color: #c6d7ed;
}
.blueprint-footnote {
  font-size: 10px;
  color: #6f8aac;
}
.deploy-step-enter-active,
.deploy-step-leave-active {
  transition:
    opacity 0.18s ease,
    transform 0.18s ease;
}
.deploy-step-enter-from {
  opacity: 0;
  transform: translateX(9px);
}
.deploy-step-leave-to {
  opacity: 0;
  transform: translateX(-7px);
}
@media (max-width: 1000px) {
  .wizard-body {
    grid-template-columns: minmax(0, 1fr) 240px;
    gap: 24px;
  }
  .deploy-wizard {
    padding: 24px;
  }
}
@media (max-width: 760px) {
  .deploy-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 16px;
  }
  .deploy-skill-download {
    font-size: 12px;
    padding: 11px 14px;
  }
  .resource-card {
    padding: 18px 16px 14px;
  }
  .resource-badge {
    display: none;
  }
  .resource-values > div {
    padding: 0 12px;
    grid-template-columns: 1fr;
    gap: 3px;
  }
  .resource-values .ui-icon {
    display: none;
  }
  .resource-values strong {
    grid-column: 1;
    font-size: 23px;
  }
  .resource-values span {
    font-size: 10px;
  }
  .resource-values small {
    font-size: 10px;
    margin: 0;
  }
  .resource-heading h2 {
    font-size: 16px;
  }
  .wizard-body {
    grid-template-columns: 1fr;
    padding-top: 24px;
  }
  .deploy-blueprint {
    display: none;
  }
  .deploy-wizard {
    padding: 22px 18px;
    border-radius: 20px;
  }
  .step-label {
    font-size: 11px;
  }
  .step-node {
    width: 34px;
    height: 34px;
    border-radius: 11px;
    box-shadow: 0 0 0 4px #fcfdff;
  }
  .step-track {
    top: 17px;
  }
  .deploy-steps button {
    padding: 0 3px;
  }
  .step-content {
    min-height: 340px;
  }
  .step-content h2 {
    font-size: 22px;
  }
  .domain-preview {
    padding: 12px;
  }
  .domain-preview p {
    font-size: 12px;
  }
  .deploy-form input {
    font-size: 16px;
  }
  .next-step {
    min-width: 125px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .deploy-step-enter-active,
  .deploy-step-leave-active,
  .step-track > span,
  .step-node {
    transition: none;
  }
}
</style>

<style scoped>
.next-step:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
