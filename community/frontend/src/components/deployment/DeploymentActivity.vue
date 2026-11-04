<script setup>
import DeploymentStatus from "./DeploymentStatus.vue";
defineProps({ job: Object, history: { type: Array, default: () => [] } });
defineEmits(["select"]);
</script>
<template>
  <section v-if="job" class="deployment-result" aria-live="polite">
    <h3><DeploymentStatus :status="job.status" /></h3>
    <p>
      {{ job.subdomain }} ·
      {{
        {
          queued: "排队",
          source: "获取代码",
          prepared: "配置检查",
          freeze: "固定版本",
          reserved: "预留资源",
          build: "构建镜像",
          built: "镜像就绪",
          start: "启动服务",
          health: "健康检查",
          https: "配置 HTTPS",
          publish: "发布路由",
          verify: "验证访问",
          complete: "完成",
        }[job.phase] || job.phase
      }}
    </p>
    <div v-if="!job.terminal" class="deployment-pulse"></div>
    <div v-if="job.error" class="deployment-failure" role="alert">
      <strong>{{ job.error.message }}</strong>
      <p v-if="job.error.suggestion">{{ job.error.suggestion }}</p>
      <small
        >{{ job.error.code
        }}<template v-if="job.error.field">
          · {{ job.error.field }}</template
        ></small
      >
    </div>
    <a v-if="job.url" :href="job.url" target="_blank" rel="noopener noreferrer"
      >{{ job.url }} <TripleArrow compact
    /></a>
  </section>
  <section v-if="history.length" class="deployment-history">
    <h3>我的部署记录</h3>
    <button
      v-for="item in history"
      :key="item.id"
      type="button"
      @click="$emit('select', item)"
      :aria-current="job?.id === item.id ? 'true' : undefined"
    >
      <span>{{ item.subdomain }}</span
      ><DeploymentStatus :status="item.status" />
      <TripleArrow compact />
    </button>
  </section>
</template>

<style scoped>
.deployment-result,
.deployment-history {
  margin-top: 24px;
  padding: 24px;
  border: 1px solid #cbdcf2;
  border-radius: 22px;
  background: linear-gradient(125deg, #eef5ff, #fff);
  color: #24466c;
}
.deployment-result h3,
.deployment-history h3 {
  font-size: 20px;
  margin: 0 0 12px;
}
.deployment-result p {
  margin: 12px 0;
}
.deployment-result a {
  display: inline-flex;
  gap: 12px;
  color: #2460ce;
}
.deployment-pulse {
  height: 4px;
  border-radius: 4px;
  background: linear-gradient(90deg, #d5e3fc, #3375ec, #d5e3fc);
  background-size: 200% 100%;
  animation: deploy-pulse 1.8s linear infinite;
}
.deployment-history button {
  display: flex;
  width: 100%;
  gap: 16px;
  align-items: center;
  padding: 14px 0;
  border: 0;
  border-bottom: 1px solid #d8e4f5;
  background: none;
  text-align: left;
  color: inherit;
}
.deployment-history button span:first-child {
  flex: 1;
}
.next-step:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
@keyframes deploy-pulse {
  to {
    background-position: -200% 0;
  }
}
@media (prefers-reduced-motion: reduce) {
  .deployment-pulse {
    animation: none;
  }
}
</style>

<style scoped>
.deployment-result a {
  overflow-wrap: anywhere;
  max-width: 100%;
  align-items: center;
}
.deployment-failure {
  background: #fff6f2;
  border: 1px solid #f1d3c9;
  border-radius: 14px;
  padding: 16px;
  margin: 16px 0;
  color: #8c4436;
  overflow-wrap: anywhere;
}
.deployment-failure small {
  display: block;
  margin-top: 8px;
}
.deployment-history button[aria-current="true"] {
  background: #e7f0ff;
  border-radius: 10px;
  padding-inline: 10px;
}
.deployment-history button > span:first-child {
  overflow-wrap: anywhere;
  min-width: 0;
}
</style>
