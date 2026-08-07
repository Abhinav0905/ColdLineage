'use client';

import { RefreshCw, XCircle } from 'lucide-react';
import { useEffect } from 'react';

/**
 * Route-level error boundary. A thrown render error shows the message and a
 * way back instead of a white screen.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surface it for anyone watching the console during a demo.
    console.error('[ColdLineage] route error', error);
  }, [error]);

  return (
    <div className="stack" style={{ maxWidth: 720 }}>
      <div className="page-head">
        <div>
          <div className="eyebrow">Interface error</div>
          <h1 className="page-title">This screen failed to render</h1>
          <p className="page-lede">
            The data behind this page is unchanged. Nothing was executed. Retry the render, or
            check that the ColdLineage backend is running.
          </p>
        </div>
      </div>

      <div className="error-box" role="alert">
        <div className="error-title">
          <XCircle size={16} color="var(--critical)" aria-hidden="true" />
          {error.name || 'Error'}
        </div>
        <div className="error-body">{error.message || 'No message was attached to this error.'}</div>
        {error.digest ? <div className="error-body">digest {error.digest}</div> : null}
      </div>

      <div className="row">
        <button type="button" className="btn primary" onClick={reset}>
          <RefreshCw size={15} aria-hidden="true" />
          Try again
        </button>
      </div>
    </div>
  );
}
