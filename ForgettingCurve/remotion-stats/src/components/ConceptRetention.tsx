import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame, spring, useVideoConfig } from 'remotion';

export interface ConceptRetentionProps {
  distribution: { [bucket: string]: number };
  totalConcepts: number;
}

const BUCKETS = [
  { key: '80-100', label: '80–100%', color: '#10b981', emoji: '🟢' },
  { key: '60-80',  label: '60–80%',  color: '#06b6d4', emoji: '🔵' },
  { key: '40-60',  label: '40–60%',  color: '#f59e0b', emoji: '🟡' },
  { key: '20-40',  label: '20–40%',  color: '#f97316', emoji: '🟠' },
  { key: '0-20',   label: '0–20%',   color: '#ef4444', emoji: '🔴' },
];

export const ConceptRetention: React.FC<ConceptRetentionProps> = ({
  distribution,
  totalConcepts,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const titleOpacity = interpolate(frame, [0, 20], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const titleY = interpolate(frame, [0, 20], [12, 0], {
    extrapolateRight: 'clamp',
  });

  const maxCount = Math.max(1, ...BUCKETS.map(b => distribution[b.key] || 0));

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
      {/* Title */}
      <div
        style={{
          opacity: titleOpacity,
          transform: `translateY(${titleY}px)`,
          marginBottom: 8,
          fontSize: 22,
          fontWeight: 700,
          color: '#e2e8f0',
          letterSpacing: '-0.02em',
        }}
      >
        Concept Retention Health
      </div>
      <div
        style={{
          opacity: titleOpacity,
          fontSize: 13,
          color: '#64748b',
          marginBottom: 32,
        }}
      >
        {totalConcepts} concepts across all topics
      </div>

      {/* Bars */}
      <div style={{ width: '100%', maxWidth: 680, display: 'flex', flexDirection: 'column', gap: 14 }}>
        {BUCKETS.map((bucket, i) => {
          const count = distribution[bucket.key] || 0;
          const pct = totalConcepts > 0 ? Math.round((count / totalConcepts) * 100) : 0;
          const barMaxWidth = 100;
          const targetWidth = maxCount > 0 ? (count / maxCount) * barMaxWidth : 0;

          const entryDelay = 12 + i * 6;
          const barProgress = interpolate(frame, [entryDelay, entryDelay + 25], [0, 1], {
            extrapolateRight: 'clamp',
          });
          const barWidth = targetWidth * barProgress;

          const rowOpacity = interpolate(frame, [entryDelay - 4, entryDelay + 8], [0, 1], {
            extrapolateRight: 'clamp',
          });
          const rowX = interpolate(frame, [entryDelay - 4, entryDelay + 8], [-16, 0], {
            extrapolateRight: 'clamp',
          });

          const countSpring = spring({
            frame: Math.max(0, frame - entryDelay),
            fps,
            config: { damping: 18, stiffness: 80 },
          });
          const displayCount = Math.round(count * countSpring);

          return (
            <div
              key={bucket.key}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 12,
                opacity: rowOpacity,
                transform: `translateX(${rowX}px)`,
              }}
            >
              {/* Label */}
              <div
                style={{
                  width: 90,
                  fontSize: 13,
                  fontWeight: 600,
                  color: bucket.color,
                  textAlign: 'right',
                  flexShrink: 0,
                }}
              >
                {bucket.label}
              </div>

              {/* Bar track */}
              <div
                style={{
                  flex: 1,
                  height: 28,
                  background: '#1e293b',
                  borderRadius: 8,
                  overflow: 'hidden',
                  border: '1px solid #334155',
                  position: 'relative',
                }}
              >
                <div
                  style={{
                    height: '100%',
                    width: `${barWidth}%`,
                    background: `linear-gradient(90deg, ${bucket.color}cc, ${bucket.color})`,
                    borderRadius: 7,
                    boxShadow: `0 0 12px ${bucket.color}30`,
                    transition: 'none',
                  }}
                />
              </div>

              {/* Count */}
              <div
                style={{
                  width: 64,
                  fontSize: 13,
                  color: '#94a3b8',
                  fontWeight: 500,
                  fontVariantNumeric: 'tabular-nums',
                }}
              >
                {displayCount}{' '}
                <span style={{ color: '#475569', fontSize: 11 }}>({pct}%)</span>
              </div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
