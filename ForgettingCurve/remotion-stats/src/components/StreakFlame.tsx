import React from 'react';
import { AbsoluteFill, interpolate, useCurrentFrame, spring, useVideoConfig } from 'remotion';

export interface StreakFlameProps {
  currentStreak: number;
  longestStreak: number;
}

export const StreakFlame: React.FC<StreakFlameProps> = ({ currentStreak, longestStreak }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const flameScale = spring({
    frame,
    fps,
    config: { damping: 12, stiffness: 80 },
  });

  // Gentle breathing animation (subtle, not flickery)
  const breathe = 1 + Math.sin(frame * 0.12) * 0.02;

  const countProgress = interpolate(frame, [15, 48], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const displayStreak = Math.round(currentStreak * countProgress);

  const titleOpacity = interpolate(frame, [0, 18], [0, 1], {
    extrapolateRight: 'clamp',
  });
  const titleY = interpolate(frame, [0, 18], [10, 0], {
    extrapolateRight: 'clamp',
  });

  // Flame glow intensity based on streak length
  const intensity = Math.min(1, currentStreak / 30);
  const glowRadius = 16 + intensity * 12 + Math.sin(frame * 0.1) * 3;
  const glowColor = `hsl(${35 + intensity * 8}, 100%, 55%)`;

  const barWidth = longestStreak > 0 ? (currentStreak / longestStreak) * 100 : 0;
  const barProgress = interpolate(frame, [40, 68], [0, barWidth], {
    extrapolateRight: 'clamp',
  });

  // Ring around flame
  const ringProgress = interpolate(frame, [20, 55], [0, barWidth], {
    extrapolateRight: 'clamp',
  });
  const ringR = 64;
  const ringCircum = 2 * Math.PI * ringR;
  const ringDash = ringCircum * (ringProgress / 100);

  return (
    <AbsoluteFill
      style={{
        background: 'linear-gradient(145deg, #0f172a 0%, #1e293b 100%)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 40,
        fontFamily: 'system-ui, -apple-system, sans-serif',
      }}
    >
      {/* Title */}
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
        Study Streak
      </div>

      {/* Flame with ring */}
      <div style={{ position: 'relative', width: 150, height: 150, marginBottom: 12 }}>
        {/* Background ring */}
        <svg
          width={150}
          height={150}
          style={{ position: 'absolute', top: 0, left: 0 }}
        >
          <circle
            cx={75}
            cy={75}
            r={ringR}
            fill="none"
            stroke="#1e293b"
            strokeWidth={5}
          />
          <circle
            cx={75}
            cy={75}
            r={ringR}
            fill="none"
            stroke={glowColor}
            strokeWidth={5}
            strokeLinecap="round"
            strokeDasharray={ringCircum}
            strokeDashoffset={ringCircum - ringDash}
            transform="rotate(-90 75 75)"
            style={{ filter: `drop-shadow(0 0 6px ${glowColor}60)` }}
          />
        </svg>

        {/* Flame emoji */}
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            transform: `scale(${flameScale * breathe})`,
            fontSize: 72,
            lineHeight: 1,
            filter: `drop-shadow(0 0 ${glowRadius}px ${glowColor})`,
          }}
        >
          🔥
        </div>
      </div>

      {/* Streak count */}
      <div
        style={{
          fontSize: 64,
          fontWeight: 900,
          color: '#f59e0b',
          textShadow: '0 0 30px rgba(245,158,11,0.3)',
          opacity: interpolate(frame, [10, 25], [0, 1], {
            extrapolateRight: 'clamp',
          }),
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {displayStreak}
      </div>
      <div
        style={{
          fontSize: 14,
          color: '#fbbf24',
          fontWeight: 600,
          marginTop: 2,
          textTransform: 'uppercase',
          letterSpacing: '0.12em',
          opacity: interpolate(frame, [18, 30], [0, 1], {
            extrapolateRight: 'clamp',
          }),
        }}
      >
        Day Streak
      </div>

      {/* Progress bar toward best */}
      <div
        style={{
          marginTop: 32,
          width: '50%',
          maxWidth: 360,
          opacity: interpolate(frame, [34, 48], [0, 1], {
            extrapolateRight: 'clamp',
          }),
        }}
      >
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            marginBottom: 8,
            fontSize: 12,
            color: '#64748b',
          }}
        >
          <span>Progress to personal best</span>
          <span style={{ color: '#f59e0b', fontWeight: 600 }}>
            {longestStreak} days
          </span>
        </div>
        <div
          style={{
            height: 8,
            background: '#1e293b',
            borderRadius: 4,
            overflow: 'hidden',
            border: '1px solid #334155',
          }}
        >
          <div
            style={{
              height: '100%',
              width: `${barProgress}%`,
              background: `linear-gradient(90deg, #f59e0b, #f97316)`,
              borderRadius: 4,
              boxShadow: '0 0 8px rgba(245,158,11,0.4)',
            }}
          />
        </div>
      </div>
    </AbsoluteFill>
  );
};
