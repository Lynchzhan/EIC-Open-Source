// Copyright (c) 2026 Le7
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { installControls } from './app/ui/controls'
import App from './App.vue'
import router from './router'
import './shared/styles/tokens.css'
import './app/styles/v4-theme.css'
import './app/styles/controls.css'
import './app/styles/management.css'
import ResizeObserver from 'resize-observer-polyfill'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'

dayjs.locale('zh-cn')
window.ResizeObserver = ResizeObserver

const app = createApp(App)
app.use(createPinia())
installControls(app)
app.use(router)
app.mount('#app')
