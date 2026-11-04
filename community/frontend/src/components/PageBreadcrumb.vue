<script setup>
import AppLink from "./AppLink.vue";
import Icon from "./Icon.vue";
import TripleArrow from "./TripleArrow.vue";
defineProps({
  current: { type: String, required: true },
  parents: { type: Array, default: () => [] },
});
</script>
<template>
  <nav class="page-crumb" aria-label="面包屑">
    <ol>
      <li>
        <AppLink href="index.html"
          ><Icon name="house" /><span>首页</span></AppLink
        >
      </li>
      <li v-for="parent in parents" :key="parent.href">
        <TripleArrow compact class="crumb-separator" />
        <AppLink :href="parent.href">{{ parent.label }}</AppLink>
      </li>
      <li>
        <TripleArrow compact class="crumb-separator" /><span
          class="crumb-current"
          aria-current="page"
          >{{ current }}</span
        >
      </li>
    </ol>
  </nav>
</template>
<style scoped>
.page-crumb {
  display: flex;
  width: max-content;
  max-width: 100%;
  margin: 0 0 28px;
  justify-content: flex-start;
  align-self: flex-start;
  justify-self: start;
  padding: 8px 14px 8px 8px;
  background: #f8fbff;
  border: 1px solid #fff;
  border-radius: 14px;
  box-shadow: 0 2px 8px #34527808;
  font-size: 12px;
  color: #60758f;
}
.page-crumb ol {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 13px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.page-crumb li {
  display: inline-flex;
  align-items: center;
  gap: 13px;
  min-width: 0;
}
.page-crumb a {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 9px;
  border-radius: 8px;
  transition:
    background 0.18s,
    color 0.18s;
}
.page-crumb a:hover {
  background: #e5efff;
  color: #235dcb;
}
.page-crumb a:focus-visible {
  outline: 2px solid #5389d6;
  outline-offset: 2px;
}
.page-crumb a > .ui-icon {
  width: 14px;
  height: 14px;
}
.crumb-separator {
  width: 14px;
  height: 14px;
  color: #9aaec6;
}
.crumb-current {
  color: #2e5a91;
  font-weight: 600;
}
@media (max-width: 760px) {
  .page-crumb {
    font-size: 11px;
    padding: 5px 12px 5px 5px;
    margin-bottom: 24px;
  }
  .page-crumb ol,
  .page-crumb li {
    gap: 8px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .page-crumb a {
    transition: none;
  }
}
</style>
