import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame, spring, useVideoConfig } from 'remotion';

interface CounterProps {
  value: number;
  label: string;
  color: string;
  icon: string;
  delay?: number;
  suffix?: string;
}

export const AnimatedCounter: React.FC<CounterProps> = ({
  value,
  label,
  color,
  icon,
  delay = 0,
  suffix = '',
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const adjustedFrame = Math.max(0, frame - delay);
  const scale = spring({
    frame: adjustedFrame,
    fps,
    config: { damping: 14, stiffness: 90 },
  });
  const countProgress = interpolate(adjustedFrame, [0, 36], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const displayValue = Math.round(value * countProgress);
  const opacity = interpolate(adjustedFrame, [0, 12], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const translateY = interpolate(adjustedFrame, [0, 12], [8, 0], {
    extrapolateRight: 'clamp',
  });

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        transform: `scale(${scale}) translateY(${translateY}px)`,
        opacity,
        padding: '14px 18px',
        background: '#0f172a80',
        borderRadius: 16,
        border: '1px solid #334155',
        minWidth: 130,
      }}
    >
      <div style={{ fontSize: 36, marginBottom: 6, lineHeight: 1 }}>{icon}</div>
      <div
        style={{
          fontSize: 48,
          fontWeight: 800,
          color,
          fontFamily: 'system-ui, -apple-system, sans-serif',
          lineHeight: 1,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {displayValue.toLocaleString()}
        {suffix && (
          <span style={{ fontSize: 22, fontWeight: 600, opacity: 0.8 }}>{suffix}</span>
        )}
      </div>
      <div
        style={{
          fontSize: 12,
          color: '#64748b',
          fontWeight: 600,
          marginTop: 6,
          fontFamily: 'system-ui, -apple-system, sans-serif',
          textTransform: 'uppercase',
          letterSpacing: '0.1em',
        }}
      >
        {label}
      </div>
    </div>
  );
};

export interface StatsOverviewProps {
  currentStreak: number;
  longestStreak: number;
  totalReviewDays: number;
  totalCards: number;
  totalTopics: number;
  totalConcepts?: number;
  avgRetention?: number;
}

export const StatsOverview: React.FC<StatsOverviewProps> = (props) => {
  const frame = useCurrentFrame();
  const bgOpacity = interpolate(frame, [0, 15], [0, 1], {
    extrapolateRight: 'clamp',
  });

  const titleOpacity = interpolate(frame, [0, 20], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const titleY = interpolate(frame, [0, 20], [12, 0], {
    extrapolateRight: 'clamp',
  });

  return (
    <AbsoluteFill
      style={{
        background: `linear-gradient(145deg, #0f172a ${bgOpacity * 100}%, #1e293b)`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexDirection: 'column',
        padding: '32px 40px',
      }}
    >
      <div
        style={{
          fontSize: 24,
          fontWeight: 700,
          color: '#e2e8f0',
          marginBottom: 32,
          fontFamily: 'system-ui, -apple-system, sans-serif',
          opacity: titleOpacity,
          transform: `translateY(${titleY}px)`,
          letterSpacing: '-0.02em',
        }}
      >
        Your Study Statistics
      </div>

      <div
        style={{
          display: 'flex',
          gap: 16,
          flexWrap: 'wrap',
          justifyContent: 'center',
        }}
      >
        <AnimatedCounter
          value={props.currentStreak}
          label="Day Streak"
          color="#f59e0b"
          icon="🔥"
          delay={5}
        />
        <AnimatedCounter
          value={props.totalReviewDays}
          label="Review Days"
          color="#06b6d4"
          icon="📅"
          delay={12}
        />
        <AnimatedCounter
          value={props.totalCards}
          label="Total Cards"
          color="#10b981"
          icon="🃏"
          delay={19}
        />
        <AnimatedCounter
          value={props.totalTopics}
          label="Topics"
          color="#8b5cf6"
          icon="📚"
          delay={26}
        />
        {(props.totalConcepts ?? 0) > 0 && (
          <AnimatedCounter
            value={props.totalConcepts ?? 0}
            label="Concepts"
            color="#ec4899"
            icon="🧠"
            delay={33}
          />
        )}
        {(props.avgRetention ?? 0) > 0 && (
          <AnimatedCounter
            value={Math.round(props.avgRetention ?? 0)}
            label="Avg Retention"
            color="#10b981"
            icon="💡"
            delay={40}
            suffix="%"
          />
        )}
      </div>

      <div
        style={{
          marginTop: 28,
          display: 'flex',
          gap: 14,
          opacity: interpolate(frame, [42, 56], [0, 1], {
            extrapolateRight: 'clamp',
          }),
        }}
      >
        <div
          style={{
            background: '#0f172a',
            border: '1px solid #334155',
            borderRadius: 10,
            padding: '8px 18px',
            color: '#64748b',
            fontSize: 12,
            fontFamily: 'system-ui, sans-serif',
          }}
        >
          Best Streak:{' '}
          <span style={{ color: '#f59e0b', fontWeight: 700 }}>
            {props.longestStreak} days
          </span>
        </div>
      </div>
    </AbsoluteFill>
  );
};
