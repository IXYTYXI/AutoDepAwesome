<script setup>
defineProps({ state: Object });
defineEmits(["retry"]);
</script>
<template>
  <UAlert
    v-if="state.casesError"
    color="error"
    variant="soft"
    :title="state.casesError"
    class="mb-5"
    ><template #actions
      ><UButton
        size="sm"
        color="error"
        variant="outline"
        @click="$emit('retry')"
        >重新加载</UButton
      ></template
    ></UAlert
  ><UAlert
    v-else-if="state.casesStale"
    color="warning"
    variant="soft"
    title="正在展示上次同步的案例"
    description="更新暂不可用，请稍后再试。"
    class="mb-5"
  />
</template>
