import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, vi } from 'vitest'
import { TasksPage } from './TasksPage'

function response(body: unknown) { return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })) }
const task = { id: 'task-1', group_id: 'group-1', dataset: 'EGO0-ready', status: 'succeeded', stage: 'publishing', created_at: '2026-08-20T10:00:00Z', started_at: '2026-08-20T10:01:00Z', finished_at: '2026-08-20T10:20:00Z', exit_code: 0, error_summary: null }

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => String(input).startsWith('/api/v1/tasks?') ? response({ items: [task] }) : response({ max_concurrency: 1, running: 0, queued: 0, accepting_tasks: true })))
})
afterEach(() => vi.unstubAllGlobals())

it('shows Kalibr task fields and result link', async () => {
  render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><TasksPage /></MemoryRouter>)
  expect(await screen.findByText('EGO0-ready')).toBeInTheDocument()
  expect(screen.getByText('发布结果')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '查看 EGO0-ready 结果' })).toHaveAttribute('href', '/results/task-1')
  expect(screen.getByRole('combobox', { name: '任务状态' })).toHaveTextContent('运行中')
  expect(screen.getByRole('textbox', { name: '数据集过滤' })).toBeInTheDocument()
})

it('keeps showing tasks when the runtime summary request fails', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) =>
    String(input).startsWith('/api/v1/tasks?')
      ? response({ items: [task] })
      : Promise.reject(new Error('runtime unavailable')),
  ))

  render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><TasksPage /></MemoryRouter>)

  expect(await screen.findByText('EGO0-ready')).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('运行状态加载失败：runtime unavailable')
})
