<template>
  <section class="page" data-module="task">
    <header class="page-head">
      <div>
        <h2>采集任务管理</h2>
        <p class="page-desc">维护采集任务的调度周期、增量策略与执行参数；解析诊断产生的字段级修复待办在此闭环处理。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集任务</button>
        <button class="btn" type="button" @click="exportRows">导出采集任务清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">诊断修复待办（待修复 / 已修复）</span>
        <strong class="stat-value">{{ todoSummary['待修复'] ?? 0 }} / {{ todoSummary['已修复'] ?? 0 }}</strong>
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
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集任务数据，可先登记采集任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集任务记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <h3 class="section-title">诊断修复待办工作台</h3>
    <div class="workbench">
      <p class="muted" style="font-size: 12px; margin: 0 0 10px">
        字段级诊断失败后自动生成待办，诊断摘要同步到本清单；错误字段与转换前后值成对出现，修复后在解析模板页重跑该字段即可关闭待办。试运行诊断不会写入正式采集日志。
      </p>
      <table class="todo-table">
        <thead>
          <tr>
            <th>待办</th>
            <th>来源诊断</th>
            <th>解析模板</th>
            <th>关联任务</th>
            <th>失败字段 / 目标类型</th>
            <th>转换前值</th>
            <th>转换后值</th>
            <th>诊断摘要</th>
            <th>状态</th>
            <th>动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="todo in todos" :key="String(todo.id)">
            <td>#{{ todo.id }}</td>
            <td class="mono">{{ todo['诊断ID'] }}</td>
            <td>{{ todo['模板编号'] }}</td>
            <td>{{ todo['关联任务'] ?? '—' }}</td>
            <td><b>{{ todo['字段名'] }}</b><br /><span class="muted mono">{{ todo['目标类型'] }}</span></td>
            <td class="mono error-text">{{ displayValue(todo['转换前值']) }}</td>
            <td class="mono">{{ displayValue(todo['转换后值']) }}</td>
            <td style="max-width: 280px">{{ todo['诊断摘要'] }}</td>
            <td>
              <span class="badge" :class="todo['状态'] === '已修复' ? 'pass' : 'fail'">{{ todo['状态'] }}</span>
              <br /><span class="muted" style="font-size: 11px">{{ todo['创建时间'] }}</span>
            </td>
            <td class="row-actions">
              <button
                v-if="todo['状态'] === '待修复'"
                class="link"
                type="button"
                @click="runTodoAction(todo.id, '标记已修复')"
              >
                标记已修复
              </button>
              <button
                v-else
                class="link"
                type="button"
                @click="runTodoAction(todo.id, '重新打开')"
              >
                重新打开
              </button>
            </td>
          </tr>
          <tr v-if="!todos.length">
            <td colspan="10" class="empty-state">暂无修复待办，字段级诊断全部通过</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
interface TodoRow {
  id: number
  诊断ID: string
  模板编号: string
  关联任务: string | null
  字段名: string
  目标类型: string
  转换前值: unknown
  转换后值: unknown
  诊断摘要: string
  状态: string
  创建时间: string
}

const ENDPOINT = '/api/task'
const columns = ['任务编号', '任务名称', '关联源端', '采集频率', '增量字段', '起始水位', '批次大小', '执行窗口']
const actions = ['提交执行', '暂停任务', '恢复任务']
const stats = [{ label: '采集任务总数', value: 0 }, { label: '待执行任务', value: 0 }, { label: '执行中任务', value: 0 }]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const todos = ref<TodoRow[]>([])
const todoSummary = ref<Record<string, number>>({})

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '采集任务登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集任务动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('采集任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务列表读取失败'
  }
}

async function loadTodos() {
  try {
    const response = await request(`${ENDPOINT}/repair-todos`)
    if (!response.ok) {
      throw new Error('修复待办读取失败')
    }
    const payload = await response.json()
    todos.value = payload.items ?? []
    todoSummary.value = payload.summary ?? {}
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '修复待办读取失败'
  }
}

async function runTodoAction(todoId: number, action: string) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/repair-todos/${todoId}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message)
    }
    await loadTodos()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '修复待办操作失败'
  }
}

onMounted(async () => {
  await reload()
  await loadTodos()
})
</script>
