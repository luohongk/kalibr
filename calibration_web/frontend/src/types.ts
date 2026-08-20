export type DatasetState = 'ready' | 'busy' | 'completed'

export interface Dataset {
  name: string
  state: DatasetState
  active_task_id: string | null
}

export interface TaskGroupCreated {
  group_id: string
  task_ids: string[]
}

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'interrupted'

export interface Task {
  id: string
  group_id: string
  dataset: string
  status: TaskStatus
  stage: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  exit_code: number | null
  error_summary: string | null
  group_created_at?: string
}

export interface Artifact {
  path: string
  name: string
  size: number
}

export interface CalibrationSummary {
  verdict: 'OK' | 'WARN' | 'FAIL' | null
  text: string
}

export interface RuntimeStatus {
  max_concurrency: number
  running: number
  queued: number
  accepting_tasks: boolean
}
