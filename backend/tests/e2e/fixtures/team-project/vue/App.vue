<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

type Status = 'open' | 'done'
type Ticket = { id: number, title: string, status: Status }

const tickets = ref<Ticket[]>([])
const title = ref<string>('')
const query = ref('')
const filter = ref<Status | ''>('')
const notice = ref('')
const visibleTickets = computed(() => tickets.value.filter(ticket =>
  (!filter.value || ticket.status === filter.value)
  && ticket.title.toLowerCase().includes(query.value.trim().toLowerCase()),
))

async function request(path: string, options?: RequestInit) {
  const response = await fetch(path, options)
  if (!response.ok) throw new Error(`工单服务返回 ${response.status}`)
  return response
}

async function load() {
  try {
    const response = await fetch('/api/tickets')
    if (!response.ok) throw new Error('无法读取工单')
    const body = await response.json()
    tickets.value = body.items
  } catch (error) {
    notice.value = String(error)
  }
}

async function create() {
  try {
    await request('/api/tickets', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title.value.trim() }) })
    title.value = ''
    notice.value = '工单已创建'
    await load()
  } catch (error) {
    notice.value = String(error)
  }
}

async function update(ticket: Ticket) {
  try {
    const status = ticket.status === 'open' ? 'done' : 'open'
    await request(`/api/tickets/${ticket.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }) })
    notice.value = '状态已更新'
    await load()
  } catch (error) {
    notice.value = String(error)
  }
}

async function remove(ticket: Ticket) {
  try {
    await request(`/api/tickets/${ticket.id}`, { method: 'DELETE' })
    notice.value = '工单已删除'
    await load()
  } catch (error) {
    notice.value = String(error)
  }
}

onMounted(load)
</script>

<template>
  <main>
    <header><span>运维工作台</span><h1>巡检工单台</h1><p>记录巡检问题，跟踪处理和闭环。</p></header>
    <form aria-label="新建工单" @submit.prevent="create">
      <label>工单标题<input id="ticket-title" v-model="title" name="title" maxlength="120" required></label>
      <button type="submit">创建工单</button>
    </form>
    <section aria-label="工单筛选">
      <label>搜索工单<input id="search" v-model="query" type="search"></label>
      <label>状态<select id="filter" v-model="filter"><option value="">全部</option><option value="open">待处理</option><option value="done">已完成</option></select></label>
    </section>
    <p role="status" id="notice">{{ notice }}</p>
    <ul id="tickets" aria-label="工单列表">
      <li v-for="ticket in visibleTickets" :key="ticket.id">
        <strong>{{ ticket.title }}</strong><span>{{ ticket.status === 'open' ? '待处理' : '已完成' }}</span>
        <button type="button" @click="update(ticket)">{{ ticket.status === 'open' ? '完成' : '重开' }}</button>
        <button type="button" @click="remove(ticket)">删除</button>
      </li>
    </ul>
  </main>
</template>
