<script setup>
defineProps({ name: String });
</script>
<template>
  <svg
    class="ui-icon"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.75"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <use :href="'/assets/icons/lucide.svg#' + name" />
  </svg>
</template>
