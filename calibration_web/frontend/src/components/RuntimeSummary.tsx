import { useEffect, useState } from 'react'
import { taskApi } from '../api'
import type { RuntimeStatus } from '../types'

export function RuntimeSummary() {
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    let timer: number | undefined
    let stopped = false
    const poll = async () => {
      try { setRuntime(await taskApi.runtime(controller.signal)) } catch { /* task page shows details */ }
      if (!stopped) timer = window.setTimeout(() => void poll(), 5000)
    }
    void poll()
    return () => { stopped = true; controller.abort(); if (timer !== undefined) window.clearTimeout(timer) }
  }, [])
  return (
    <section className="runtime-strip" aria-label="运行状态">
      <div className="runtime-cell"><span>并发上限</span><strong>{runtime?.max_concurrency ?? '—'}</strong></div>
      <div className="runtime-cell runtime-cell--active"><span>运行中</span><strong>{runtime?.running ?? '—'}</strong></div>
      <div className="runtime-cell"><span>排队</span><strong>{runtime?.queued ?? '—'}</strong></div>
      <span className="runtime-pulse">{runtime?.accepting_tasks ? 'SYSTEM READY' : 'SYSTEM PAUSED'}</span>
    </section>
  )
}
