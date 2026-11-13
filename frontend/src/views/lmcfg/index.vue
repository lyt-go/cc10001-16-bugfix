<template>
  <section class="page" data-module="lmcfg">
    <header class="page-head">
      <div>
        <h2>登录态管理管理</h2>
        <p class="page-desc">管理需要登录态的采集目标站点的认证凭证，维护Cookie有效期与自动刷新。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记登录凭证</button>
        <button class="btn" type="button" @click="exportRows">导出登录态管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>最近验证成功时间</th>
          <th>原因 / 阻断说明</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td :class="['status-cell', statusClass(row)]">{{ row.status ?? '—' }}</td>
          <td>{{ row['最近验证成功时间'] ?? '—' }}</td>
          <td class="reason-cell">{{ row['失败原因'] || row['失效原因'] || '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in rowActions(row)"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="!rowActions(row).length" class="muted">—</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无登录态管理数据，可先登记登录凭证</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条登录态管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/lmcfg'
const columns = ["凭证编号", "目标站点", "认证方式", "Cookie内容", "有效期至", "刷新策略", "最近验证时间", "绑定代理"]
// 只展示状态机允许的动作；过期的凭证必须先重新提交验证，不能直接点成有效。
const statusActions: Record<string, string[]> = {
  草稿: ["提交验证"],
  验证中: ["标记有效", "验证失败", "标记过期"],
  有效: ["提交验证", "标记过期"],
  已过期: ["提交验证"],
}
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const stats = reactive([
  { label: '登录凭证总数', value: 0 },
  { label: '有效凭证数', value: 0 },
  { label: '已过期凭证数', value: 0 },
])

function rowActions(row: Row): string[] {
  const served = row['可执行动作']
  if (Array.isArray(served)) {
    return served.map(String)
  }
  return statusActions[String(row.status ?? '')] ?? []
}

function statusClass(row: Row) {
  const status = String(row.status ?? '')
  return status === '有效' ? 'status-ok' : status === '已过期' ? 'status-bad' : 'status-wait'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '登录凭证登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  const values: Record<string, unknown> = { action }
  if (action === '验证失败' || action === '标记过期') {
    const reason = window.prompt('请填写失败原因（可选，失败结果会保留原因）', '')
    if (reason) {
      values['失败原因'] = reason
    }
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message || '登录态管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登录态管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('登录凭证列表读取失败')
    }
    const payload = await response.json()
    const items: Row[] = payload.items ?? []
    rows.value = items
    total.value = payload.total ?? items.length
    stats[0].value = total.value
    stats[1].value = items.filter((row) => String(row.status) === '有效').length
    stats[2].value = items.filter((row) => String(row.status) === '已过期').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登录态管理列表读取失败'
  }
}

onMounted(reload)
</script>
