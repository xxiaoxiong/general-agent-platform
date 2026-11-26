import { createApp } from 'vue'
import '../src/styles/global.css'
import Harness from './Harness.vue'
import { initializeTheme } from '../src/lib/theme'

initializeTheme()
createApp(Harness).mount('#app')
