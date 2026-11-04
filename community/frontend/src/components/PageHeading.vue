<template>
  <h1 class="page-heading"><slot /></h1>
</template>

<style scoped>
h1.page-heading {
  margin: 0;
  font-family: var(--font-sans);
  font-size: 34px;
  font-weight: 650;
  line-height: 1.5;
  letter-spacing: -1px;
  color: #0f172a;
}
@media (max-width: 760px) {
  h1.page-heading {
    font-size: 30px;
  }
}
</style>
