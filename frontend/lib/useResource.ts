'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { errorMessage, isAbort } from './api';

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** True while a refetch is in flight over data we already have. */
  refreshing: boolean;
  reload: () => void;
}

/**
 * Load a value from the API and keep it in component state.
 *
 * On refetch the previous value is held (never cleared to a skeleton) so the
 * layout does not jump, and an error never wipes the last good render -- it is
 * surfaced alongside it.
 */
export function useResource<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  deps: unknown[],
): Resource<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [nonce, setNonce] = useState(0);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const hasDataRef = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    if (hasDataRef.current) setRefreshing(true);
    else setLoading(true);

    loaderRef
      .current(controller.signal)
      .then((value) => {
        if (cancelled) return;
        hasDataRef.current = true;
        setData(value);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (cancelled || isAbort(cause)) return;
        setError(errorMessage(cause));
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
        setRefreshing(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  return { data, error, loading, refreshing, reload };
}
