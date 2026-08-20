import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, vi } from 'vitest'
import { TasksPage } from './TasksPage'

const base = {
  group_id: 'group-1',
  created_at: '2026-08-20T10:00:00Z',
  started_at: null,
  finished_at: null,
  exit_code: null,
  error_summary: null,
}
const tasks = [
  { ...base, id: 'queued-1', dataset: 'queued-set', status: 'queued', stage: null },
  { ...base, id: 'running-1', dataset: 'running-set', status: 'running', stage: 'camera_calibration' },
  { ...base, id: 'paused-1', dataset: 'paused-set', status: 'running', stage: 'paused' },
  { ...base, id: 'failed-1', dataset: 'failed-set', status: 'failed', stage: 'camera_calibration' },
]

function response(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).startsWith('/api/v1/tasks?')) return response({ items: tasks })
    if (String(input) === '/api/v1/runtime') {
      return response({ max_concurrency: 1, running: 2, queued: 1, accepting_tasks: true })
    }
    return response({})
  }))
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('pauses and resumes running calibration tasks', async () => {
  const user = userEvent.setup()
  render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><TasksPage /></MemoryRouter>)

  await user.click(await screen.findByRole('button', { name: '暂停标定' }))
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(
    '/api/v1/tasks/running-1/pause',
    expect.objectContaining({ method: 'POST' }),
  ))

  await user.click(screen.getByRole('button', { name: '继续标定' }))
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(
    '/api/v1/tasks/paused-1/resume',
    expect.objectContaining({ method: 'POST' }),
  ))
})

it('confirms and deletes queued calibration tasks', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  const user = userEvent.setup()
  render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><TasksPage /></MemoryRouter>)

  await user.click(await screen.findByRole('button', { name: '删除队列' }))
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(
    '/api/v1/tasks/queued-1',
    expect.objectContaining({ method: 'DELETE' }),
  ))
})

it('confirms and deletes finished task records', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  const user = userEvent.setup()
  render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><TasksPage /></MemoryRouter>)

  await user.click(await screen.findByRole('button', { name: '删除记录' }))
  expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('原始 Bag 和 result 标定结果不会删除'))
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(
    '/api/v1/tasks/failed-1',
    expect.objectContaining({ method: 'DELETE' }),
  ))
})
