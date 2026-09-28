import React, { useCallback, useEffect, useState } from 'react';
import { getCurrentUser, login as apiLogin, logout as apiLogout, getToken } from '../services/api';
import type { CurrentUser } from '../types';

/**
 * Sign-in control for the top bar.
 *
 * The backend gates every mutating endpoint behind `require_permission`, but that
 * gate is a no-op while `AUTH_ENABLED=false`. Without this control, turning
 * authentication on made the whole UI read-only with no way to authenticate — so
 * the security model could not actually be switched on.
 *
 * Renders as a compact status chip; the form only appears when it is needed.
 */
const AuthMenu: React.FC = () => {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState('planner');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try { setUser(await getCurrentUser()); }
    catch { setUser(null); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await apiLogin(username, password);
      setPassword(''); setOpen(false);
      await refresh();
    } catch (err) {
      // The backend returns one message for unknown user, inactive user and wrong
      // password alike, so it cannot be used to enumerate accounts. Repeated
      // failures are throttled and answer 429.
      setError(err instanceof Error && err.message.includes('429')
        ? 'Too many attempts. Wait a moment and try again.'
        : 'Sign-in failed. Check the username and password.');
    } finally { setBusy(false); }
  };

  const signOut = async () => { apiLogout(); await refresh(); };

  // Nothing to show when auth is off and no session exists: the demo runs open,
  // and a sign-in button for a gate that is not enforced is just confusing.
  if (user && !user.auth_enabled && !user.authenticated && !getToken()) {
    return (
      <span title="Mutating endpoints are unguarded because AUTH_ENABLED=false"
        style={{ fontSize:11, color:'#475569', background:'#0a0d14', padding:'3px 10px', borderRadius:6, border:'1px solid #1e2d45' }}>
        Auth off
      </span>
    );
  }

  if (user?.authenticated) {
    return (
      <div style={{ display:'flex', alignItems:'center', gap:8 }}>
        <span style={{ fontSize:11, color:'#94a3b8', background:'#0a0d14', padding:'3px 10px', borderRadius:6, border:'1px solid #1e2d45' }}>
          {user.username} · <span style={{ color:'#3b82f6', fontWeight:600 }}>{user.role}</span>
        </span>
        <button onClick={signOut}
          style={{ background:'transparent', border:'1px solid #1e2d45', borderRadius:6, color:'#64748b', cursor:'pointer', padding:'3px 9px', fontSize:11 }}>
          Sign out
        </button>
      </div>
    );
  }

  return (
    <div style={{ position:'relative' }}>
      <button onClick={() => setOpen(o => !o)}
        style={{
          background:'rgba(59,130,246,.1)', border:'1px solid rgba(59,130,246,.28)', borderRadius:6,
          color:'#3b82f6', cursor:'pointer', padding:'4px 12px', fontSize:11, fontWeight:600,
        }}>
        Sign in
      </button>

      {open && (
        <form onSubmit={submit}
          style={{
            position:'absolute', top:32, right:0, width:250, zIndex:50,
            background:'#111827', border:'1px solid #1e2d45', borderRadius:10,
            padding:14, display:'flex', flexDirection:'column', gap:9,
            boxShadow:'0 12px 28px rgba(0,0,0,.45)',
          }}>
          <div style={{ fontSize:12, fontWeight:700, color:'#f1f5f9' }}>Sign in</div>
          <input value={username} onChange={e => setUsername(e.target.value)}
            autoComplete="username" placeholder="Username"
            style={{ background:'#0a0d14', border:'1px solid #1e2d45', borderRadius:6, padding:'6px 9px', color:'#f1f5f9', fontSize:12 }} />
          <input value={password} onChange={e => setPassword(e.target.value)}
            type="password" autoComplete="current-password" placeholder="Password"
            style={{ background:'#0a0d14', border:'1px solid #1e2d45', borderRadius:6, padding:'6px 9px', color:'#f1f5f9', fontSize:12 }} />
          {error && <div style={{ fontSize:10, color:'#f87171' }}>{error}</div>}
          <button type="submit" disabled={busy || !password}
            style={{
              background: busy || !password ? '#1a2234' : 'rgba(59,130,246,.14)',
              border:'1px solid rgba(59,130,246,.3)', borderRadius:6,
              color: busy || !password ? '#475569' : '#3b82f6',
              cursor: busy || !password ? 'not-allowed' : 'pointer',
              padding:'6px', fontSize:12, fontWeight:600,
            }}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
          <div style={{ fontSize:9, color:'#475569', lineHeight:1.5 }}>
            Demo accounts: <strong>planner</strong> (full), <strong>technician</strong>,
            {' '}<strong>viewer</strong> (read-only). Password is whatever
            DEMO_PASSWORD is set to.
          </div>
        </form>
      )}
    </div>
  );
};

export default AuthMenu;
