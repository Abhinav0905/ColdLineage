/**
 * Thin typed client for the ColdLineage backend.
 *
 * Two rules this file exists to enforce:
 *   1. Nothing is cached. Every screen shows what the backend says right now.
 *   2. A failed call throws an ApiError carrying the status and the raw
 *      `detail` payload, so a 409 can render the verdict/blocker list the
 *      backend refused on instead of a generic "something went wrong".
 */

import type {
  ArchivePlan,
  AuditRow,
  DatasetDetail,
  DatasetSummary,
  ExecuteResponse,
  HealthResponse,
  RangeVerdict,
  RestoreResponse,
  RunRow,
} from './types';

/** Every route below is relative to this, so it must end at `/api`.
 *
 *  Platforms hand you the service root and nothing more — Render's
 *  RENDER_EXTERNAL_URL is `https://name.onrender.com` — so accept either form and
 *  normalise. Without this, a deploy wired straight from the platform's own
 *  variable calls `/datasets` instead of `/api/datasets` and every request 404s,
 *  with nothing in the UI to suggest why. */
function resolveBase(raw: string | undefined): string {
  const url = (raw || 'http://localhost:8000/api').replace(/\/+$/, '');
  return /\/api$/.test(url) ? url : `${url}/api`;
}

export const API_BASE = resolveBase(process.env.NEXT_PUBLIC_API_URL);

export class ApiError extends Error {
  readonly status: number;
  /** The parsed `detail` field, verbatim. May be a string, object, or array. */
  readonly detail: unknown;

  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** True when the request failed before reaching the backend at all. */
export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NetworkError';
  }
}

function describeDetail(detail: unknown, status: number): string {
  if (typeof detail === 'string' && detail.trim() !== '') return detail;
  if (detail && typeof detail === 'object') {
    const record = detail as Record<string, unknown>;
    if (typeof record.message === 'string') return record.message;
    if (typeof record.rationale === 'string') return record.rationale;
    try {
      return JSON.stringify(detail);
    } catch {
      /* fall through */
    }
  }
  return `Request failed with HTTP ${status}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      cache: 'no-store',
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init?.body ? { 'Content-Type': 'application/json' } : null),
        ...init?.headers,
      },
    });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw new NetworkError(
      `Cannot reach the ColdLineage backend at ${API_BASE}${path}`,
    );
  }

  const raw = await response.text();
  let parsed: unknown = null;
  if (raw.length > 0) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = raw;
    }
  }

  if (!response.ok) {
    const detail =
      parsed && typeof parsed === 'object' && 'detail' in parsed
        ? (parsed as { detail: unknown }).detail
        : parsed;
    throw new ApiError(
      response.status,
      detail,
      describeDetail(detail, response.status),
    );
  }

  return parsed as T;
}

function post<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    body: JSON.stringify(body),
    signal,
  });
}

export const api = {
  health: (signal?: AbortSignal) =>
    request<HealthResponse>('/health', { signal }),

  datasets: (signal?: AbortSignal) =>
    request<DatasetSummary[]>('/datasets', { signal }),

  dataset: (id: number, signal?: AbortSignal) =>
    request<DatasetDetail>(`/datasets/${id}`, { signal }),

  simulate: (id: number, cutoffDate: string, signal?: AbortSignal) =>
    post<RangeVerdict>(`/datasets/${id}/simulate`, { cutoff_date: cutoffDate }, signal),

  plan: (id: number, cutoffDate: string, signal?: AbortSignal) =>
    post<ArchivePlan>(`/datasets/${id}/plan`, { cutoff_date: cutoffDate }, signal),

  execute: (planHash: string, approvedBy: string, signal?: AbortSignal) =>
    post<ExecuteResponse>('/execute', { plan_hash: planHash, approved_by: approvedBy }, signal),

  restore: (runId: number, temporary: boolean, signal?: AbortSignal) =>
    post<RestoreResponse>('/restore', { run_id: runId, temporary }, signal),

  runs: (signal?: AbortSignal) => request<RunRow[]>('/runs', { signal }),

  audit: (signal?: AbortSignal) => request<AuditRow[]>('/audit', { signal }),
};

/** Human-readable message for anything thrown by this module. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return `HTTP ${error.status} - ${error.message}`;
  if (error instanceof NetworkError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

/** True for the abort we issue ourselves when a request is superseded. */
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}
