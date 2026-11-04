<script setup>
defineProps({
  direction: {
    type: String,
    default: "right",
    validator: (value) => ["right", "left", "up", "down"].includes(value),
  },
  compact: Boolean,
});
</script>
<template>
  <span
    class="triple-arrow"
    :class="{ 'triple-arrow-compact': compact }"
    :data-direction="direction"
    aria-hidden="true"
  >
    <span class="triple-arrow-track">
      <svg
        v-for="step in 3"
        :key="step"
        class="ui-icon"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.75"
        stroke-linecap="round"
        stroke-linejoin="round"
        focusable="false"
      >
        <use href="/assets/icons/lucide.svg#chevron-right" />
      </svg>
    </span>
  </span>
</template>
<style scoped>
.triple-arrow {
  --arrow-rotation: 0deg;
  --arrow-size: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: inherit;
  width: var(--arrow-size);
  height: 22px;
  vertical-align: middle;
}
.triple-arrow[data-direction="left"] {
  --arrow-rotation: 180deg;
}
.triple-arrow[data-direction="down"] {
  --arrow-rotation: 90deg;
}
.triple-arrow[data-direction="up"] {
  --arrow-rotation: -90deg;
}
.triple-arrow-compact {
  --arrow-size: 28px;
  height: 16px;
}
.triple-arrow[data-direction="up"],
.triple-arrow[data-direction="down"] {
  height: var(--arrow-size);
}
.triple-arrow-track {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  transform: rotate(var(--arrow-rotation));
  transition: transform 0.26s cubic-bezier(0.2, 0.8, 0.2, 1);
}
.triple-arrow .ui-icon {
  flex-shrink: 0;
  width: 48%;
  height: auto;
  margin-left: -16%;
  opacity: 0.35;
  animation: triple-flow 2.4s ease-in-out infinite;
}
.triple-arrow .ui-icon:first-child {
  margin-left: 0;
}
.triple-arrow .ui-icon:nth-child(2) {
  animation-delay: 0.16s;
}
.triple-arrow .ui-icon:nth-child(3) {
  animation-delay: 0.32s;
}
@keyframes triple-flow {
  0%,
  65%,
  100% {
    opacity: 0.3;
    transform: translateX(0);
  }
  30% {
    opacity: 1;
    transform: translateX(12%);
  }
}
@media (prefers-reduced-motion: reduce) {
  .triple-arrow-track {
    transition: none;
  }
  .triple-arrow .ui-icon {
    animation: none;
    opacity: 0.8;
  }
}
</style>
