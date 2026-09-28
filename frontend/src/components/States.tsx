import React from 'react';

/**
 * Shared loading / error / empty states (spec §47).
 *
 * Styled with the project's CSS custom properties so they match the dark
 * command-center theme used by the pages, rather than introducing a second
 * visual language.
 */

const wrap: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  gap: 12,
  padding: 48,
  textAlign: 'center',
};

export const LoadingState: React.FC<{ message?: string }> = ({ message = 'Loading…' }) => (
  <div style={wrap}>
    <div
      style={{
        width: 26,
        height: 26,
        borderRadius: '50%',
        border: '2.5px solid rgba(59,130,246,.2)',
        borderTopColor: 'var(--blue)',
        animation: 'spin .8s linear infinite',
      }}
    />
    <span style={{ color: 'var(--muted)', fontSize: 13 }}>{message}</span>
    <style>{`@keyframes spin{to{transform:rotate(360deg)}}`}</style>
  </div>
);

export const ErrorState: React.FC<{ message?: string; onRetry?: () => void }> = ({
  message = 'Unable to connect to PredictOps services. Please retry.',
  onRetry,
}) => (
  <div style={wrap}>
    <div
      style={{
        width: 44,
        height: 44,
        borderRadius: '50%',
        background: 'var(--red-glow)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: 20,
        color: 'var(--red)',
      }}
      aria-hidden
    >
      !
    </div>
    <p style={{ color: 'var(--red)', fontWeight: 500, maxWidth: 380 }}>{message}</p>
    {onRetry && (
      <button
        onClick={onRetry}
        style={{
          marginTop: 4,
          padding: '8px 18px',
          borderRadius: 8,
          background: 'var(--surface)',
          border: '1px solid var(--border)',
          color: 'var(--muted2)',
          fontSize: 13,
          cursor: 'pointer',
        }}
      >
        Retry
      </button>
    )}
  </div>
);

export const EmptyState: React.FC<{ message?: string }> = ({
  message = 'No machine data available.',
}) => (
  <div style={wrap}>
    <div
      style={{
        width: 44,
        height: 44,
        borderRadius: '50%',
        background: 'var(--surface2)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: 18,
        color: 'var(--muted)',
      }}
      aria-hidden
    >
      —
    </div>
    <p style={{ color: 'var(--muted)', fontSize: 13 }}>{message}</p>
  </div>
);
