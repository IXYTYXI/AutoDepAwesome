<script setup>
import PageHeading from "../components/PageHeading.vue";
import PageBreadcrumb from "../components/PageBreadcrumb.vue";
import { useCatalog } from "../composables/useCatalog.js";
import FilterResults from "../components/FilterResults.vue";
import CaseCard from "../components/CaseCard.vue";
import CaseStatus from "../components/CaseStatus.vue";
import Pagination from "../components/Pagination.vue";
import { detailUrl, imageUrl } from "../services/urls.js";
const props = defineProps({ state: Object, user: Object });
const emit = defineEmits(["compose", "retry"]);
const { page, mine, category, query, number, categories, matches, visible } =
  useCatalog(props);
</script>
<template>
  <main class="portal-page wrap" id="main-content">
    <PageBreadcrumb current="案例库" />
    <div class="page-title">
      <div>
        <PageHeading>案例库<span class="case-title-dot">.</span></PageHeading>
        <span
          >{{ matches.length }}
          {{
            mine ? "个本机分享" : page === "cases" ? "个案例" : "条资讯"
          }}</span
        >
      </div>
      <AppLink
        class="quiet-link"
        :href="mine ? 'cases.html' : 'cases.html?mine=1'"
        >{{ mine ? "全部案例" : "我的分享" }} <TripleArrow compact
      /></AppLink>
    </div>
    <div class="catalog-tools">
      <div class="nuxt-filters" aria-label="案例标签筛选">
        <UButton
          color="neutral"
          :variant="category === value ? 'soft' : 'ghost'"
          size="sm"
          v-for="value in categories"
          :key="value"
          :class="{ selected: category === value }"
          :aria-pressed="category === value"
          @click="
            category = value;
            number = 1;
          "
        >
          {{ value }}
        </UButton>
      </div>
      <UInput
        class="catalog-search"
        id="search"
        v-model="query"
        @update:model-value="number = 1"
        type="search"
        size="lg"
        aria-label="搜索实践案例"
        placeholder="搜索案例"
        ><template #leading><Icon name="search" /></template
      ></UInput>
    </div>
    <CaseStatus :state="mine ? {} : state" @retry="emit('retry')" />
    <FilterResults :filter-key="`${category}:${query}:${number}:${mine}`">
      <div id="catalog-results" aria-live="polite">
        <CaseCard v-for="item in visible" :key="item.id" :item="item" />
        <div v-if="!matches.length && !state.casesError" class="catalog-empty">
          <h2>{{ mine ? "暂无本机分享" : "没有匹配的案例" }}</h2>
          <p>请更换关键词或分类。</p>
        </div>
      </div>
    </FilterResults>
    <Pagination :count="matches.length" v-model="number" />
  </main>
</template>
