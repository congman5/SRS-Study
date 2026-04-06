import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame, spring, useVideoConfig } from 'remotion';

export interface RatingDistProps {
  complete: number;
  partial: number;
  failed: number;
}

export const RatingDistribution: React.FC<RatingDistProps> = ({ complete, partial, failed }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const total = complete + partial + failed;

  const titleOpacity = interpolate(frame, [0, 18], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const titleY = interpolate(frame, [0, 18], [10, 0], {
    extrapolateRight: 'clamp',
  });

  if (total === 0) {
    return (
      <AbsoluteFill
        style={{
          background: 'linear-gradient(145deg, #0f172a, #1e293b)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontFamily: 'system-ui, sans-serif',
          color: '#475569',
          fontSize: 16,
        }}
      >
        No review data yet
      </AbsoluteFill>
    );
  }

  const data = [
    { label: 'Mastered', value: complete, color: '#10b981' },
    { label: 'Partial', value: partial, color: '#f59e0b' },
    { label: 'Failed', value: failed, color: '#ef4444' },
  ];

  // Donut chart
  const R = 86;
  const r = 54;
  const cx = 110;
  const cy = 110;

  let startAngle = -90;
  const arcs = data.map((d, i) => {
    const pct = d.value / total;
    const sweepAngle = pct * 360;
    const drawPct = interpolate(frame, [8 + i * 8, 38 + i * 8], [0, 1], {
      extrapolateRight: 'clamp',
    });
    const actualSweep = sweepAngle * drawPct;

    const startRad = (startAngle * Math.PI) / 180;
    const endRad = ((startAngle + actualSweep) * Math.PI) / 180;

    const x1 = cx + R * Math.cos(startRad);
    const y1 = cy + R * Math.sin(startRad);
    const x2 = cx + R * Math.cos(endRad);
    const y2 = cy + R * Math.sin(endRad);
    const x3 = cx + r * Math.cos(endRad);
    const y3 = cy + r * Math.sin(endRad);
    const x4 = cx + r * Math.cos(startRad);
    const y4 = cy + r * Math.sin(startRad);

    const largeArc = actualSweep > 180 ? 1 : 0;

    const path =
      actualSweep > 0.1
        ? `M ${x1} ${y1} A ${R} ${R} 0 ${largeArc} 1 ${x2} ${y2} L ${x3} ${y3} A ${r} ${r} 0 ${largeArc} 0 ${x4} ${y4} Z`
        : '';

    const result = { path, color: d.color, startAngle };
    startAngle += sweepAngle;
    return result;
  });

  // Center counter
  const centerSpring = spring({
    frame: Math.max(0, frame - 20),
    fps,
    config: { damping: 18, stiffness: 80 },
  });
  const displayTotal = Math.round(total * centerSpring);

  return (
    <AbsoluteFill
      style={{
        background: 'linear-gradient(145deg, #0f172a 0%, #1e293b 100%)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '36px 48px',
        fontFamily: 'system-ui, -apple-system, sans-serif',
      }}
    >
      <div
        style={{
          opacity: titleOpacity,
          transform: `translateY(${titleY}px)`,
          fontSize: 22,
          fontWeight: 700,
          color: '#e2e8f0',
          marginBottom: 28,
          letterSpacing: '-0.02em',
        }}
      >
        Review Performance
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 48 }}>
        {/* Donut */}
        <div style={{ position: 'relative', width: 220, height: 220 }}>
          <svg width={220} height={220}>
            {arcs.map((a, i) =>
              a.path ? (
                <path
                  key={i}
                  d={a.path}
                  fill={a.color}
                  opacity={0.9}
                  style={{ filter: `drop-shadow(0 0 4px ${a.color}30)` }}
                />
              ) : null,
            )}
          </svg>
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <div
              style={{
                fontSize: 32,
                fontWeight: 800,
                color: '#e2e8f0',
                opacity: interpolate(frame, [25, 40], [0, 1], {
                  extrapolateRight: 'clamp',
                }),
                fontVariantNumeric: 'tabular-nums',
              }}
            >
              {displayTotal}
            </div>
            <div
              style={{
                fontSize: 11,
                color: '#64748b',
                fontWeight: 500,
                opacity: interpolate(frame, [25, 40], [0, 1], {
                  extrapolateRight: 'clamp',
                }),
              }}
            >
              reviews
            </div>
          </div>
        </div>

        {/* Legend with bars */}
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 18,
            opacity: interpolate(frame, [22, 38], [0, 1], {
              extrapolateRight: 'clamp',
            }),
          }}
        >
          {data.map((d, i) => {
            const pct = Math.round((d.value / total) * 100);
            const barW = interpolate(
              frame,
              [28 + i * 6, 50 + i * 6],
              [0, pct * 1.8],
              { extrapolateRight: 'clamp' },
            );

            const countSpring = spring({
              frame: Math.max(0, frame - 28 - i * 6),
              fps,
              config: { damping: 20, stiffness: 80 },
            });
            const displayVal = Math.round(d.value * countSpring);

            return (
              <div key={i}>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    marginBottom: 5,
                  }}
                >
                  <div
                    style={{
                      width: 8,
                      height: 8,
                      borderRadius: '50%',
                      background: d.color,
                      boxShadow: `0 0 6px ${d.color}40`,
                    }}
                  />
                  <span
                    style={{
                      fontSize: 13,
                      color: '#e2e8f0',
                      fontWeight: 600,
                    }}
                  >
                    {d.label}
                  </span>
                  <span
                    style={{
                      fontSize: 12,
                      color: '#64748b',
                      marginLeft: 'auto',
                      fontVariantNumeric: 'tabular-nums',
                    }}
                  >
                    {displayVal} ({pct}%)
                  </span>
                </div>
                <div
                  style={{
                    height: 5,
                    background: '#1e293b',
                    borderRadius: 3,
                    width: 180,
                    overflow: 'hidden',
                    border: '1px solid #33415520',
                  }}
                >
                  <div
                    style={{
                      height: '100%',
                      width: barW,
                      background: `linear-gradient(90deg, ${d.color}cc, ${d.color})`,
                      borderRadius: 3,
                    }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </AbsoluteFill>
  );
};
