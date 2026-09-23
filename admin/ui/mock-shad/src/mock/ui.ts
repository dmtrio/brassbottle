import { ref } from 'vue'
import { useMediaQuery } from '@vueuse/core'

export const notifyEnabled = ref(false)
export const live = ref<'live' | 'reconnecting' | 'polling'>('live')
export const usePhone = () => useMediaQuery('(max-width: 640px)')
