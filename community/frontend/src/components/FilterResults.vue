<script setup>
defineProps({ filterKey: { type: String, required: true } });
</script>
<template>
  <div class="filter-stage">
    <Transition name="filter-change">
      <div :key="filterKey" class="filter-panel"><slot /></div>
    </Transition>
  </div>
</template>
<style scoped>
.filter-stage {
  display: grid;
  min-width: 0;
  isolation: isolate;
}
.filter-panel {
  grid-area: 1 / 1;
  min-width: 0;
  align-self: start;
}
.filter-change-enter-active {
  transition:
    opacity 0.26s ease,
    transform 0.26s cubic-bezier(0.2, 0.7, 0.2, 1),
    filter 0.26s ease;
}
.filter-change-leave-active {
  transition:
    opacity 0.12s ease,
    filter 0.12s ease;
  pointer-events: none;
}
.filter-change-enter-from {
  opacity: 0;
  transform: translateY(8px);
  filter: blur(3px);
}
.filter-change-leave-to {
  opacity: 0;
  filter: blur(2px);
}
@media (prefers-reduced-motion: reduce) {
  .filter-change-enter-active,
  .filter-change-leave-active {
    transition: none;
  }
  .filter-change-enter-from {
    transform: none;
    filter: none;
  }
}
</style>
