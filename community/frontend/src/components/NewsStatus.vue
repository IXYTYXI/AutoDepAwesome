<script setup>
defineProps({ state: Object });
defineEmits(["retry"]);
</script>
<template>
  <UAlert
    v-if="state.newsError"
    color="error"
    variant="soft"
    :title="state.newsError"
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
  >
</template>
