'use client';

import { useEffect, useState } from 'react';

import { api, errorMessage, isAbort } from '@/lib/api';
import { formatCount, formatTimestamp } from '@/lib/format';
import type { HealthResponse } from '@/lib/types';

/**
 * The one status indicator in this application that is allowed to say anything
 * about DataHub, and it says only what GET /api/health reports.
 *
 * There are exactly four renderings:
 *   live + reachable      -> "DataHub LIVE - <gms_url>"            (green)
 *   live + not reachable  -> "DataHub UNREACHABLE - <gms_url>"     (red)
 *   replay                -> "DataHub REPLAY - recorded <ts>"      (amber)
 *   /api/health failed    -> "backend unreachable"                 (red)
 *
 * There is no fifth rendering, and in particular there is no hardcoded
 * "connected" state. If we do not know, the chip says we do not know.
 */

const POLL_INTERVAL_MS = 20_000;

interface ChipState {
  health: HealthResponse | null;
  error: string | null;
  loaded: boolean;
}

export default function DataHubStatusChip() {
  const [state, setState] = useState<ChipState>({ health: null, error: null, loaded: false });

  useEffect(() => {
    let disposed = false;
    let controller: AbortController | null = null;

    const poll = async () => {
      controller?.abort();
      controller = new AbortController();
      try {
        const health = await api.health(controller.signal);
        if (!disposed) setState({ health, error: null, loaded: true });
      } catch (cause) {
        if (disposed || isAbort(cause)) return;
        setState({ health: null, error: errorMessage(cause), loaded: true });
      }
    };

    void poll();
    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);

    return () => {
      disposed = true;
      controller?.abort();
      window.clearInterval(timer);
    };
  }, []);

  if (!state.loaded) {
    return (
      <div className="dh-chip is-unknown" aria-live="polite">
        <span className="dh-dot" aria-hidden="true" />
        <span>
          <span className="dh-label">CHECKING…</span>
          <span className="dh-value">GET /api/health</span>
        </span>
      </div>
    );
  }

  if (!state.health) {
    return (
      <div className="dh-chip is-down" aria-live="polite" title={state.error ?? undefined}>
        <span className="dh-dot" aria-hidden="true" />
        <span>
          <span className="dh-label">BACKEND UNREACHABLE</span>
          <span className="dh-value">{state.error ?? 'GET /api/health failed'}</span>
        </span>
      </div>
    );
  }

  const { datahub } = state.health;

  if (datahub.mode === 'replay') {
    return (
      <div className="dh-chip is-replay" aria-live="polite" title={datahub.detail}>
        <span className="dh-dot" aria-hidden="true" />
        <span>
          <span className="dh-label">DataHub REPLAY</span>
          <span className="dh-value">
            recorded {datahub.recorded_at ? formatTimestamp(datahub.recorded_at) : 'timestamp not reported'}
          </span>
          <span className="dh-value">
            {datahub.entity_count === null
              ? 'entity count not reported'
              : `${formatCount(datahub.entity_count)} entities in cassette`}
          </span>
        </span>
      </div>
    );
  }

  if (!datahub.reachable) {
    return (
      <div className="dh-chip is-down" aria-live="polite" title={datahub.detail}>
        <span className="dh-dot" aria-hidden="true" />
        <span>
          <span className="dh-label">DataHub UNREACHABLE</span>
          <span className="dh-value">{datahub.gms_url}</span>
          {datahub.detail ? <span className="dh-value">{datahub.detail}</span> : null}
        </span>
      </div>
    );
  }

  return (
    <div className="dh-chip is-live" aria-live="polite" title={datahub.detail}>
      <span className="dh-dot" aria-hidden="true" />
      <span>
        <span className="dh-label">DataHub LIVE</span>
        <span className="dh-value">{datahub.gms_url}</span>
        {datahub.entity_count !== null ? (
          <span className="dh-value">{formatCount(datahub.entity_count)} entities</span>
        ) : null}
      </span>
    </div>
  );
}
