import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame, spring, useVideoConfig } from 'remotion';

export interface TopicHealthItem {
  name: string;
  avgRetention: number;   // 0–100
  minRetention: number;   // 0–100
  dueCount: number;
  totalConcepts: number;
}

export interface TopicHealthProps {
  topics: TopicHealthItem[];
}

function retentionColor(pct: number): string {
  if (pct >= 80) return '#10b981';
  if (pct >= 60) return '#06b6d4';
  if (pct >= 40) return '#f59e0b';
  if (pct >= 20) return '#f97316';
  return '#ef4444';
}

export const TopicHealth: React.FC<TopicHealthProps> = ({ topics }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const titleOpacity = interpolate(frame, [0, 18], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const titleY = interpolate(frame, [0, 18], [10, 0], {
    extrapolateRight: 'clamp',
  });

  const display = topics.slice(0, 6);

  if (display.length === 0) {
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
        No topic data yet
      </AbsoluteFill>
    );
  }

  return (
    <AbsoluteFill
      style={{
        background: 'linear-gradient(145deg, #0f172a 0%, #1e293b 100%)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '32px 48px',
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
        Topic Memory Health
      </div>

      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 16,
          justifyContent: 'center',
          maxWidth: 820,
        }}
      >
        {display.map((topic, i) => {
          const delay = 10 + i * 7;
          const cardScale = spring({
            frame: Math.max(0, frame - delay),
            fps,
            config: { damping: 14, stiffness: 100 },
          });
          const cardOpacity = interpolate(frame, [delay, delay + 12], [0, 1], {
            extrapolateRight: 'clamp',
          });

          const avgColor = retentionColor(topic.avgRetention);
          const minColor = retentionColor(topic.minRetention);

          // Circular progress for avg retention
          const circR = 28;
          const circum = 2 * Math.PI * circR;
          const retProgress = interpolate(frame, [delay + 5, delay + 30], [0, 1], {
            extrapolateRight: 'clamp',
          });
          const retVal = topic.avgRetention * retProgress;
          const dashOffset = circum * (1 - retVal / 100);

          const retSpring = spring({
            frame: Math.max(0, frame - delay - 5),
            fps,
            config: { damping: 20, stiffness: 60 },
          });
          const displayRet = Math.round(topic.avgRetention * retSpring);

          return (
            <div
              key={i}
              style={{
                width: 240,
                background: '#0f172a',
                border: '1px solid #334155',
                borderRadius: 14,
                padding: '20px 18px',
                opacity: cardOpacity,
                transform: `scale(${cardScale})`,
                display: 'flex',
                flexDirection: 'column',
                gap: 12,
              }}
            >
              {/* Topic name */}
              <div
                style={{
                  fontSize: 13,
                  fontWeight: 600,
                  color: '#e2e8f0',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  lineHeight: 1.3,
                }}
              >
                {topic.name}
              </div>

              {/* Ring + stats row */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
                {/* SVG ring */}
                <div style={{ position: 'relative', width: 66, height: 66, flexShrink: 0 }}>
                  <svg width={66} height={66}>
                    <circle
                      cx={33}
                      cy={33}
                      r={circR}
                      fill="none"
                      stroke="#1e293b"
                      strokeWidth={5}
                    />
                    <circle
                      cx={33}
                      cy={33}
                      r={circR}
                      fill="none"
                      stroke={avgColor}
                      strokeWidth={5}
                      strokeLinecap="round"
                      strokeDasharray={circum}
                      strokeDashoffset={dashOffset}
                      transform="rotate(-90 33 33)"
                      style={{ filter: `drop-shadow(0 0 4px ${avgColor}60)` }}
                    />
                  </svg>
                  <div
                    style={{
                      position: 'absolute',
                      inset: 0,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: 15,
                      fontWeight: 800,
                      color: avgColor,
                    }}
                  >
                    {displayRet}%
                  </div>
                </div>

                {/* Stat labels */}
                <div style={{ display: 'flex', flexDirection: 'column', gap: 5, flex: 1 }}>
                  <div style={{ fontSize: 11, color: '#64748b' }}>
                    Weakest{' '}
                    <span style={{ color: minColor, fontWeight: 600 }}>
                      {Math.round(topic.minRetention)}%
                    </span>
                  </div>
                  <div style={{ fontSize: 11, color: '#64748b' }}>
                    Due{' '}
                    <span style={{ color: topic.dueCount > 0 ? '#f59e0b' : '#10b981', fontWeight: 600 }}>
                      {topic.dueCount}/{topic.totalConcepts}
                    </span>
                  </div>
                  <div style={{ fontSize: 11, color: '#64748b' }}>
                    Concepts{' '}
                    <span style={{ color: '#94a3b8', fontWeight: 600 }}>
                      {topic.totalConcepts}
                    </span>
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
