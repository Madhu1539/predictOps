import React, { Suspense, lazy, useState } from 'react';
import { BrowserRouter, Routes, Route, NavLink, useLocation } from 'react-router-dom';
import AuthMenu from './components/AuthMenu';

const Dashboard   = lazy(() => import('./pages/Dashboard'));
const MachineDetail = lazy(() => import('./pages/MachineDetail'));
const WorkOrders  = lazy(() => import('./pages/WorkOrders'));
const Investigate = lazy(() => import('./pages/Investigate'));
const BringYourData = lazy(() => import('./pages/BringYourData'));
const OeeLosses   = lazy(() => import('./pages/OeeLosses'));
const ModelCard   = lazy(() => import('./pages/ModelCard'));
const Verify      = lazy(() => import('./pages/Verify'));

/* ── Icons ─────────────────────────────────────────────────── */
const IconDash = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>
    <rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>
  </svg>
);
const IconWO = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2"/>
    <rect x="9" y="3" width="6" height="4" rx="1"/><path d="M9 12h6M9 16h4"/>
  </svg>
);
const IconBolt = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
    <path d="M13 2L4.09 12.96A1 1 0 005 14.5h6.5L11 22l9-10.96A1 1 0 0019 9.5h-6.5L13 2z"/>
  </svg>
);
const IconSearch = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>
  </svg>
);
const IconUpload = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v12"/>
  </svg>
);
const IconLoss = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 3v18h18"/><rect x="7" y="12" width="3" height="6"/><rect x="12" y="8" width="3" height="10"/><rect x="17" y="5" width="3" height="13"/>
  </svg>
);
const IconModel = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>
    <path d="M4.9 4.9l2.9 2.9M16.2 16.2l2.9 2.9M19.1 4.9l-2.9 2.9M7.8 16.2l-2.9 2.9"/>
  </svg>
);

/* ── Sidebar NavItem ────────────────────────────────────────── */
const NavItem: React.FC<{ to: string; exact?: boolean; icon: React.ReactNode; label: string }> = ({ to, exact, icon, label }) => (
  <NavLink to={to} end={exact} style={({ isActive }) => ({
    display: 'flex', alignItems: 'center', gap: 10, padding: '9px 12px',
    borderRadius: 8, textDecoration: 'none', fontSize: 13, fontWeight: isActive ? 600 : 400,
    color: isActive ? '#f1f5f9' : '#64748b',
    background: isActive ? 'rgba(59,130,246,0.12)' : 'transparent',
    border: isActive ? '1px solid rgba(59,130,246,0.2)' : '1px solid transparent',
    transition: 'all .15s',
  })}>
    {icon}
    {label}
  </NavLink>
);

/* ── Loading spinner ────────────────────────────────────────── */
const Spinner = () => (
  <div style={{ display:'flex', alignItems:'center', justifyContent:'center', height:400, gap:12 }}>
    <div style={{ width:22, height:22, borderRadius:'50%', border:'2.5px solid rgba(59,130,246,0.2)', borderTopColor:'#3b82f6', animation:'spin .8s linear infinite' }} />
    <span style={{ color:'#64748b', fontSize:13 }}>Loading…</span>
    <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
  </div>
);

/* ── Page title helper ──────────────────────────────────────── */
const PageTitle: React.FC = () => {
  const loc = useLocation();
  const map: Record<string, string> = {
    '/': 'Command Center', '/workorders': 'Work Orders', '/investigate': 'Investigation',
    '/your-data': 'Bring Your Own Data', '/oee': 'OEE Loss Analysis', '/model': 'Model Transparency',
    '/verify': 'Account Verification',
  };
  return <span style={{ fontSize:12, color:'#475569', fontWeight:500 }}>{map[loc.pathname] ?? 'Machine Detail'}</span>;
};

const App: React.FC = () => {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <BrowserRouter>
      <div style={{ display:'flex', height:'100vh', overflow:'hidden' }}>

        {/* ── SIDEBAR ─────────────────────────────────────────── */}
        <aside style={{
          width: collapsed ? 56 : 220,
          minWidth: collapsed ? 56 : 220,
          background: '#111827',
          borderRight: '1px solid #1e2d45',
          display: 'flex', flexDirection: 'column',
          transition: 'width .2s, min-width .2s',
          overflow: 'hidden',
        }}>
          {/* Logo */}
          <div style={{ padding: '16px 12px', borderBottom:'1px solid #1e2d45', display:'flex', alignItems:'center', gap:10, minHeight:57 }}>
            <div style={{ width:30, height:30, borderRadius:8, background:'linear-gradient(135deg,#3b82f6,#8b5cf6)', display:'flex', alignItems:'center', justifyContent:'center', flexShrink:0, color:'white' }}>
              <IconBolt />
            </div>
            {!collapsed && (
              <div>
                <div style={{ fontSize:14, fontWeight:700, color:'#f1f5f9', letterSpacing:'-0.3px' }}>
                  Predict<span style={{ color:'#3b82f6' }}>Ops</span>
                </div>
                <div style={{ fontSize:10, color:'#475569', fontWeight:500, letterSpacing:'0.5px', marginTop:1 }}>AI MAINTENANCE</div>
              </div>
            )}
          </div>

          {/* Nav */}
          <nav style={{ padding:'12px 8px', display:'flex', flexDirection:'column', gap:4, flex:1 }}>
            {collapsed ? (
              <>
                <NavLink to="/" end title="Command Center" style={({ isActive }) => ({ display:'flex', alignItems:'center', justifyContent:'center', height:38, borderRadius:8, color: isActive ? '#3b82f6' : '#64748b', background: isActive ? 'rgba(59,130,246,0.12)' : 'transparent', border: isActive ? '1px solid rgba(59,130,246,0.2)' : '1px solid transparent' })}><IconDash /></NavLink>
                <NavLink to="/workorders" title="Work Orders" style={({ isActive }) => ({ display:'flex', alignItems:'center', justifyContent:'center', height:38, borderRadius:8, color: isActive ? '#3b82f6' : '#64748b', background: isActive ? 'rgba(59,130,246,0.12)' : 'transparent', border: isActive ? '1px solid rgba(59,130,246,0.2)' : '1px solid transparent' })}><IconWO /></NavLink>
                <NavLink to="/oee" title="OEE Loss Analysis" style={({ isActive }) => ({ display:'flex', alignItems:'center', justifyContent:'center', height:38, borderRadius:8, color: isActive ? '#3b82f6' : '#64748b', background: isActive ? 'rgba(59,130,246,0.12)' : 'transparent', border: isActive ? '1px solid rgba(59,130,246,0.2)' : '1px solid transparent' })}><IconLoss /></NavLink>
                <NavLink to="/investigate" title="Investigation" style={({ isActive }) => ({ display:'flex', alignItems:'center', justifyContent:'center', height:38, borderRadius:8, color: isActive ? '#3b82f6' : '#64748b', background: isActive ? 'rgba(59,130,246,0.12)' : 'transparent', border: isActive ? '1px solid rgba(59,130,246,0.2)' : '1px solid transparent' })}><IconSearch /></NavLink>
                <NavLink to="/model" title="Model Transparency" style={({ isActive }) => ({ display:'flex', alignItems:'center', justifyContent:'center', height:38, borderRadius:8, color: isActive ? '#3b82f6' : '#64748b', background: isActive ? 'rgba(59,130,246,0.12)' : 'transparent', border: isActive ? '1px solid rgba(59,130,246,0.2)' : '1px solid transparent' })}><IconModel /></NavLink>
                <NavLink to="/your-data" title="Bring Your Own Data" style={({ isActive }) => ({ display:'flex', alignItems:'center', justifyContent:'center', height:38, borderRadius:8, color: isActive ? '#3b82f6' : '#64748b', background: isActive ? 'rgba(59,130,246,0.12)' : 'transparent', border: isActive ? '1px solid rgba(59,130,246,0.2)' : '1px solid transparent' })}><IconUpload /></NavLink>
              </>
            ) : (
              <>
                <NavItem to="/" exact icon={<IconDash />} label="Command Center" />
                <NavItem to="/workorders" icon={<IconWO />} label="Work Orders" />
                <NavItem to="/oee" icon={<IconLoss />} label="OEE Losses" />
                <NavItem to="/investigate" icon={<IconSearch />} label="Investigation" />
                <NavItem to="/model" icon={<IconModel />} label="Model Card" />
                <NavItem to="/your-data" icon={<IconUpload />} label="Bring Your Data" />
              </>
            )}
          </nav>

          {/* Collapse button */}
          <div style={{ padding:'10px 8px', borderTop:'1px solid #1e2d45' }}>
            <button onClick={() => setCollapsed(c => !c)} style={{ background:'transparent', border:'1px solid #1e2d45', borderRadius:8, color:'#475569', cursor:'pointer', padding:'6px', display:'flex', alignItems:'center', justifyContent:'center', width:'100%', transition:'all .15s' }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                {collapsed ? <path d="M9 18l6-6-6-6"/> : <path d="M15 18l-6-6 6-6"/>}
              </svg>
            </button>
          </div>
        </aside>

        {/* ── MAIN ─────────────────────────────────────────────── */}
        <div style={{ flex:1, display:'flex', flexDirection:'column', overflow:'hidden' }}>
          {/* Top bar */}
          <header style={{ height:56, background:'#111827', borderBottom:'1px solid #1e2d45', display:'flex', alignItems:'center', padding:'0 24px', gap:8, flexShrink:0 }}>
            <PageTitle />
            <div style={{ marginLeft:'auto', display:'flex', alignItems:'center', gap:12 }}>
              <span style={{ fontSize:11, color:'#475569', background:'#0a0d14', padding:'3px 10px', borderRadius:6, border:'1px solid #1e2d45' }}>
                Live push · 15s fallback
              </span>
              <AuthMenu />
            </div>
          </header>

          {/* Content */}
          <main style={{ flex:1, overflowY:'auto', overflowX:'hidden', padding:'24px' }}>
            <Suspense fallback={<Spinner />}>
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/machines/:id" element={<MachineDetail />} />
                <Route path="/workorders" element={<WorkOrders />} />
                <Route path="/oee" element={<OeeLosses />} />
                <Route path="/investigate" element={<Investigate />} />
                <Route path="/model" element={<ModelCard />} />
                <Route path="/your-data" element={<BringYourData />} />
              {/* Landing page for the confirmation link in a verification email.
                  Not in the sidebar: it is only ever reached from that link. */}
              <Route path="/verify" element={<Verify />} />
                {/* Catch-all, so a mistyped URL renders the command center rather
                    than a blank page with no navigation. */}
                <Route path="*" element={<Dashboard />} />
              </Routes>
            </Suspense>
          </main>
        </div>
      </div>
    </BrowserRouter>
  );
};

export default App;
