import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, vi } from 'vitest'
import { DataRunPage } from './DataRunPage'

const datasets = { items: [
  { name: 'EGO0-ready', state: 'ready', active_task_id: null },
  { name: 'EGO2-done', state: 'completed', active_task_id: null },
  { name: 'EGO5-busy', state: 'busy', active_task_id: 'task-5' },
] }

function response(body: unknown, status = 200) { return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })) }

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/v1/datasets') return response(datasets)
    if (url === '/api/v1/task-groups') return response({ group_id: 'g', task_ids: ['t'] }, 201)
    return response({ max_concurrency: 1, running: 0, queued: 0, accepting_tasks: true })
  }))
})
afterEach(() => vi.unstubAllGlobals())

function renderPage() { return render(<MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><DataRunPage /></MemoryRouter>) }

it('shows Kalibr datasets and their states', async () => {
  renderPage()
  expect(await screen.findByText('EGO0-ready')).toBeInTheDocument()
  expect(screen.getByText('EGO2-done')).toBeInTheDocument()
  expect(screen.getByText('EGO5-busy')).toBeInTheDocument()
  expect(screen.getByText('可标定')).toBeInTheDocument()
  expect(screen.getByText('已完成')).toBeInTheDocument()
})

it('only allows ready datasets and submits exact dataset list', async () => {
  const user = userEvent.setup()
  renderPage()
  const ready = await screen.findByRole('checkbox', { name: '选择 EGO0-ready' })
  expect(screen.getByRole('checkbox', { name: '选择 EGO2-done' })).toBeDisabled()
  await user.click(ready)
  await user.click(screen.getByRole('button', { name: /开始标定 1 组/ }))
  await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/task-groups', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ datasets: ['EGO0-ready'] }),
  })))
})

it('filters datasets by name', async () => {
  const user = userEvent.setup()
  renderPage()
  await screen.findByText('EGO0-ready')
  await user.type(screen.getByRole('searchbox', { name: '搜索数据集' }), 'EGO2')
  expect(screen.getByText('EGO2-done')).toBeInTheDocument()
  expect(screen.queryByText('EGO0-ready')).not.toBeInTheDocument()
})
