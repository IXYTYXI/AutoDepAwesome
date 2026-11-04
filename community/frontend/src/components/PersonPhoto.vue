<script setup>
import { ref, watch } from "vue";
const props = defineProps({ person: Object, eager: Boolean });
const failed = ref(false);
watch(
  () => props.person.image,
  () => (failed.value = false),
);
</script>
<template>
  <div class="person-photo" :class="{ failed }">
    <span class="person-initial">{{ person.name.slice(0, 1) }}</span
    ><img
      v-if="person.image"
      :src="person.image"
      :alt="person.name"
      :loading="eager ? 'eager' : 'lazy'"
      decoding="async"
      @error="failed = true"
    />
  </div>
</template>
