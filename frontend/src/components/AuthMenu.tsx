import React, { useCallback, useEffect, useState } from 'react';
import {
  ApiError,
  getCurrentUser,
  getToken,
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
} from '../services/api';
import type { CurrentUser, RegisterResponse } from '../types';

/**
 * Sign-in and create-account control for the top bar.
 *
 * The backend gates every mutating endpoint behind `require_permission`, but that
 * gate is a no-op while `AUTH_ENABLED=false`. Without this control, turning
 * authentication on made the whole UI read-only with no way to authenticate — so
 * the security model could not actually be switched on.
 *
 * Renders as a compact status chip; the panel only appears when it is needed.
 */

const panelStyle: React.CSSProperties = {
  position: 'absolute', top: 32, right: 0, width: 278, zIndex: 50,
  background: '#111827', border: '1px solid #1e2d45', borderRadius: 10,
  padding: 14, display: 'flex', flexDirection: 'column', gap: 9,
  boxShadow: '0 12px 28px rgba(0,0,0,.45)',
};

const inputStyle: React.CSSProperties = {
  background: '#0a0d14', border: '1px solid #1e2d45', borderRadius: 6,
  padding: '6px 9px', color: '#f1f5f9', fontSize: 12, width: '100%',
  boxSizing: 'border-box',
};

const chipStyle: React.CSSProperties = {
  fontSize: 11, color: '#94a3b8', background: '#0a0d14',
  padding: '3px 10px', borderRadius: 6, border: '1px solid #1e2d45',
};

function actionButtonStyle(disabled: boolean): React.CSSProperties {
  return {
    background: disabled ? '#1a2234' : 'rgba(59,130,246,.14)',
    border: '1px solid rgba(59,130,246,.3)', borderRadius: 6,
    color: disabled ? '#475569' : '#3b82f6',
    cursor: disabled ? 'not-allowed' : 'pointer',
    padding: '6px', fontSize: 12, fontWeight: 600,
  };
}

const MIN_PASSWORD_LENGTH = 10;

type Mode = 'signin' | 'register';

const AuthMenu: React.FC = () => {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>('signin');

  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [fullName, setFullName] = useState('');

  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [registered, setRegistered] = useState<RegisterResponse | null>(null);

  const refresh = useCallback(async () => {
    try { setUser(await getCurrentUser()); }
    catch { setUser(null); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const switchMode = (next: Mode) => {
    setMode(next);
    setError(null);
    setRegistered(null);
    setPassword(''); setConfirm('');
  };

  const signIn = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try {
      await apiLogin(identifier.trim(), password);
      setPassword(''); setOpen(false);
      await refresh();
    } catch (err) {
      // The backend returns one message for unknown user, inactive user and wrong
      // password alike, so it cannot be used to enumerate accounts. An
      // unconfirmed address and a throttled caller are distinguishable, and both
      // are actionable, so those are passed through.
      if (err instanceof ApiError && (err.status === 403 || err.status === 429)) {
        setError(err.message);
      } else {
        setError('Sign-in failed. Check the email or username and the password.');
      }
    } finally { setBusy(false); }
  };

  const createAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    // Checked here as well as on the server. The confirm field exists only in this
    // form, so the server has nothing to compare against.
    if (password !== confirm) { setError('The two passwords do not match.'); return; }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }

    setBusy(true);
    try {
      const result = await apiRegister(identifier.trim(), password, fullName);
      setRegistered(result);
      setPassword(''); setConfirm('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create the account.');
    } finally { setBusy(false); }
  };

  const signOut = async () => { apiLogout(); await refresh(); };

  // Nothing to show when auth is off and no session exists: the demo runs open,
  // and a sign-in button for a gate that is not enforced is just confusing.
  if (user && !user.auth_enabled && !user.authenticated && !getToken()) {
    return (
      <span title="Mutating endpoints are unguarded because AUTH_ENABLED=false"
        style={{ ...chipStyle, fontSize: 11, color: '#475569' }}>
        Auth off
      </span>
    );
  }

  if (user?.authenticated) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={chipStyle}>
          {user.username} · <span style={{ color: '#3b82f6', fontWeight: 600 }}>{user.role}</span>
        </span>
        <button onClick={signOut}
          style={{
            background: 'transparent', border: '1px solid #1e2d45', borderRadius: 6,
            color: '#64748b', cursor: 'pointer', padding: '3px 9px', fontSize: 11,
          }}>
          Sign out
        </button>
      </div>
    );
  }

  return (
    <div style={{ position: 'relative' }}>
      <button onClick={() => setOpen(o => !o)}
        style={{
          background: 'rgba(59,130,246,.1)', border: '1px solid rgba(59,130,246,.28)',
          borderRadius: 6, color: '#3b82f6', cursor: 'pointer',
          padding: '4px 12px', fontSize: 11, fontWeight: 600,
        }}>
        Sign in
      </button>

      {open && registered && (
        // Account created. Shown instead of the form so the next step is the only
        // thing on screen.
        <div style={panelStyle}>
          <div style={{ fontSize: 12, fontWeight: 700, color: '#4ade80' }}>
            Account created
          </div>
          <div style={{ fontSize: 11, color: '#cbd5e1', lineHeight: 1.6 }}>
            {registered.message}
          </div>

          {registered.verification_link && (
            // Only present when the server could not send mail. Surfaced rather
            // than hidden, because the account is otherwise unusable with no way
            // to find out why.
            <>
              <a href={registered.verification_link}
                style={{
                  fontSize: 11, color: '#3b82f6', wordBreak: 'break-all',
                  background: '#0a0d14', border: '1px solid #1e2d45',
                  borderRadius: 6, padding: '7px 9px', textDecoration: 'none',
                }}>
                Confirm my email →
              </a>
              <div style={{ fontSize: 9, color: '#64748b', lineHeight: 1.5 }}>
                Email delivery is not configured on this server, so the link is
                shown here instead of being sent.
              </div>
            </>
          )}

          <button onClick={() => switchMode('signin')} style={actionButtonStyle(false)}>
            Back to sign in
          </button>
        </div>
      )}

      {open && !registered && (
        <form onSubmit={mode === 'signin' ? signIn : createAccount} style={panelStyle}>
          <div style={{ display: 'flex', gap: 6 }}>
            {(['signin', 'register'] as Mode[]).map(m => (
              <button key={m} type="button" onClick={() => switchMode(m)}
                style={{
                  flex: 1, padding: '5px', fontSize: 11, fontWeight: 600,
                  borderRadius: 6, cursor: 'pointer',
                  background: mode === m ? 'rgba(59,130,246,.14)' : 'transparent',
                  border: `1px solid ${mode === m ? 'rgba(59,130,246,.3)' : '#1e2d45'}`,
                  color: mode === m ? '#3b82f6' : '#64748b',
                }}>
                {m === 'signin' ? 'Sign in' : 'Create account'}
              </button>
            ))}
          </div>

          <input value={identifier} onChange={e => setIdentifier(e.target.value)}
            autoComplete={mode === 'signin' ? 'username' : 'email'}
            type={mode === 'register' ? 'email' : 'text'}
            required
            placeholder={mode === 'signin' ? 'Email or username' : 'you@company.com'}
            style={inputStyle} />

          {mode === 'register' && (
            <input value={fullName} onChange={e => setFullName(e.target.value)}
              autoComplete="name" placeholder="Full name (optional)"
              style={inputStyle} />
          )}

          <input value={password} onChange={e => setPassword(e.target.value)}
            type="password" required
            autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
            placeholder={mode === 'signin' ? 'Password' : `Choose a password (${MIN_PASSWORD_LENGTH}+ characters)`}
            style={inputStyle} />

          {mode === 'register' && (
            <input value={confirm} onChange={e => setConfirm(e.target.value)}
              type="password" autoComplete="new-password" required
              placeholder="Confirm password"
              style={inputStyle} />
          )}

          {error && <div style={{ fontSize: 10, color: '#f87171', lineHeight: 1.5 }}>{error}</div>}

          <button type="submit" disabled={busy || !password || !identifier.trim()}
            style={actionButtonStyle(busy || !password || !identifier.trim())}>
            {busy
              ? (mode === 'signin' ? 'Signing in…' : 'Creating account…')
              : (mode === 'signin' ? 'Sign in' : 'Create account')}
          </button>

          {mode === 'signin' ? (
            <div style={{ fontSize: 9, color: '#475569', lineHeight: 1.5 }}>
              Demo accounts: <strong>planner</strong> (full), <strong>technician</strong>,
              {' '}<strong>viewer</strong> (read-only). Password is whatever
              DEMO_PASSWORD is set to.
            </div>
          ) : (
            <div style={{ fontSize: 9, color: '#475569', lineHeight: 1.5 }}>
              You will get the <strong>planner</strong> role and can upload your own
              factory data. Uploaded datasets are visible to every account on this
              deployment.
            </div>
          )}
        </form>
      )}
    </div>
  );
};

export default AuthMenu;
