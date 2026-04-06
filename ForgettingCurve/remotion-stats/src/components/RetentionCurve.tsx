import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame, spring, useVideoConfig } from 'remotion';

export interface RetentionCurveProps {
  topics: {
    name: string;
    a: number;
    k: number;
    reviewCount: number;
    conceptAvgRetention?: number;
  }[];
}

export const RetentionCurve: React.FC<RetentionCurveProps> = ({ topics }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const W = 740;
  const H = 320;
  const PAD = { top: 30, right: 40, bottom: 44, left: 54 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;

  const drawProgress = interpolate(frame, [12, 65], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const titleOpacity = interpolate(frame, [0, 18], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const titleY = interpolate(frame, [0, 18], [10, 0], {
    extrapolateRight: 'clamp',
  });

  const colors = ['#06b6d4', '#10b981', '#8b5cf6', '#f59e0b', '#ec4899', '#ef4444'];

  const displayTopics = topics.slice(0, 5);
  const maxDays = 30;
  const RESOLUTION = 120;

  // Build full path + animated portion + dot position
  const curves = displayTopics.map((t, ti) => {
    const allPoints: { x: number; y: number }[] = [];
    for (let i = 0; i <= RESOLUTION; i++) {
      const day = (i / RESOLUTION) * maxDays;
      const r = t.a + (1 - t.a) * Math.exp(-t.k * day);
      allPoints.push({
        x: PAD.left + (day / maxDays) * plotW,
        y: PAD.top + (1 - r) * plotH,
      });
    }

    // Stagger per topic
    const stagger = ti * 0.08;
    const localProgress = Math.max(0, Math.min(1, (drawProgress - stagger) / (1 - stagger)));
    const visibleCount = Math.floor(localProgress * RESOLUTION);

    const pathD = allPoints
      .slice(0, visibleCount + 1)
      .map((p, j) => `${j === 0 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`)
      .join(' ');

    // Glow path (full, for area fill)
    const fullPathD = allPoints
      .map((p, j) => `${j === 0 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`)
      .join(' ');
    const areaPath =
      fullPathD +
      ` L ${allPoints[allPoints.length - 1].x.toFixed(1)} ${(PAD.top + plotH).toFixed(1)}` +
      ` L ${allPoints[0].x.toFixed(1)} ${(PAD.top + plotH).toFixed(1)} Z`;

    // Dot at current draw position
    const dot = visibleCount >= 0 ? allPoints[Math.min(visibleCount, RESOLUTION)] : null;

    return {
      pathD,
      areaPath,
      color: colors[ti % colors.length],
      name: t.name,
      dot,
      localProgress,
      retention: t.conceptAvgRetention,
    };
  });

  // Threshold line at 80%
  const threshY = PAD.top + (1 - 0.8) * plotH;
  const threshOpacity = interpolate(frame, [6, 22], [0, 0.6], {
    extrapolateRight: 'clamp',
  });

  // Grid fade
  const gridOpacity = interpolate(frame, [0, 15], [0, 0.5], {
    extrapolateRight: 'clamp',
  });

  return (
    <AbsoluteFill
      style={{
        background: 'linear-gradient(145deg, #0f172a 0%, #1e293b 100%)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '32px 40px',
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
          marginBottom: 20,
          letterSpacing: '-0.02em',
        }}
      >
        Retention Curves
      </div>

      <svg width={W} height={H} style={{ overflow: 'visible' }}>
        <defs>
          {curves.map((c, i) => (
            <linearGradient key={`grad-${i}`} id={`area-${i}`} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={c.color} stopOpacity={0.15} />
              <stop offset="100%" stopColor={c.color} stopOpacity={0.0} />
            </linearGradient>
          ))}
        </defs>

        {/* Horizontal grid */}
        {[0, 0.2, 0.4, 0.6, 0.8, 1.0].map((v) => {
          const y = PAD.top + (1 - v) * plotH;
          return (
            <g key={v} opacity={gridOpacity}>
              <line
                x1={PAD.left}
                y1={y}
                x2={PAD.left + plotW}
                y2={y}
                stroke="#334155"
                strokeWidth={0.8}
              />
              <text
                x={PAD.left - 10}
                y={y + 4}
                fill="#64748b"
                fontSize={10}
                textAnchor="end"
                fontFamily="system-ui, sans-serif"
              >
                {Math.round(v * 100)}%
              </text>
            </g>
          );
        })}

        {/* X axis labels */}
        {[0, 5, 10, 15, 20, 25, 30].map((d) => {
          const x = PAD.left + (d / maxDays) * plotW;
          return (
            <text
              key={d}
              x={x}
              y={PAD.top + plotH + 22}
              fill="#64748b"
              fontSize={10}
              textAnchor="middle"
              fontFamily="system-ui, sans-serif"
              opacity={gridOpacity}
            >
              {d}d
            </text>
          );
        })}

        {/* Threshold line */}
        <line
          x1={PAD.left}
          y1={threshY}
          x2={PAD.left + plotW}
          y2={threshY}
          stroke="#ef4444"
          strokeWidth={1.2}
          strokeDasharray="5,4"
          opacity={threshOpacity}
        />
        <text
          x={PAD.left + plotW + 5}
          y={threshY + 4}
          fill="#ef4444"
          fontSize={9}
          fontFamily="system-ui, sans-serif"
          opacity={threshOpacity}
        >
          80%
        </text>

        {/* Area fills (subtle) */}
        {curves.map(
          (c, i) =>
            c.localProgress > 0.3 && (
              <path
                key={`area-${i}`}
                d={c.areaPath}
                fill={`url(#area-${i})`}
                opacity={interpolate(c.localProgress, [0.3, 0.7], [0, 0.4], {
                  extrapolateRight: 'clamp',
                })}
              />
            ),
        )}

        {/* Curve lines */}
        {curves.map((c, i) => (
          <path
            key={`line-${i}`}
            d={c.pathD}
            fill="none"
            stroke={c.color}
            strokeWidth={2.5}
            strokeLinecap="round"
            strokeLinejoin="round"
            opacity={0.9}
          />
        ))}

        {/* Animated dots */}
        {curves.map(
          (c, i) =>
            c.dot &&
            c.localProgress > 0.02 && (
              <g key={`dot-${i}`}>
                <circle
                  cx={c.dot.x}
                  cy={c.dot.y}
                  r={6}
                  fill={c.color}
                  opacity={0.25}
                />
                <circle
                  cx={c.dot.x}
                  cy={c.dot.y}
                  r={3.5}
                  fill={c.color}
                  stroke="#0f172a"
                  strokeWidth={1.5}
                />
              </g>
            ),
        )}
      </svg>

      {/* Legend */}
      <div
        style={{
          display: 'flex',
          gap: 18,
          marginTop: 18,
          flexWrap: 'wrap',
          justifyContent: 'center',
          opacity: interpolate(frame, [45, 60], [0, 1], {
            extrapolateRight: 'clamp',
          }),
        }}
      >
        {displayTopics.map((t, i) => (
          <div
            key={i}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 11,
              color: '#94a3b8',
            }}
          >
            <div
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: colors[i % colors.length],
                boxShadow: `0 0 6px ${colors[i % colors.length]}50`,
              }}
            />
            <span
              style={{
                maxWidth: 130,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {t.name}
            </span>
            {t.conceptAvgRetention != null && (
              <span
                style={{
                  fontSize: 10,
                  color: '#475569',
                  fontWeight: 600,
                }}
              >
                {Math.round(t.conceptAvgRetention)}%
              </span>
            )}
          </div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
