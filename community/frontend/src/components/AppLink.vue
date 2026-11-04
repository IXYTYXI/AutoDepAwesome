<script setup>
import { computed } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { internalDestination } from '../services/navigation.js';
const props = defineProps({ href: String, target: String });
const route = useRoute();
const router = useRouter();
const destination = computed(() => internalDestination(props.href, route.fullPath, location.origin));
const internal = computed(() => destination.value && router.resolve(destination.value).matched.length);
</script>
<template>
  <RouterLink v-if="internal" :to="destination" :target="target"><slot /></RouterLink>
  <a v-else :href="href" :target="target"><slot /></a>
</template>
