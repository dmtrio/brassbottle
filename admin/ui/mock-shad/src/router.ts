import { createRouter, createWebHashHistory } from 'vue-router'
import EgressView from './views/EgressView.vue'
import PlaceholderView from './views/PlaceholderView.vue'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/egress' },
    { path: '/egress', component: EgressView, meta: { title: 'Egress' } },
    { path: '/denylist', component: PlaceholderView, meta: { title: 'Denylist' } },
    { path: '/bottles', component: PlaceholderView, meta: { title: 'Bottles' } },
    { path: '/backup', component: PlaceholderView, meta: { title: 'Backup' } },
  ],
})
