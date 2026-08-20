import type {
  Artifact,
  CalibrationSummary,
  Dataset,
  RuntimeStatus,
  Task,
  TaskGroupCreated,
} from './types'

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function apiRequest<T>(
  path: string,
  init?: RequestInit,
  expectedStatus?: number,
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    cache: init?.cache ?? 'no-store',
    headers: { Accept: 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    let message = `请求失败 (${response.status})`
    let code: string | undefined
    try {
      const error = (await response.json()) as {
        detail?: string | { code?: string; message?: string }
      }
      if (typeof error.detail === 'string') message = error.detail
      else if (error.detail) {
        if (typeof error.detail.message === 'string') message = error.detail.message
        if (typeof error.detail.code === 'string') code = error.detail.code
      }
    } catch {
      // Keep the status-based message when the server did not return JSON.
    }
    throw new ApiError(message, response.status, code)
  }
  if (expectedStatus !== undefined && response.status !== expectedStatus) {
    throw new Error(`请求需要返回 ${expectedStatus}，实际为 ${response.status}`)
  }
  return (await response.json()) as T
}

export const taskApi = {
  detail: (taskId: string, signal?: AbortSignal) =>
    apiRequest<Task>(`/api/v1/tasks/${encodeURIComponent(taskId)}`, { signal }),
  summary: (taskId: string, signal?: AbortSignal) =>
    apiRequest<CalibrationSummary>(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/summary`,
      { signal },
    ),
  artifacts: (taskId: string, signal?: AbortSignal) =>
    apiRequest<{ items: Artifact[] }>(
      `/api/v1/tasks/${encodeURIComponent(taskId)}/artifacts`,
      { signal },
    ),
  list: (query: string, signal?: AbortSignal) =>
    apiRequest<{ items: Task[] }>(`/api/v1/tasks?${query}`, { signal }),
  runtime: (signal?: AbortSignal) =>
    apiRequest<RuntimeStatus>('/api/v1/runtime', { signal }),
}

export const catalogApi = {
  datasets: (signal?: AbortSignal) =>
    apiRequest<{ items: Dataset[] }>('/api/v1/datasets', { signal }),
  createTaskGroup: (datasets: string[], signal?: AbortSignal) =>
    apiRequest<TaskGroupCreated>(
      '/api/v1/task-groups',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ datasets }),
        signal,
      },
      201,
    ),
}
