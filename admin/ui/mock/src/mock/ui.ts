import { onBeforeUnmount, ref } from 'vue'

export function useMedia(query: string) {
  const mq = window.matchMedia(query)
  const matches = ref(mq.matches)
  const on = (e: MediaQueryListEvent) => { matches.value = e.matches }
  mq.addEventListener('change', on)
  onBeforeUnmount(() => mq.removeEventListener('change', on))
  return matches
}

export const notifyEnabled = ref(false)
export const live = ref<'live' | 'reconnecting' | 'polling'>('live')
