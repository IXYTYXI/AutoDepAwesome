<script setup>
defineProps({ status: String });
const labels = {
  queued: "等待部署",
  running: "正在部署",
  succeeded: "部署成功",
  failed: "部署失败",
  recovery_required: "等待恢复处理",
  cancelled: "已取消",
};
</script>
<template>
  <span class="deployment-status" :data-status="status"
    ><i aria-hidden="true"></i>{{ labels[status] || status }}</span
  >
</template>
<style scoped>
.deployment-status {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 600;
  color: #436998;
}
i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
[data-status="succeeded"] {
  color: #237c68;
}
[data-status="failed"],
[data-status="recovery_required"] {
  color: #a64d40;
}
[data-status="running"] {
  color: #2864d3;
}
</style>
