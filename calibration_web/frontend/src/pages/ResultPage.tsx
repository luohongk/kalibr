import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError, taskApi } from '../api'
import { StatusBadge } from '../components/StatusBadge'
import type { Artifact, CalibrationSummary, Task } from '../types'

export function ResultPage() {
  const { taskId = '' } = useParams()
  const [task, setTask] = useState<Task | null>(null)
  const [summary, setSummary] = useState<CalibrationSummary | null>(null)
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [error, setError] = useState('')
  const [artifactError, setArtifactError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    const load = async () => {
      try {
        const taskData = await taskApi.detail(taskId, controller.signal)
        if (!active) return
        setTask(taskData)
        const results = await Promise.allSettled([
          taskApi.summary(taskId, controller.signal),
          taskApi.artifacts(taskId, controller.signal),
        ])
        if (!active) return
        if (results[0].status === 'fulfilled') setSummary(results[0].value)
        else if (!isExpectedMissing(results[0].reason)) setError(errorMessage(results[0].reason, '质量汇总加载失败'))
        if (results[1].status === 'fulfilled') setArtifacts(results[1].value.items.filter((item) => isSafeArtifactPath(item.path)))
        else setArtifactError(errorMessage(results[1].reason, '产物列表加载失败'))
      } catch (reason) {
        if (active && !isAbort(reason)) setError(errorMessage(reason, '任务加载失败'))
      }
    }
    void load()
    return () => { active = false; controller.abort() }
  }, [taskId])

  const terminalFailure = task?.status === 'failed' || task?.status === 'interrupted'

  return (
    <div className="result-page">
      <header className="result-heading">
        <div><p className="eyebrow">KALIBR / QUALITY / ARTIFACTS</p><h1>{task?.dataset ?? '结果详情'}</h1><p className="task-reference">TASK · {taskId}</p></div>
        {task && <StatusBadge status={task.status} />}
      </header>
      {error && <div className="error-banner" role="alert">{error}</div>}

      {task && task.status !== 'succeeded' && (
        <section className="result-pending panel-block">
          <span>{terminalFailure ? 'CALIBRATION TERMINATED' : 'RESULT NOT READY'}</span>
          <h2>{terminalFailure ? '标定未成功完成' : '标定尚未完成'}</h2>
          <StatusBadge status={task.status} />
          <p>{terminalFailure ? task.error_summary ?? '请下载 runner.log 检查失败原因。' : '任务完成后将显示质量汇总、YAML 参数、PDF 报告和过程日志。'}</p>
          <Link to="/tasks">返回任务中心</Link>
        </section>
      )}

      {task && <section className="result-meta panel-block">
        <div><span>DATASET</span><b>{task.dataset}</b></div>
        <div><span>STAGE</span><b>{task.stage ?? '—'}</b></div>
        <div><span>EXIT CODE</span><b>{task.exit_code ?? '—'}</b></div>
        <div><span>STARTED</span><b>{task.started_at ? formatDate(task.started_at) : '—'}</b></div>
        <div><span>FINISHED</span><b>{task.finished_at ? formatDate(task.finished_at) : '—'}</b></div>
      </section>}

      {summary && <section className="summary-workbench">
        <div className={`summary-verdict summary-verdict--${(summary.verdict ?? 'unknown').toLowerCase()}`}>
          <span>QUALITY VERDICT</span><b>{summary.verdict ?? 'UNKNOWN'}</b><small>来自 result/summary.txt</small>
        </div>
        <div className="summary-panel panel-block"><div className="section-label"><span>01</span>标定质量汇总</div><pre>{summary.text}</pre></div>
      </section>}

      {task && <section className="artifact-panel panel-block">
        <div className="section-label"><span>02</span>标定产物 <b>{artifacts.length}</b></div>
        {artifactError ? <p className="artifact-error" role="alert">{artifactError}</p> : <div className="artifact-list">
          {artifacts.map((artifact) => <a key={artifact.path} href={artifactDownloadUrl(task.id, artifact.path)} aria-label={`下载 ${artifact.name}`}><span>{artifact.name}</span><small>{formatBytes(artifact.size)}</small><b>DOWNLOAD ↓</b></a>)}
          {artifacts.length === 0 && <p>尚未发现可下载产物</p>}
        </div>}
      </section>}
    </div>
  )
}

function isExpectedMissing(reason: unknown) { return reason instanceof ApiError && (reason.code === 'summary_missing' || reason.status === 404) }
function isSafeArtifactPath(path: string) { return path.split('/').every((part) => part !== '' && part !== '.' && part !== '..') }
function artifactDownloadUrl(taskId: string, path: string) { return `/api/v1/tasks/${encodeURIComponent(taskId)}/artifacts/${path.split('/').map(encodeURIComponent).join('/')}` }
function formatBytes(size: number) { if (size < 1024) return `${size} B`; if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`; return `${(size / 1024 / 1024).toFixed(1)} MB` }
function formatDate(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function isAbort(reason: unknown) { return reason instanceof DOMException && reason.name === 'AbortError' }
function errorMessage(reason: unknown, fallback: string) { return reason instanceof Error ? reason.message : fallback }
