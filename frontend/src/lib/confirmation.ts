import { TriangleAlert } from '@lucide/vue'
import { ElMessageBox } from 'element-plus'
import { markRaw } from 'vue'

export function confirmDestructive(
  message: string,
  title: string,
  { confirmButtonText = '删除', cancelButtonText = '取消' } = {},
) {
  return ElMessageBox.confirm(message, title, {
    type: 'error',
    icon: markRaw(TriangleAlert),
    confirmButtonType: 'danger',
    confirmButtonText,
    cancelButtonText,
    autofocus: false,
    closeOnClickModal: false,
  })
}
