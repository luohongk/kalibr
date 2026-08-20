import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { DataRunPage } from './pages/DataRunPage'
import { ResultPage } from './pages/ResultPage'
import { TasksPage } from './pages/TasksPage'

export function App() {
  return (
    <AppShell>
      <Routes>
        <Route path="/run" element={<DataRunPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/results/:taskId" element={<ResultPage />} />
        <Route path="*" element={<Navigate to="/run" replace />} />
      </Routes>
    </AppShell>
  )
}
