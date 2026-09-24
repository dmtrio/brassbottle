import { computed, reactive, ref, watch, type Ref } from 'vue'
import { fetchRecent } from '@/api/egress'
import type { HistoryRow } from '@/types'

export const HISTORY_PAGE_SIZE = 50

export type HistoryFilters = {
  container: string | null
  since: string | null
  until: string | null
}

// One keyset-paged walk over every decided row in the store. `cursors[i]` is
// the `before` value that loads page i (null for the first page), so Older
// pushes the broker's `next` and Newer pops back to a cursor already used. A
// page becomes current only when its fetch succeeds, so a failed Older leaves
// the list and the page number as they were.
export function useHistory(filters: Ref<HistoryFilters>) {
  const rows = ref<HistoryRow[]>([])
  const next = ref<string | null>(null)
  const cursors = ref<(string | null)[]>([null])
  const loading = ref(false)
  const loaded = ref(false)
  const error = ref<string | null>(null)
  // Every bottle seen on any page, so the bottle filter still offers the
  // others while one is selected.
  const bottles = reactive(new Set<string>())
  let seq = 0
  // The page the last request was for, so Retry repeats a failed Older or
  // Newer rather than reloading the page already showing.
  let attempted: (string | null)[] = [null]

  async function go(target: (string | null)[]): Promise<void> {
    const mine = ++seq
    attempted = target
    loading.value = true
    const result = await fetchRecent({
      before: target[target.length - 1],
      limit: HISTORY_PAGE_SIZE,
      ...filters.value,
    })
    if (mine !== seq) return // a newer request superseded this one
    loading.value = false
    if (!result.ok) {
      error.value = result.error
      return
    }
    error.value = null
    loaded.value = true
    cursors.value = target
    rows.value = result.data.rows
    next.value = result.data.next
    for (const row of result.data.rows) bottles.add(row.container)
  }

  // A new filter is a new walk: nothing of the old one may stay on screen, or a
  // failed fetch would leave the previous filter's rows, and an Older cursor
  // that belongs to it, under the new filter.
  watch(
    filters,
    () => {
      rows.value = []
      next.value = null
      loaded.value = false
      cursors.value = [null]
      void go([null])
    },
    { deep: true },
  )

  return {
    rows,
    next,
    loading,
    loaded,
    error,
    bottles: computed(() => [...bottles].sort()),
    pageNumber: computed(() => cursors.value.length),
    load: () => go([null]),
    retry: () => go(attempted),
    older: () => {
      if (next.value && !loading.value) void go([...cursors.value, next.value])
    },
    newer: () => {
      if (cursors.value.length > 1 && !loading.value) void go(cursors.value.slice(0, -1))
    },
  }
}
