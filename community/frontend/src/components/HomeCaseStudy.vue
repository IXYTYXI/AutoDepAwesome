<script setup>
import { computed } from "vue";
import CaseCover from "./CaseCover.vue";
import { detailUrl } from "../services/urls.js";
const props = defineProps({ item: Object, index: Number });
const kind = computed(() => ({ 'Web App': 'app', 'AI Agent': 'agent', Skill: 'skill' })[props.item.product_type] || 'app');
</script>

<template>
  <AppLink class="story" :class="[{ 'story-featured': index === 0 }, 'story-' + kind]" :href="detailUrl('case', item.id)" :aria-label="'查看案例：' + item.title">
    <div class="story-shell">
      <CaseCover :item="item" variant="story-background" aria-hidden="true">
        <div class="story-placeholder"><i></i><i></i><i></i></div>
      </CaseCover>
      <div class="story-copy">
        <div class="story-team"><span></span>{{ item.team || 'OWS Community' }}</div>
        <div class="story-main">
          <p v-if="item.category" class="story-category">{{ item.category }}</p>
          <h3>{{ item.title }}</h3>
          <p v-if="item.summary" class="story-summary">{{ item.summary }}</p>
        </div>
        <div class="story-bottom">
          <div class="story-credit"><span v-if="item.author"><em>贡献者</em>{{ item.author }}</span><small v-if="item.access === '需申请权限'">原文需申请权限</small></div>
          <TripleArrow class="story-forward" />
        </div>
      </div>
    </div>
  </AppLink>
</template>
