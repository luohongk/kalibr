import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, vi } from 'vitest'
import { DataRunPage } from './DataRunPage'

function response(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }))
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input) === '/api/v1/datasets') {
      return response({ items: [
        { name: 'EGO5-complete', state: 'completed', active_task_id: null },
      ] })
    }
    return response({ dataset: 'EGO5-complete', reset: true })
  }))
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

it('confirms and deletes the completed dataset result directory', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
  const user = userEvent.setup()
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <DataRunPage />
    </MemoryRouter>,
  )

  await user.click(await screen.findByRole('button', { name: '重置标定' }))

  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('永久删除'))
  await waitFor(() => expect(fetch).toHaveBeenCalledWith(
    '/api/v1/datasets/EGO5-complete/result',
    expect.objectContaining({ method: 'DELETE' }),
  ))
})

it('does not delete when confirmation is cancelled', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(false)
  const user = userEvent.setup()
  render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <DataRunPage />
    </MemoryRouter>,
  )

  await user.click(await screen.findByRole('button', { name: '重置标定' }))

  expect(fetch).not.toHaveBeenCalledWith(
    '/api/v1/datasets/EGO5-complete/result',
    expect.objectContaining({ method: 'DELETE' }),
  )
})
