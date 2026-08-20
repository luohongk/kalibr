import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { taskApi } from '../api'
import { StatusBadge } from '../components/StatusBadge'
import { TaskLog } from '../components/TaskLog'
import type { RuntimeStatus, Task, TaskStatus } from '../types'

const stageLabels: Record<string, string> = {
  preparing: '准备环境',
  imu_calibration: 'IMU Allan 标定',
  bag_conversion: 'Bag 转换',
  camera_calibration: '多相机标定',
  imu_camera_calibration: 'Camera-IMU 标定',
  summarizing: '质量汇总',
  publishing: '发布结果',
}

export function TasksPage() {
  const [tasks, setTasks] = useState<Task[]>([])
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null)
  const [selectedId, setSelectedId] = useState('')
  const [error, setError] = useState('')
  const [actionId, setActionId] = useState('')
  const [appliedQuery, setAppliedQuery] = useState('limit=100&offset=0')
  const [status, setStatus] = useState('')
  const [dataset, setDataset] = useState('')

  const load = useCallback(async (signal?: AbortSignal) => {
    const [taskResult, runtimeResult] = await Promise.allSettled([
      taskApi.list(appliedQuery, signal),
      taskApi.runtime(signal),
    ])

    if (taskResult.status === 'fulfilled') {
      setTasks(taskResult.value.items)
    } else if (!isAbortError(taskResult.reason)) {
      setError(taskResult.reason instanceof Error ? taskResult.reason.message : '任务加载失败')
      return
    }

    if (runtimeResult.status === 'fulfilled') {
      setRuntime(runtimeResult.value)
      setError('')
    } else if (!isAbortError(runtimeResult.reason)) {
      setError(runtimeResult.reason instanceof Error
        ? `运行状态加载失败：${runtimeResult.reason.message}`
        : '运行状态加载失败')
    }
  }, [appliedQuery])

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    let timer: number | undefined
    const poll = async () => {
      await load(controller.signal)
      if (!disposed) timer = window.setTimeout(() => void poll(), 3000)
    }
    void poll()
    return () => {
      disposed = true
      controller.abort()
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [load])

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === selectedId) ?? null,
    [selectedId, tasks],
  )

  const applyFilters = (event: FormEvent) => {
    event.preventDefault()
    const query = new URLSearchParams()
    if (status) query.set('status', status)
    if (dataset.trim()) query.set('dataset', dataset.trim())
    query.set('limit', '100')
    query.set('offset', '0')
    setAppliedQuery(query.toString())
  }

  const controlTask = async (task: Task, action: 'pause' | 'resume' | 'delete') => {
    if (action === 'delete' && !window.confirm(
      `确定删除任务“${task.dataset}”吗？任务运行日志将被清理，原始 Bag 和 result 标定结果不会删除。`,
    )) return
    setActionId(task.id)
    setError('')
    const path = action === 'delete'
      ? `/api/v1/tasks/${encodeURIComponent(task.id)}`
      : `/api/v1/tasks/${encodeURIComponent(task.id)}/${action}`
    try {
      const response = await fetch(path, {
        method: action === 'delete' ? 'DELETE' : 'POST',
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) {
        let message = `任务操作失败 (${response.status})`
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
      setError(reason instanceof Error ? reason.message : '任务操作失败')
    } finally {
      setActionId('')
    }
  }

  return (
    <div className="tasks-page">
      <header className="tasks-heading">
        <div><p className="eyebrow">QUEUE / CALIBRATION / LOGS</p><h1>任务中心</h1></div>
        <section className="queue-readout" aria-label="队列运行摘要">
          <span><b>{runtime?.max_concurrency ?? '—'}</b> 路并发</span>
          <span><b>{runtime?.running ?? '—'}</b> 运行中</span>
          <span><b>{runtime?.queued ?? '—'}</b> 排队</span>
          <i className={runtime?.accepting_tasks ? 'is-online' : ''}>{runtime?.accepting_tasks ? 'ACCEPTING' : 'PAUSED'}</i>
        </section>
      </header>

      {error && <div className="error-banner" role="alert">{error}</div>}

      <form className="task-filters calibration-task-filters panel-block" onSubmit={applyFilters}>
        <label><span>任务状态</span><select aria-label="任务状态" value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="">全部状态</option><option value="queued">等待中</option><option value="running">运行中</option><option value="succeeded">标定成功</option><option value="failed">标定失败</option><option value="interrupted">已中断</option>
        </select></label>
        <label><span>数据集</span><input aria-label="数据集过滤" value={dataset} onChange={(event) => setDataset(event.target.value)} /></label>
        <button type="submit">应用过滤</button>
      </form>

      <div className="task-workbench">
        <section className="task-list panel-block" aria-label="任务列表">
          <div className="section-label"><span>01</span>标定队列 <b>{tasks.length}</b></div>
          <div className="task-list__body">
            {tasks.map((task) => (
              <article key={task.id} className={selectedId === task.id ? 'task-entry is-selected' : 'task-entry'}>
                <button type="button" aria-pressed={selectedId === task.id} onClick={() => setSelectedId(task.id)}>
                  <div className="task-entry__top"><b>{task.dataset}</b><StatusBadge status={task.status} paused={task.stage === 'paused'} /></div>
                  <dl>
                    <div><dt>STAGE</dt><dd>{task.stage ? stageLabels[task.stage] ?? task.stage : '—'}</dd></div>
                    <div><dt>TASK</dt><dd>{task.id}</dd></div>
                    <div><dt>CREATED</dt><dd>{formatTime(task.created_at)}</dd></div>
                  </dl>
                  {(task.status === 'failed' || task.status === 'interrupted') && (
                    <div className="task-failure"><span>退出码 {task.exit_code ?? '—'}</span><strong>{task.error_summary ?? '未知错误'}</strong></div>
                  )}
                </button>
                {task.status !== 'running' && (
                  <div className="task-entry__actions">
                    <button type="button" className="task-action task-action--delete" disabled={actionId === task.id} onClick={() => void controlTask(task, 'delete')}>
                      {actionId === task.id ? '正在删除…' : task.status === 'queued' ? '删除队列' : '删除记录'}
                    </button>
                  </div>
                )}
                {task.status === 'running' && (
                  <div className="task-entry__actions">
                    <button type="button" className="task-action" disabled={actionId === task.id} onClick={() => void controlTask(task, task.stage === 'paused' ? 'resume' : 'pause')}>
                      {actionId === task.id ? '正在操作…' : task.stage === 'paused' ? '继续标定' : '暂停标定'}
                    </button>
                  </div>
                )}
                {task.status === 'succeeded' && <Link className="result-link" to={`/results/${task.id}`} aria-label={`查看 ${task.dataset} 结果`}>查看标定结果 →</Link>}
              </article>
            ))}
            {tasks.length === 0 && !error && <p className="task-empty">暂无符合条件的任务</p>}
          </div>
        </section>
        <section className="task-detail panel-block">
          <div className="section-label"><span>02</span>实时标定日志</div>
          {selectedTask ? <TaskLog task={selectedTask} /> : <div className="console-empty"><span>SELECT TASK</span><p>从左侧选择任务以连接实时日志。</p></div>}
        </section>
      </div>
    </div>
  )
}

function formatTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function isAbortError(reason: unknown) {
  return reason instanceof DOMException && reason.name === 'AbortError'
}

export function isTaskTerminal(status: TaskStatus) {
  return new Set<TaskStatus>(['succeeded', 'failed', 'interrupted']).has(status)
}
