import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, vi } from 'vitest'
import { ResultPage } from './ResultPage'

function response(body: unknown, status = 200) { return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })) }
const task = { id: 'task-1', group_id: 'group-1', dataset: 'EGO5-calib', status: 'succeeded', stage: 'publishing', created_at: '2026-08-20T10:00:00Z', started_at: '2026-08-20T10:01:00Z', finished_at: '2026-08-20T10:20:00Z', exit_code: 0, error_summary: null }

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/summary')) return response({ verdict: 'OK', text: '总判定: [OK]\n' })
    if (url.endsWith('/artifacts')) return response({ items: [{ path: 'result/imu.yaml', name: 'imu.yaml', size: 449 }] })
    if (url === '/api/v1/tasks/task-1') return response(task)
    return response({ max_concurrency: 1, running: 0, queued: 0, accepting_tasks: true })
  }))
})
afterEach(() => vi.unstubAllGlobals())

it('renders quality summary and downloadable Kalibr artifacts', async () => {
  render(<MemoryRouter initialEntries={['/results/task-1']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><Routes><Route path="/results/:taskId" element={<ResultPage />} /></Routes></MemoryRouter>)
  expect(await screen.findByRole('heading', { name: 'EGO5-calib' })).toBeInTheDocument()
  expect(await screen.findByText('总判定: [OK]')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '下载 imu.yaml' })).toHaveAttribute('href', '/api/v1/tasks/task-1/artifacts/result/imu.yaml')
})
