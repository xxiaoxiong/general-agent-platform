/**
 * 对话流粘底跟随 + 提问锚点导航：
 * - 任务开始 / 发送 / 切换会话：回到对话末端并持续跟随最新内容（无需手动下拉）；
 * - 用户上翻查看历史：停止跟随（绝不强行跳底）；
 * - 用户滚回底部：自动恢复跟随。
 * 旧实现按"距底 <160px"判定跟随——一次大段渲染（批量增量/工具块插入）就会
 * 超出阈值导致从此不再自动滚动。粘底标志表达的是"用户意图"，与内容涨幅无关。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { hasScrolledAway, isNearLatest } from './interactions'
import { questionPreview } from './chatDisplay'
import type { WorkspaceState } from './chatState'

export function useThreadScroll(st: WorkspaceState) {
  const messagesRef = ref<HTMLElement>()
  const threadRef = ref<HTMLElement>()
  const stickToBottom = ref(true)
  const showJumpToLatest = ref(false)
  let messageResize: ResizeObserver | undefined
  /** 最近一次钉底时的 scrollTop：内容撑开不改变 scrollTop，用户上翻才会 */
  let lastPinnedTop = -1

  function onMessagesScroll() {
    const el = messagesRef.value
    if (!el) return
    stickToBottom.value = isNearLatest(el)
    showJumpToLatest.value = !stickToBottom.value
  }

  /** 滚轮上翻 = 明确的"离开底部"意图，同步解除粘底（scroll 事件派发晚于微任务，存在竞态） */
  function onWheelScroll(e: WheelEvent) {
    if (e.deltaY < 0) stickToBottom.value = false
  }

  /** 跟随最新内容（仅粘底状态下生效，不打断阅读历史） */
  async function scrollToBottom() {
    await nextTick()
    const el = messagesRef.value
    if (!el || !stickToBottom.value) return
    // 粘底判定必须放在滚动前一刻：若在 nextTick 之前检查，上翻瞬间在途的
    // 跟随调用会把用户强行拽回底部（并经 scroll 事件把粘底标志重新武装）
    // 双保险：scrollTop 被拽离最近钉底位置 = 用户上翻（内容撑开不会改 scrollTop）
    if (hasScrolledAway(el, lastPinnedTop)) {
      stickToBottom.value = false
      showJumpToLatest.value = true
      return
    }
    el.scrollTop = el.scrollHeight
    lastPinnedTop = el.scrollTop
  }

  /** 强制回到底部并恢复跟随（切换会话/发送消息/任务开始等明确意图） */
  async function forceScrollToBottom() {
    stickToBottom.value = true
    showJumpToLatest.value = false
    await nextTick()
    const el = messagesRef.value
    if (el) {
      el.scrollTop = el.scrollHeight
      lastPinnedTop = el.scrollTop
    }
  }

  async function focusLinkedRun() {
    const id = st.route.query.run
    if (typeof id !== 'string') return false
    await nextTick()
    const target = Array.from(threadRef.value?.querySelectorAll<HTMLElement>('[data-run-id]') || [])
      .find((message) => message.dataset.runId === id)
    const viewport = messagesRef.value
    if (!target || !viewport) return false
    stickToBottom.value = false
    viewport.scrollTop += target.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 12
    return true
  }

  // ── 提问锚点导航（右侧纵向圆点：点击跳转对应提问，悬浮气泡预览内容）────────

  const questionAnchors = computed(() =>
    st.messages.value
      .map((m, idx) => ({ m, idx }))
      .filter(({ m }) => m.role === 'user')
      .map(({ m, idx }) => ({ idx, preview: questionPreview(m.content) || '附件消息' })),
  )

  function jumpToQuestion(idx: number) {
    const el = messagesRef.value
    const target = el?.querySelector<HTMLElement>(`#q-${idx}`)
    if (!el || !target) return
    // 跳到历史提问 = 阅读意图：解除粘底，避免流式更新把视图拽回底部
    stickToBottom.value = false
    // 直接赋值定位：长会话（数万 px）下元素级 scrollTo({behavior:'smooth'})
    // 不产生滚动（实测无效），瞬时跳转对长距离导航也更利落
    el.scrollTop =
      el.scrollTop + target.getBoundingClientRect().top - el.getBoundingClientRect().top - 12
  }

  function startResizeObserver() {
    messageResize = new ResizeObserver(() => { void scrollToBottom() })
    if (messagesRef.value) messageResize.observe(messagesRef.value)
    watch(threadRef, (thread, previous) => {
      if (previous) messageResize?.unobserve(previous)
      if (thread) messageResize?.observe(thread)
    })
  }

  function stopResizeObserver() {
    messageResize?.disconnect()
  }

  /** 复位粘底状态（切换会话时） */
  function resetFollow() {
    showJumpToLatest.value = false
  }

  return {
    messagesRef, threadRef,
    stickToBottom, showJumpToLatest, questionAnchors,
    onMessagesScroll, onWheelScroll, scrollToBottom, forceScrollToBottom,
    focusLinkedRun, jumpToQuestion, resetFollow,
    startResizeObserver, stopResizeObserver,
  }
}
