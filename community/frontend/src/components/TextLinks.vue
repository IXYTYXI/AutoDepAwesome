<script setup>
import { computed } from "vue";
import { textLinks } from "../services/text-links.js";
import TripleArrow from "./TripleArrow.vue";
const props = defineProps({
  text: { type: String, default: "" },
  linkLabel: { type: String, default: "打开链接" },
});
const parts = computed(() => textLinks(props.text));
</script>
<template>
  <template v-for="(part, index) in parts" :key="index"
    ><a
      v-if="part.href"
      :href="part.href"
      target="_blank"
      rel="noopener noreferrer"
      class="text-link"
      >{{ linkLabel }}<TripleArrow compact /></a
    ><template v-else>{{ part.text }}</template></template
  >
</template>
<style scoped>
.text-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  vertical-align: baseline;
  color: var(--accent, #296fbb);
  text-decoration: underline;
  text-underline-offset: 3px;
  white-space: nowrap;
}
.text-link:focus-visible {
  outline: 2px solid currentColor;
  outline-offset: 3px;
}
</style>
