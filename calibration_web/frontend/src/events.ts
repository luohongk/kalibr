import type { Task, TaskStatus } from './types'

export interface TaskEventHandlers {
  snapshot: (task: Task) => void
  log: (text: string, reset: boolean) => void
  status: (status: TaskStatus) => void
  terminal: (status: TaskStatus) => void
  open?: () => void
  error?: () => void
}

export function openTaskEvents(taskId: string, handlers: TaskEventHandlers) {
  const source = new EventSource(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`)
  let disposed = false

  source.addEventListener('snapshot', (event) => {
    if (disposed) return
    const payload = JSON.parse((event as MessageEvent).data) as { task: Task }
    handlers.snapshot(payload.task)
  })
  source.addEventListener('log', (event) => {
    if (disposed) return
    const payload = JSON.parse((event as MessageEvent).data) as {
      text: string
      reset?: boolean
    }
    handlers.log(payload.text, Boolean(payload.reset))
  })
  source.addEventListener('status', (event) => {
    if (disposed) return
    const payload = JSON.parse((event as MessageEvent).data) as { status: TaskStatus }
    handlers.status(payload.status)
  })
  source.addEventListener('terminal', (event) => {
    if (disposed) return
    const payload = JSON.parse((event as MessageEvent).data) as { status: TaskStatus }
    handlers.terminal(payload.status)
    disposed = true
    source.close()
  })
  source.onopen = () => {
    if (!disposed) handlers.open?.()
  }
  source.onerror = () => {
    if (!disposed) handlers.error?.()
  }
  return () => {
    disposed = true
    source.close()
  }
}
