'use client';

import { TemperatureMeter, TemperaturePill } from '@/components/Primitives';
import type { TemperatureBreakdown } from '@/lib/types';

const COMPONENTS: Array<{ key: keyof TemperatureBreakdown; label: string }> = [
  { key: 'recency_component', label: 'Recency' },
  { key: 'frequency_component', label: 'Frequency' },
  { key: 'downstream_component', label: 'Downstream' },
  { key: 'criticality_component', label: 'Criticality' },
];

/**
 * The temperature score, broken into the four components the backend actually
 * computed. Bars are scaled against the sum of the components so their relative
 * weight is visible, and both the sum and the reported score are printed -- if
 * they ever disagree the reader sees it rather than being told a tidy story.
 */
export default function TemperaturePanel({
  temperature,
}: {
  temperature: TemperatureBreakdown;
}) {
  const values = COMPONENTS.map(({ key, label }) => ({
    label,
    value: Number(temperature[key] ?? 0),
  }));
  const total = values.reduce((sum, item) => sum + Math.max(0, item.value), 0);
  const scale = total > 0 ? total : 1;
  const inputs = Object.entries(temperature.inputs ?? {});

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <div className="row">
          <TemperaturePill classification={temperature.classification} />
          <TemperatureMeter temperature={temperature} />
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 26, fontWeight: 650, lineHeight: 1.1 }}>
            {temperature.score.toFixed(1)}
          </div>
          <div className="muted" style={{ fontSize: 13 }}>
            score / 100
          </div>
        </div>
      </div>

      <div className="component-list">
        {values.map((item) => (
          <div className="component" key={item.label}>
            <span className="dim">{item.label}</span>
            <span className="component-track">
              <span
                className="component-fill"
                style={{ width: `${Math.max(0, Math.min(100, (item.value / scale) * 100))}%` }}
              />
            </span>
            <span className="tnum" style={{ textAlign: 'right' }}>
              {item.value.toFixed(1)}
            </span>
          </div>
        ))}
      </div>

      <div className="muted" style={{ fontSize: 13 }}>
        components sum to {total.toFixed(1)} · reported score {temperature.score.toFixed(1)}
      </div>

      {inputs.length > 0 ? (
        <details className="expander">
          <summary>What fed this score ({inputs.length} inputs)</summary>
          <div className="expander-body">
            <dl className="kv">
              {inputs.map(([key, value]) => (
                <div key={key} style={{ display: 'contents' }}>
                  <dt className="mono">{key}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          </div>
        </details>
      ) : (
        <div className="muted" style={{ fontSize: 13 }}>
          The backend reported no named inputs for this score.
        </div>
      )}
    </div>
  );
}
