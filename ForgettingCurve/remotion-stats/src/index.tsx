import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Player } from '@remotion/player';
import { StatsOverview } from './components/StatsOverview';
import { StreakFlame } from './components/StreakFlame';
import { RetentionCurve } from './components/RetentionCurve';
import { RatingDistribution } from './components/RatingDistribution';
import { ConceptRetention } from './components/ConceptRetention';
import { TopicHealth } from './components/TopicHealth';

interface StatsData {
  current_streak: number;
  longest_streak: number;
  total_review_days: number;
  total_cards: number;
  total_topics: number;
  heatmap: { date: string; topics: number; cards: number }[];
  topics: {
    name: string;
    a: number;
    k: number;
    review_count: number;
    concept_avg_retention: number | null;
    concept_min_retention: number | null;
    concept_due_count: number;
    concept_total: number;
  }[];
  ratings: { complete: number; partial: number; failed: number };
  concept_retention_distribution: { [key: string]: number };
  total_concepts: number;
}

const StatsDashboard: React.FC = () => {
  const [data, setData] = useState<StatsData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      fetch('/api/stats').then((r) => r.json()),
      fetch('/api/stats/extended').then((r) => r.json()),
    ])
      .then(([stats, extended]) => {
        setData({
          ...stats,
          topics: extended.topics || [],
          ratings: extended.ratings || { complete: 0, partial: 0, failed: 0 },
          concept_retention_distribution:
            extended.concept_retention_distribution || {},
          total_concepts: extended.total_concepts || 0,
        });
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100vh',
          background: '#0f172a',
          color: '#64748b',
          fontFamily: 'system-ui, sans-serif',
          fontSize: 16,
        }}
      >
        <div style={{ textAlign: 'center' }}>
          <div
            style={{
              width: 32,
              height: 32,
              border: '3px solid #334155',
              borderTopColor: '#06b6d4',
              borderRadius: '50%',
              animation: 'spin 0.8s linear infinite',
              margin: '0 auto 12px',
            }}
          />
          Loading statistics...
        </div>
        <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
      </div>
    );
  }

  if (!data) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          height: '100vh',
          background: '#0f172a',
          color: '#ef4444',
          fontFamily: 'system-ui, sans-serif',
          fontSize: 16,
        }}
      >
        Failed to load statistics
      </div>
    );
  }

  const playerStyle: React.CSSProperties = {
    borderRadius: 14,
    overflow: 'hidden',
    boxShadow: '0 4px 24px rgba(0,0,0,0.25)',
    border: '1px solid #334155',
  };

  const sectionLabel: React.CSSProperties = {
    color: '#64748b',
    fontSize: 11,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.1em',
    marginBottom: 10,
    fontFamily: 'system-ui, sans-serif',
  };

  // Compute average retention across all topics for overview
  const topicsWithRetention = data.topics.filter(
    (t) => t.concept_avg_retention != null,
  );
  const avgRetention =
    topicsWithRetention.length > 0
      ? topicsWithRetention.reduce(
          (s, t) => s + (t.concept_avg_retention ?? 0),
          0,
        ) / topicsWithRetention.length
      : 0;

  return (
    <div
      style={{
        minHeight: '100vh',
        background: '#0f172a',
        padding: '32px 20px 48px',
        fontFamily: 'system-ui, -apple-system, sans-serif',
      }}
    >
      <div style={{ maxWidth: 960, margin: '0 auto' }}>
        {/* Header */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            marginBottom: 36,
          }}
        >
          <div>
            <h1
              style={{
                color: '#e2e8f0',
                fontSize: 28,
                fontWeight: 800,
                margin: 0,
                letterSpacing: '-0.02em',
              }}
            >
              Study Statistics
            </h1>
            <p
              style={{
                color: '#475569',
                fontSize: 13,
                margin: '6px 0 0',
              }}
            >
              Animated insights into your learning progress
            </p>
          </div>
          <a
            href="/"
            style={{
              color: '#64748b',
              textDecoration: 'none',
              fontSize: 13,
              padding: '7px 14px',
              border: '1px solid #334155',
              borderRadius: 8,
              transition: 'all 0.15s',
              fontWeight: 500,
            }}
            onMouseOver={(e) => {
              (e.target as HTMLElement).style.borderColor = '#06b6d4';
              (e.target as HTMLElement).style.color = '#e2e8f0';
            }}
            onMouseOut={(e) => {
              (e.target as HTMLElement).style.borderColor = '#334155';
              (e.target as HTMLElement).style.color = '#64748b';
            }}
          >
            ← Dashboard
          </a>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 28 }}>
          {/* Stats Overview — full width */}
          <div>
            <h2 style={sectionLabel}>Overview</h2>
            <Player
              component={StatsOverview}
              inputProps={{
                currentStreak: data.current_streak,
                longestStreak: data.longest_streak,
                totalReviewDays: data.total_review_days,
                totalCards: data.total_cards,
                totalTopics: data.total_topics,
                totalConcepts: data.total_concepts,
                avgRetention,
              }}
              durationInFrames={54000}
              fps={30}
              compositionWidth={900}
              compositionHeight={500}
              style={{ ...playerStyle, width: '100%', aspectRatio: '9/5' }}
              autoPlay
            />
          </div>

          {/* Two-column: Streak + Rating Distribution */}
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: '1fr 1fr',
              gap: 24,
            }}
          >
            <div>
              <h2 style={sectionLabel}>Current Streak</h2>
              <Player
                component={StreakFlame}
                inputProps={{
                  currentStreak: data.current_streak,
                  longestStreak: data.longest_streak,
                }}
                durationInFrames={54000}
                fps={30}
                compositionWidth={900}
                compositionHeight={500}
                style={{
                  ...playerStyle,
                  width: '100%',
                  aspectRatio: '9/5',
                }}
                autoPlay
              />
            </div>

            <div>
              <h2 style={sectionLabel}>Review Performance</h2>
              <Player
                component={RatingDistribution}
                inputProps={data.ratings}
                durationInFrames={54000}
                fps={30}
                compositionWidth={900}
                compositionHeight={500}
                style={{
                  ...playerStyle,
                  width: '100%',
                  aspectRatio: '9/5',
                }}
                autoPlay
              />
            </div>
          </div>

          {/* Concept Retention Distribution — full width */}
          {data.total_concepts > 0 && (
            <div>
              <h2 style={sectionLabel}>Concept Retention Distribution</h2>
              <Player
                component={ConceptRetention}
                inputProps={{
                  distribution: data.concept_retention_distribution,
                  totalConcepts: data.total_concepts,
                }}
                durationInFrames={54000}
                fps={30}
                compositionWidth={900}
                compositionHeight={500}
                style={{
                  ...playerStyle,
                  width: '100%',
                  aspectRatio: '9/5',
                }}
                autoPlay
              />
            </div>
          )}

          {/* Topic Health — full width */}
          {data.topics.length > 0 && (
            <div>
              <h2 style={sectionLabel}>Topic Memory Health</h2>
              <Player
                component={TopicHealth}
                inputProps={{
                  topics: data.topics.map((t) => ({
                    name: t.name,
                    avgRetention: t.concept_avg_retention ?? 0,
                    minRetention: t.concept_min_retention ?? 0,
                    dueCount: t.concept_due_count,
                    totalConcepts: t.concept_total,
                  })),
                }}
                durationInFrames={54000}
                fps={30}
                compositionWidth={900}
                compositionHeight={500}
                style={{
                  ...playerStyle,
                  width: '100%',
                  aspectRatio: '9/5',
                }}
                autoPlay
              />
            </div>
          )}

          {/* Retention Curves — full width */}
          {data.topics.length > 0 && (
            <div>
              <h2 style={sectionLabel}>Retention Curves</h2>
              <Player
                component={RetentionCurve}
                inputProps={{
                  topics: data.topics.map((t) => ({
                    name: t.name,
                    a: t.a,
                    k: t.k,
                    reviewCount: t.review_count,
                    conceptAvgRetention: t.concept_avg_retention ?? undefined,
                  })),
                }}
                durationInFrames={54000}
                fps={30}
                compositionWidth={900}
                compositionHeight={500}
                style={{
                  ...playerStyle,
                  width: '100%',
                  aspectRatio: '9/5',
                }}
                autoPlay
              />
            </div>
          )}
        </div>

        {/* Footer */}
        <div
          style={{
            textAlign: 'center',
            marginTop: 40,
            color: '#334155',
            fontSize: 11,
            fontFamily: 'system-ui, sans-serif',
          }}
        >
          Powered by Remotion
        </div>
      </div>
    </div>
  );
};

// Mount the React app
const container = document.getElementById('stats-root');
if (container) {
  const root = createRoot(container);
  root.render(<StatsDashboard />);
}
