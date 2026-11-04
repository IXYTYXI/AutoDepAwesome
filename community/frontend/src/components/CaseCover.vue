<script setup>
import { computed, ref, watch } from 'vue';
import { preview } from '../services/covers.js';
import { caseImageUrl } from '../services/urls.js';
const props = defineProps({ item: Object, variant: { type: String, default: 'catalog' } });
const source = computed(() => caseImageUrl(props.item));
const failed = ref(false);
watch(source, () => { failed.value = false; });
const fallback = computed(() => preview({ ...props.item, image: null }));
</script>
<template>
  <div class="case-media" :class="['case-media-' + variant, { 'has-cover': source && !failed }]">
    <img v-if="source && !failed" :key="source" :src="source" :alt="item.title + '封面'"
      :loading="variant === 'detail' ? 'eager' : 'lazy'" decoding="async" @error="failed = true" />
    <slot v-else><div class="case-media-fallback" v-html="fallback"></div></slot>
  </div>
</template>
<style>
.case-media { position:relative; width:100%; min-width:0; }
.case-media.has-cover { overflow:hidden; background:#e9eff6; }
.case-media-catalog.has-cover { aspect-ratio:16 / 9; }
.case-media.has-cover>img { display:block; width:100%; height:100%; object-fit:contain; }
.case-media-detail.has-cover { aspect-ratio:16 / 9; max-height:520px; }
.case-media-fallback { width:100%; }
</style>
