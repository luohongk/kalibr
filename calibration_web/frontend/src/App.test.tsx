import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, vi } from 'vitest'
import { App } from './App'

beforeEach(() => { vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => undefined))) })
afterEach(() => { vi.unstubAllGlobals() })

function renderRoute(route: string) {
  return render(<MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><App /></MemoryRouter>)
}

describe('application routes', () => {
  it.each([['/run', '自动标定'], ['/tasks', '任务中心'], ['/results/task-42', '结果详情']])('renders %s', (route, heading) => {
    renderRoute(route)
    expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: '主导航' })).toBeInTheDocument()
  })

  it('redirects unknown routes to calibration run', () => {
    renderRoute('/does-not-exist')
    expect(screen.getByRole('heading', { name: '自动标定' })).toBeInTheDocument()
  })

  it('shows the global runtime summary', () => {
    renderRoute('/run')
    expect(screen.getByText('并发上限')).toBeInTheDocument()
    expect(screen.getByText('运行中')).toBeInTheDocument()
    expect(screen.getByText('排队')).toBeInTheDocument()
  })
})
