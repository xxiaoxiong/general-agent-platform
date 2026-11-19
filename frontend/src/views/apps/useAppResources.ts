import { ref } from 'vue'
import { knowledgeApi, modelApi, workflowApi } from '@/api'
import type { KnowledgeBase, ModelConfig, Workflow } from '@/types'

export interface AppBindings {
  model_config_id: number | null
  knowledge_base_id: number | null
  workflow_id: number | null
}

export function useAppResources() {
  const models = ref<ModelConfig[]>([])
  const bases = ref<KnowledgeBase[]>([])
  const workflows = ref<Workflow[]>([])
  const loading = ref(false)
  const failed = ref(false)
  let loaded = false

  async function load() {
    if (loaded || loading.value) return
    loading.value = true
    try {
      const [modelResult, baseResult, workflowResult] = await Promise.allSettled([
        modelApi.listConfigs({ model_type: 'chat' }),
        knowledgeApi.listBases({ page_size: 100 }),
        workflowApi.list({ page_size: 100 }),
      ])
      if (modelResult.status === 'fulfilled') models.value = modelResult.value.filter(model => model.is_active)
      if (baseResult.status === 'fulfilled') bases.value = baseResult.value.items
      if (workflowResult.status === 'fulfilled') workflows.value = workflowResult.value.items
      failed.value = [modelResult, baseResult, workflowResult].some(result => result.status === 'rejected')
      loaded = !failed.value
    } finally {
      loading.value = false
    }
  }

  return { models, bases, workflows, loading, failed, load }
}
