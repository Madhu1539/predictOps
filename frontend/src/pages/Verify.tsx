import React, { useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { verifyEmail } from '../services/api';

/**
 * Landing page for the confirmation link in a verification email.
 *
 * The link points here rather than at the API because a person opens it in a
 * browser: hitting the endpoint directly would return raw JSON. This page performs
 * the POST and reports the outcome.
 */
type State = 'working' | 'done' | 'failed';

const Verify: React.FC = () => {
  const [params] = useSearchParams();
  const token = params.get('token') ?? '';

  const [state, setState] = useState<State>('working');
  const [message, setMessage] = useState('Confirming your email address…');

  // Guards against the double invocation of effects under React StrictMode in
  // development, which would otherwise fire two POSTs for one click.
  const attempted = useRef(false);

  useEffect(() => {
    if (attempted.current) return;
    attempted.current = true;

    if (!token) {
      setState('failed');
      setMessage('That link is incomplete. Open the link from your email again.');
      return;
    }

    verifyEmail(token)
      .then(result => {
        setState(result.verified ? 'done' : 'failed');
        setMessage(result.message);
      })
      .catch(() => {
        setState('failed');
        setMessage(
          'That link is not valid or has expired. Links last 24 hours — ' +
          'create the account again to get a new one.',
        );
      });
  }, [token]);

  const accent = state === 'done' ? '#4ade80' : state === 'failed' ? '#f87171' : '#3b82f6';

  return (
    <div style={{ padding: '48px 24px', display: 'flex', justifyContent: 'center' }}>
      <div style={{
        maxWidth: 440, width: '100%', background: '#111827',
        border: '1px solid #1e2d45', borderRadius: 12, padding: 28,
        display: 'flex', flexDirection: 'column', gap: 14, alignItems: 'flex-start',
      }}>
        <div style={{
          width: 40, height: 40, borderRadius: '50%',
          background: `${accent}1f`, border: `1px solid ${accent}55`,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          color: accent, fontSize: 20, fontWeight: 700,
        }}>
          {state === 'done' ? '✓' : state === 'failed' ? '!' : '…'}
        </div>

        <h1 style={{ margin: 0, fontSize: 18, color: '#f1f5f9' }}>
          {state === 'done' ? 'Email confirmed'
            : state === 'failed' ? 'Could not confirm'
              : 'Confirming…'}
        </h1>

        <p style={{ margin: 0, fontSize: 13, color: '#94a3b8', lineHeight: 1.7 }}>
          {message}
        </p>

        {state === 'done' && (
          <p style={{ margin: 0, fontSize: 12, color: '#64748b', lineHeight: 1.7 }}>
            Use <strong style={{ color: '#cbd5e1' }}>Sign in</strong> at the top right
            with the email and password you chose.
          </p>
        )}

        <Link to="/"
          style={{
            background: 'rgba(59,130,246,.14)', border: '1px solid rgba(59,130,246,.3)',
            borderRadius: 6, color: '#3b82f6', padding: '7px 14px',
            fontSize: 12, fontWeight: 600, textDecoration: 'none',
          }}>
          Go to Command Center
        </Link>
      </div>
    </div>
  );
};

export default Verify;
