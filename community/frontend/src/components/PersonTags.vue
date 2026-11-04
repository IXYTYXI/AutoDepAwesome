<script setup>
defineProps({ tags: Array });
function tone(tag) {
  return (
    [
      [/产品/, "blue"],
      [/技术/, "violet"],
      [/PMO/i, "amber"],
      [/策划|运营/, "peach"],
      [/组织/, "sage"],
      [/业务|专家/, "rose"],
      [/实施|数字员工/, "aqua"],
    ].find(([p]) => p.test(tag.normalize("NFKC").trim()))?.[1] || "neutral"
  );
}
</script>
<template>
  <div class="person-tags">
    <span
      v-for="tag in tags"
      :key="tag"
      class="person-tag"
      :data-tone="tone(tag)"
      >{{ tag.replace(/AI\s*官/g, "AI 领航团") }}</span
    >
  </div>
</template>
