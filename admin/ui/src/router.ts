import { createRouter, createWebHistory } from 'vue-router'
import PlaceholderView from './views/PlaceholderView.vue'
import EgressView from './views/EgressView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/egress' },
    { path: '/egress', component: EgressView, meta: { title: 'Egress' } },
    { path: '/denylist', component: PlaceholderView, meta: { title: 'Denylist' } },
    { path: '/bottles', component: PlaceholderView, meta: { title: 'Bottles' } },
    { path: '/backup', component: PlaceholderView, meta: { title: 'Backup' } },
  ],
})
