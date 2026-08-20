import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { catalogApi } from '../api'
import type { Dataset, DatasetState } from '../types'

function isAbortError(reason: unknown) {
  return reason instanceof DOMException && reason.name === 'AbortError'
}

const stateLabel: Record<DatasetState, string> = {
  ready: '可标定',
  busy: '任务进行中',
  completed: '已完成',
}

export function DataRunPage() {
  const navigate = useNavigate()
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [resetting, setResetting] = useState('')
  const [error, setError] = useState('')
  const mounted = useRef(true)
  const request = useRef<AbortController | null>(null)

  const load = async () => {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    try {
      const response = await catalogApi.datasets(controller.signal)
      if (mounted.current) {
        setDatasets(response.items)
        setSelected((current) => new Set([...current].filter((name) =>
          response.items.some((item) => item.name === name && item.state === 'ready'),
        )))
        setError('')
      }
    } catch (reason) {
      if (mounted.current && !isAbortError(reason)) {
        setError(reason instanceof Error ? reason.message : '数据目录加载失败')
      }
    } finally {
      if (mounted.current) setLoading(false)
    }
  }

  useEffect(() => {
    mounted.current = true
    void load()
    return () => {
      mounted.current = false
      request.current?.abort()
    }
  }, [])

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase()
    return term ? datasets.filter((item) => item.name.toLowerCase().includes(term)) : datasets
  }, [datasets, search])

  const visibleReady = filtered.filter((item) => item.state === 'ready')
  const allVisibleSelected =
    visibleReady.length > 0 && visibleReady.every((item) => selected.has(item.name))

  const toggle = (dataset: Dataset) => {
    if (dataset.state !== 'ready') return
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(dataset.name)) next.delete(dataset.name)
      else next.add(dataset.name)
      return next
    })
  }

  const toggleVisible = () => {
    setSelected((current) => {
      const next = new Set(current)
      for (const item of visibleReady) {
        if (allVisibleSelected) next.delete(item.name)
        else next.add(item.name)
      }
      return next
    })
  }

  const submit = async () => {
    if (selected.size === 0) return
    setSubmitting(true)
    setError('')
    try {
      await catalogApi.createTaskGroup([...selected])
      if (mounted.current) navigate('/tasks')
    } catch (reason) {
      if (mounted.current && !isAbortError(reason)) {
        setError(reason instanceof Error ? reason.message : '任务提交失败')
      }
    } finally {
      if (mounted.current) setSubmitting(false)
    }
  }

  const resetResult = async (dataset: Dataset) => {
    const confirmed = window.confirm(
      `确定重置“${dataset.name}”吗？\n\n该操作会永久删除该数据集下的整个 result 目录，包括 YAML、PDF、质量汇总和日志。`,
    )
    if (!confirmed) return
    setResetting(dataset.name)
    setError('')
    try {
      const response = await fetch(
        `/api/v1/datasets/${encodeURIComponent(dataset.name)}/result`,
        { method: 'DELETE', headers: { Accept: 'application/json' } },
      )
      if (!response.ok) {
        let message = `重置失败 (${response.status})`
        try {
          const payload = await response.json() as { detail?: string }
          if (payload.detail) message = payload.detail
        } catch {
          // Keep the status-based message.
        }
        throw new Error(message)
      }
      await load()
    } catch (reason) {
      if (mounted.current) {
        setError(reason instanceof Error ? reason.message : '重置标定失败')
      }
    } finally {
      if (mounted.current) setResetting('')
    }
  }

  const count = (state: DatasetState) => datasets.filter((item) => item.state === state).length

  return (
    <div className="run-page">
      <header className="run-heading">
        <div><p className="eyebrow">KALIBR / MULTI-CAMERA / IMU</p><h1>自动标定</h1></div>
        <div className="run-heading__readout"><span>DATA ROOT</span><b>/home/conanluo/kalibr_data</b></div>
      </header>

      {error && <div className="error-banner" role="alert">{error}</div>}

      <section className="run-grid">
        <aside className="device-panel panel-block">
          <div className="section-label"><span>01</span>数据概览</div>
          <div className="device-list">
            <div className="device-card is-selected"><span className="device-card__signal" /><b>{count('ready')}</b><small>可标定数据集</small></div>
            <div className="device-card"><span className="device-card__signal" /><b>{count('busy')}</b><small>排队或运行中</small></div>
            <div className="device-card"><span className="device-card__signal" /><b>{count('completed')}</b><small>已有 result/.done</small></div>
            <button className="launch-button" type="button" onClick={() => void load()} disabled={loading}>{loading ? '正在扫描…' : '刷新目录'}</button>
          </div>
        </aside>

        <section className="sequence-panel panel-block">
          <div className="section-label"><span>02</span>选择标定数据集</div>
          <div className="sequence-tools">
            <input type="search" aria-label="搜索数据集" placeholder="SEARCH DATASET" value={search} onChange={(event) => setSearch(event.target.value)} />
            <label className="select-visible"><input type="checkbox" aria-label="全选可标定数据集" checked={allVisibleSelected} onChange={toggleVisible} disabled={visibleReady.length === 0} />全选当前可标定项</label>
            <strong>已选择 {selected.size} 组</strong>
          </div>
          <div className="sequence-table-wrap">
            <table className="sequence-table">
              <thead><tr><th>选择</th><th>数据集</th><th>输入</th><th>状态</th><th>关联任务</th><th>操作</th></tr></thead>
              <tbody>
                {filtered.map((dataset) => (
                  <tr key={dataset.name} className={selected.has(dataset.name) ? 'is-selected' : ''}>
                    <td><input type="checkbox" aria-label={`选择 ${dataset.name}`} checked={selected.has(dataset.name)} disabled={dataset.state !== 'ready'} onChange={() => toggle(dataset)} /></td>
                    <td><b>{dataset.name}</b></td>
                    <td>4CAM + CAM0/IMU + ALLAN</td>
                    <td><span className={dataset.state === 'ready' ? 'data-state data-state--ok' : 'data-state data-state--warn'}>{stateLabel[dataset.state]}</span></td>
                    <td>{dataset.active_task_id ? <Link to="/tasks">{dataset.active_task_id.slice(0, 8)}</Link> : '—'}</td>
                    <td>{dataset.state === 'completed' ? (
                      <button className="reset-result-button" type="button" disabled={Boolean(resetting)} onClick={() => void resetResult(dataset)}>
                        {resetting === dataset.name ? '正在重置…' : '重置标定'}
                      </button>
                    ) : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {loading && <div className="table-empty">正在检查三个必需 Bag…</div>}
            {!loading && filtered.length === 0 && <div className="table-empty">没有匹配的数据集</div>}
          </div>
        </section>
      </section>

      <section className="launch-panel panel-block">
        <div className="section-label"><span>03</span>提交标定</div>
        <div className="launch-controls calibration-launch-controls">
          <div className="calibration-command"><span>RUNNER</span><b>bash auto_calib/run_all.sh &lt;dataset&gt;</b></div>
          <div className="launch-summary"><span>SELECTED <b>{selected.size}</b></span><span>CONCURRENCY <b>1</b></span></div>
          <button className="launch-button" type="button" disabled={selected.size === 0 || submitting} onClick={() => void submit()}>{submitting ? '正在提交…' : `开始标定 ${selected.size || ''} 组`}</button>
        </div>
      </section>
    </div>
  )
}
