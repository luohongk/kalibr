import type { TaskStatus } from '../types'

const labels: Record<TaskStatus, string> = {
  queued: '等待中',
  running: '标定中',
  succeeded: '标定成功',
  failed: '标定失败',
  interrupted: '已中断',
}

export function StatusBadge({ status, paused = false }: { status: TaskStatus; paused?: boolean }) {
  if (paused) {
    return <span className="status-badge status-badge--paused">已暂停</span>
  }
  return <span className={`status-badge status-badge--${status}`}>{labels[status]}</span>
}
