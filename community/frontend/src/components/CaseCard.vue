<script setup>
import CaseCover from "./CaseCover.vue";
import { detailUrl } from "../services/urls.js";
const props = defineProps({
  item: Object,
  mode: { type: String, default: "catalog" },
});
</script>
<template>
  <AppLink
    class="case-tile"
    :class="mode + '-case'"
    :href="detailUrl('case', item.id)"
    ><div class="cover-slot"><CaseCover :item="item" /></div>
    <div class="case-tile-content">
      <div class="case-tile-top">
        <UBadge variant="soft" color="primary" size="sm">{{
          item.category
        }}</UBadge
        ><span v-if="mode === 'catalog' && item.remote" class="case-access-text"
          ><Icon
            :name="item.access === '需申请权限' ? 'lock-keyhole' : 'globe'"
          />{{ item.access === "需申请权限" ? "需申请权限" : "可公开" }}</span
        ><UBadge
          v-else-if="item.product_type"
          color="neutral"
          variant="subtle"
          size="sm"
          >{{ item.product_type }}</UBadge
        >
      </div>
      <h3>{{ item.title }}</h3>
      <p v-if="item.summary" class="case-description">{{ item.summary }}</p>
      <div class="case-tile-meta">
        <UAvatar :alt="item.author || item.team || 'OWS'" size="2xs" /><span>{{
          item.source || "个人分享"
        }}</span
        ><TripleArrow compact  />
      </div></div
  ></AppLink>
</template>
