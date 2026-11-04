import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
export function useCatalog(props) {
  const route = useRoute(),
    router = useRouter();
  const page = route.meta.page;
  const pageSize = page === "news" ? 7 : 6;
  const params = new URLSearchParams(route.query);
  const mine = computed(() => route.query.mine === "1");
  const category = ref(params.get("category") || "全部"),
    query = ref(params.get("q") || ""),
    number = ref(Math.max(1, parseInt(params.get("page")) || 1));
  const categories = computed(() =>
    page === "news"
      ? [
          "全部",
          ...new Set(
            props.state.news.map((item) => item.category).filter(Boolean),
          ),
        ]
      : [
          "全部",
          ...new Set(
            props.state.cases
              .filter((c) => (mine.value ? c.local : c.remote))
              .flatMap((c) => (c.tags?.length ? c.tags : [c.category]))
              .filter(Boolean),
          ),
        ],
  );
  watch(
    categories,
    (values) => {
      if (!values.includes(category.value)) category.value = "全部";
    },
    { immediate: true },
  );
  const matches = computed(() =>
    page === "news"
      ? props.state.news.filter(
          (c) => category.value === "全部" || c.category === category.value,
        )
      : props.state.cases.filter(
          (c) =>
            (mine.value ? c.local : c.remote) &&
            (category.value === "全部" ||
              (c.tags || [c.category]).includes(category.value)) &&
            `${c.title} ${c.summary} ${c.tags || ""} ${c.source} ${c.product_type || ""}`
              .toLowerCase()
              .includes(query.value.trim().toLowerCase()),
        ),
  );
  const visible = computed(() =>
    matches.value.slice((number.value - 1) * pageSize, number.value * pageSize),
  );
  watch(
    [matches, category, query, number],
    () => {
      if (!["cases", "news"].includes(page)) return;
      number.value = Math.min(
        number.value,
        Math.max(1, Math.ceil(matches.value.length / pageSize)),
      );
      const p = new URLSearchParams();
      if (category.value !== "全部") p.set("category", category.value);
      if (query.value) p.set("q", query.value);
      if (mine.value) p.set("mine", "1");
      if (number.value > 1) p.set("page", number.value);
      if (route.meta.page === page) {
        const next = route.path + (p.size ? "?" + p : "");
        if (next !== route.fullPath) router.replace(next);
      }
    },
    { immediate: true },
  );

  watch(
    () => route.query,
    (params) => {
      if (route.meta.page !== page) return;
      category.value = params.category || "全部";
      query.value = params.q || "";
      number.value = Math.max(1, parseInt(params.page) || 1);
    },
  );

  return {
    page,
    pageSize,
    mine,
    category,
    query,
    number,
    categories,
    matches,
    visible,
  };
}
