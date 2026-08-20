import type { ReactNode } from 'react'
import { Link, NavLink } from 'react-router-dom'
import { RuntimeSummary } from './RuntimeSummary'

const navItems = [
  { to: '/run', label: '自动标定', index: '01' },
  { to: '/tasks', label: '任务中心', index: '02' },
]

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Link className="brand" to="/run" aria-label="Kalibr 自动标定平台">
          <span className="brand-mark" aria-hidden="true">K</span>
          <span><b>KALIBR LAB</b><small>多相机与 IMU 自动标定</small></span>
        </Link>
        <nav className="main-nav" aria-label="主导航">
          {navItems.map((item) => <NavLink key={item.to} to={item.to} className={({ isActive }) => isActive ? 'nav-link is-active' : 'nav-link'}><span aria-hidden="true">{item.index}</span>{item.label}</NavLink>)}
        </nav>
        <RuntimeSummary />
      </header>
      <main className="workspace">{children}</main>
    </div>
  )
}
