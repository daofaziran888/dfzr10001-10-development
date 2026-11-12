<template>
  <section class="page" data-module="parser">
    <header class="page-head">
      <div>
        <h2>数据解析模板管理</h2>
        <p class="page-desc">维护采集结果的字段映射、类型转换与校验规则模板，实现标准化解析输出；支持字段级试解析诊断与单字段修复重跑。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记解析模板</button>
        <button class="btn" type="button" @click="exportRows">导出数据解析模板清单</button>
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
          <th>可执行动作</th>
          <th>诊断</th>
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
          <td>
            <button class="link diag-link" type="button" @click="selectTemplate(row)">试解析诊断</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无数据解析模板数据，可先登记解析模板</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据解析模板记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <h3 class="section-title">字段级诊断工作台</h3>

    <div class="workbench">
      <h4>提交样本试解析</h4>
      <form class="form-grid" @submit.prevent="submitDiagnosis('试运行')">
        <label>
          <span>解析模板</span>
          <select v-model.number="selectedTemplateId" @change="prefillSample">
            <option v-for="row in rows" :key="String(row.id)" :value="Number(row.id)">
              {{ row['模板编号'] }}（{{ row['模板版本'] }}）
            </option>
          </select>
        </label>
        <label>
          <span>运行模式（试运行不写正式采集日志）</span>
          <select v-model="runMode">
            <option>试运行</option>
            <option>正式采集</option>
          </select>
        </label>
        <label>
          <span>关联任务编号（用于生成修复待办）</span>
          <input v-model="relatedTask" placeholder="如 TASK-0002" />
        </label>
        <label style="flex: 1; min-width: 320px">
          <span>样本原文（按模板输入格式填写 CSV / JSON）</span>
          <textarea v-model="sampleText" placeholder="粘贴样本内容"></textarea>
        </label>
        <div style="display: flex; gap: 8px">
          <button class="btn primary" type="submit">试运行诊断</button>
          <button class="btn" type="button" @click="submitDiagnosis('正式采集')">正式采集诊断</button>
          <button class="btn ghost" type="button" @click="prefillSample">填入示例样本</button>
        </div>
      </form>
      <p v-if="diagnosisMessage" :class="diagnosisOk ? 'success-text' : 'error-text'" style="font-size: 12px; margin: 8px 0 0">
        {{ diagnosisMessage }}
      </p>
    </div>

    <div class="workbench">
      <h3 style="margin-bottom: 4px">诊断概览（按模板版本排列）</h3>
      <p class="muted" style="font-size: 12px; margin: 0 0 10px">
        历史诊断按当时的解析模板版本留档；快照版本与当前启用版本冲突时保留原版本解释并打冲突标记；同一版本连续提交仅保留首个诊断。
      </p>
      <details v-for="group in overviewGroups" :key="group['模板版本']" class="version-group" open>
        <summary>
          <span>模板版本 {{ group['模板版本'] }}</span>
          <span class="badge neutral">诊断 {{ group['诊断条数'] }} 条</span>
          <span class="badge fail" v-if="group['累计失败字段']">累计失败字段 {{ group['累计失败字段'] }}</span>
          <span class="badge pass" v-else>无失败字段</span>
          <span class="badge conflict" v-if="group['存在版本冲突']">存在版本冲突（保留原版本解释）</span>
        </summary>
        <div v-for="item in group['诊断列表']" :key="item.id" style="padding: 6px 14px 12px">
          <div class="diag-card">
            <div class="diag-card-head">
              <span class="diag-id">{{ item.id }}</span>
              <span>{{ item['模板编号'] }}</span>
              <span class="badge neutral">{{ item['运行模式'] }}</span>
              <span>快照版本 <b>{{ item['模板版本快照'] }}</b></span>
              <span>当前启用版本 {{ item['当前启用版本'] ?? '—' }}</span>
              <span class="badge conflict" v-if="item['版本冲突']">版本冲突</span>
              <span class="badge" :class="resultClass(item['整体结果'])">{{ item['整体结果'] }}</span>
              <span class="muted">{{ item['创建时间'] }}</span>
              <button class="link" type="button" @click="toggleDetail(item.id)">
                {{ openDiagnosisId === item.id ? '收起字段详情' : '查看字段级详情' }}
              </button>
            </div>
            <div class="diag-summary">诊断摘要：{{ item['摘要'] }}</div>

            <template v-if="openDiagnosisId === item.id">
              <diagnosis-detail
                v-if="detailMap[item.id]"
                :record="detailMap[item.id]"
                @rerun="onRerun"
              />
              <p v-else class="muted" style="padding: 10px 12px; font-size: 12px">字段详情加载中…</p>
            </template>
          </div>
        </div>
      </details>
      <p v-if="!overviewGroups.length" class="muted" style="font-size: 13px">暂无诊断记录，可先在上方提交样本试解析。</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { defineComponent, h, onMounted, ref, type PropType } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface FieldResult {
  字段名: string
  源字段: string
  目标类型: string
  必填: boolean
  状态: string
  映射: { 源字段: string; 找到: boolean; 转换前值: unknown }
  转换: { 通过: boolean; 转换前值: unknown; 转换后值: unknown; 目标类型: string; 错误信息: string | null }
  默认值: { 来源: string; 默认值: unknown; 是否生效: boolean; 生效值: unknown; 说明: string }
  校验: { 通过: boolean; 规则: string; 范围: string; 标准: string; 判定阈值: string; 正则: string | null; 说明: string }
}

interface DiagnosisRecord {
  id: string
  模板编号: string
  运行模式: string
  输入格式: string
  输出格式: string
  模板版本快照: string
  当前启用版本: string | null
  版本冲突: boolean
  创建时间: string
  整体结果: string
  失败字段数: number
  摘要: string
  样本原文: string
  字段结果: FieldResult[]
}

interface OverviewItem {
  id: string
  模板编号: string
  运行模式: string
  模板版本快照: string
  当前启用版本: string | null
  版本冲突: boolean
  整体结果: string
  失败字段数: number
  摘要: string
  创建时间: string
}

interface OverviewGroup {
  模板版本: string
  诊断条数: number
  累计失败字段: number
  存在版本冲突: boolean
  诊断列表: OverviewItem[]
}

const ENDPOINT = '/api/parser'
const columns = ['模板编号', '输入格式', '字段映射', '类型转换', '默认值', '校验正则', '输出格式', '模板版本']
const actions = ['提交校验', '启用配置', '停用配置']
const stats = [{ label: '解析模板总数', value: 0 }, { label: '待校验解析模板', value: 0 }, { label: '已启用解析模板', value: 0 }]

const EXAMPLE_SAMPLES: Record<string, string> = {
  'PARS-0001': 'user_id,user_name,email,signup_date\n100861,,bad-email,2026-01-05',
  'PARS-0002': 'order_id,amount,order_date,status,remark\nORD-1001,88.5,2026-13-40,已支付,',
  'PARS-0003': '{"item_id":"ITEM-1","price":9.9,"currency":"CNY","in_stock":"是","updated_at":"2026-10-11"}',
}

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const selectedTemplateId = ref<number | null>(null)
const runMode = ref('试运行')
const relatedTask = ref('TASK-0002')
const sampleText = ref('')
const diagnosisMessage = ref('')
const diagnosisOk = ref(true)

const overviewGroups = ref<OverviewGroup[]>([])
const openDiagnosisId = ref<string | null>(null)
const detailMap = ref<Record<string, DiagnosisRecord>>({})
const rerunInputs = ref<Record<string, string>>({})

function resultClass(result: unknown): string {
  if (result === '通过') return 'pass'
  if (result === '警告') return 'warn'
  return 'fail'
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

/* 字段详情子组件：逐字段呈现映射、转换前后值、默认值来源与校验结果，失败后可从该字段继续调整 */
const DiagnosisDetail = defineComponent({
  name: 'DiagnosisDetail',
  props: { record: { type: Object as PropType<DiagnosisRecord>, required: true } },
  emits: ['rerun'],
  setup(props, { emit }) {
    return () =>
      h('div', [
        h('div', { class: 'spec-strip' }, [
          h('span', `输入格式：${props.record.输入格式}`),
          h('span', `字段映射：${props.record.字段结果.length} 个字段`),
          h('span', `输出格式：${props.record.输出格式}`),
          h('span', `模板版本：${props.record.模板版本快照}（留档快照）`),
        ]),
        h('div', { class: 'spec-strip' }, [
          h('span', { class: 'muted' }, '样本原文：'),
          h('pre', { class: 'mono', style: 'margin:0;white-space:pre-wrap;flex:1' }, props.record.样本原文),
        ]),
        h('table', { class: 'field-table' }, [
          h('thead', null, h('tr', null, [
            h('th', '字段/源字段'),
            h('th', '目标类型'),
            h('th', '字段映射（转换前值）'),
            h('th', '类型转换（转换后值）'),
            h('th', '默认值来源'),
            h('th', '校验规则 / 范围 / 标准 / 判定阈值'),
            h('th', '状态'),
            h('th', '单字段重跑'),
          ])),
          h('tbody', null, props.record.字段结果.map((field) =>
            h('tr', { key: field.字段名 }, [
              h('td', null, [
                h('div', { class: 'cell-stack' }, [
                  h('b', field.字段名),
                  h('span', { class: 'kv mono' }, `源：${field.源字段}${field.必填 ? '（必填）' : '（可选）'}`),
                ]),
              ]),
              h('td', { class: 'mono' }, field.目标类型),
              h('td', null, h('span', { class: 'mono' }, displayValue(field.映射.转换前值))),
              h('td', null, h('div', { class: 'cell-stack' }, [
                h('span', { class: 'mono' }, displayValue(field.转换.转换后值)),
                h('span', { class: field.转换.通过 ? 'success-text' : 'error-text' },
                  field.转换.错误信息 ?? '转换通过'),
              ])),
              h('td', null, h('div', { class: 'cell-stack' }, [
                h('span', `来源：${field.默认值.来源}`),
                h('span', { class: 'kv mono' }, `默认值：${displayValue(field.默认值.默认值)}`),
                h('span', { class: 'kv' }, field.默认值.说明),
              ])),
              h('td', null, h('div', { class: 'cell-stack' }, [
                h('span', `规则：${field.校验.规则}`),
                h('span', { class: 'kv' }, `范围：${field.校验.范围}`),
                h('span', { class: 'kv' }, `标准：${field.校验.标准}`),
                h('span', { class: 'kv mono' }, `阈值：${field.校验.判定阈值}`),
                h('span', field.校验.正则 ? { class: 'kv mono' } : { class: 'kv' },
                  field.校验.正则 ? `正则：${field.校验.正则}` : '正则：未配置'),
                h('span', { class: field.校验.通过 ? 'success-text' : 'error-text' }, field.校验.说明),
              ])),
              h('td', h('span', { class: `badge ${resultClass(field.状态)}` }, field.状态)),
              h('td', null, h('div', { class: 'rerun-row' }, [
                h('input', {
                  value: rerunInputs.value[`${props.record.id}:${field.字段名}`] ?? displayValue(field.映射.转换前值),
                  placeholder: '修正后的值',
                  onInput: (event: Event) => {
                    rerunInputs.value[`${props.record.id}:${field.字段名}`] = (event.target as HTMLInputElement).value
                  },
                }),
                h('button', {
                  class: 'link',
                  type: 'button',
                  onClick: () => {
                    const value = rerunInputs.value[`${props.record.id}:${field.字段名}`]
                      ?? displayValue(field.映射.转换前值)
                    emit('rerun', { diagnosisId: props.record.id, field: field.字段名, rawValue: value })
                  },
                }, '只重跑此字段'),
              ])),
            ]),
          )),
        ]),
      ])
  },
})

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '解析模板登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据解析模板动作未生效，请稍后重试')
    }
    await reload()
    await loadOverview()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据解析模板操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('解析模板列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (selectedTemplateId.value === null && rows.value.length) {
      selectedTemplateId.value = Number(rows.value[0].id)
      prefillSample()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据解析模板列表读取失败'
  }
}

function selectTemplate(row: Row) {
  selectedTemplateId.value = Number(row.id)
  prefillSample()
  document.querySelector('.workbench')?.scrollIntoView({ behavior: 'smooth' })
}

function prefillSample() {
  const current = rows.value.find((row) => Number(row.id) === selectedTemplateId.value)
  const code = current ? String(current['模板编号']) : ''
  sampleText.value = EXAMPLE_SAMPLES[code] ?? ''
  diagnosisMessage.value = ''
}

async function submitDiagnosis(mode: string) {
  diagnosisMessage.value = ''
  if (selectedTemplateId.value === null) {
    diagnosisOk.value = false
    diagnosisMessage.value = '请先选择解析模板'
    return
  }
  runMode.value = mode
  try {
    const response = await request(`${ENDPOINT}/${selectedTemplateId.value}/diagnoses`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          sample: sampleText.value,
          run_mode: mode,
          related_task: relatedTask.value || null,
        },
      }),
    })
    const payload = await response.json()
    diagnosisOk.value = Boolean(payload.ok)
    diagnosisMessage.value = payload.message
    if (payload.ok && payload.entry) {
      detailMap.value[payload.entry.id] = payload.entry as DiagnosisRecord
      openDiagnosisId.value = payload.entry.id
    }
    await loadOverview()
  } catch (error) {
    diagnosisOk.value = false
    diagnosisMessage.value = error instanceof Error ? error.message : '诊断请求失败'
  }
}

async function loadOverview() {
  try {
    const response = await request(`${ENDPOINT}/diagnoses/overview`)
    if (!response.ok) {
      throw new Error('诊断概览读取失败')
    }
    const payload = await response.json()
    overviewGroups.value = payload.groups ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '诊断概览读取失败'
  }
}

async function toggleDetail(diagnosisId: string) {
  if (openDiagnosisId.value === diagnosisId) {
    openDiagnosisId.value = null
    return
  }
  openDiagnosisId.value = diagnosisId
  if (!detailMap.value[diagnosisId]) {
    try {
      const response = await request(`${ENDPOINT}/diagnoses/${diagnosisId}`)
      if (!response.ok) {
        throw new Error('诊断详情读取失败')
      }
      detailMap.value[diagnosisId] = await response.json()
    } catch (error) {
      errorMessage.value = error instanceof Error ? error.message : '诊断详情读取失败'
    }
  }
}

async function onRerun(payload: { diagnosisId: string; field: string; rawValue: string }) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/diagnoses/${payload.diagnosisId}/rerun-field`, {
      method: 'POST',
      body: JSON.stringify({ values: { field: payload.field, raw_value: payload.rawValue } }),
    })
    const result = await response.json()
    if (!result.ok) {
      throw new Error(result.message)
    }
    detailMap.value[payload.diagnosisId] = result.entry as DiagnosisRecord
    diagnosisOk.value = true
    diagnosisMessage.value = result.message
    await loadOverview()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段重跑失败'
  }
}

onMounted(async () => {
  await reload()
  await loadOverview()
})
</script>
