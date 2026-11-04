<script setup>
import { computed } from "vue";
const props = defineProps({
  count: Number,
  modelValue: Number,
  pageSize: { type: Number, default: 6 },
});
const emit = defineEmits(["update:modelValue"]);
const total = computed(() =>
  Math.max(1, Math.ceil(props.count / props.pageSize)),
);
function step(n) {
  emit("update:modelValue", props.modelValue + n);
  document.querySelector("main")?.scrollIntoView();
}
</script>
<template>
  <nav class="pagination" aria-label="分页">
    <span>共 {{ count }} 条</span>
    <div>
      <UButton
        color="neutral"
        variant="outline"
        size="sm"
        :disabled="modelValue <= 1"
        aria-label="上一页"
        @click="step(-1)"
      >
        <TripleArrow compact direction="left"  /></UButton
      ><span>{{ modelValue }} / {{ total }}</span
      ><UButton
        color="neutral"
        variant="outline"
        size="sm"
        :disabled="modelValue >= total"
        aria-label="下一页"
        @click="step(1)"
      >
        <TripleArrow compact  />
      </UButton>
    </div>
  </nav>
</template>
