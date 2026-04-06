import React from 'react';
import { Composition } from 'remotion';
import { StatsOverview, StatsOverviewProps } from './components/StatsOverview';
import { StreakFlame, StreakFlameProps } from './components/StreakFlame';
import { RetentionCurve, RetentionCurveProps } from './components/RetentionCurve';
import { RatingDistribution, RatingDistProps } from './components/RatingDistribution';
import { ConceptRetention, ConceptRetentionProps } from './components/ConceptRetention';
import { TopicHealth, TopicHealthProps } from './components/TopicHealth';

export const Root: React.FC = () => {
  return (
    <>
      <Composition
        id="StatsOverview"
        component={StatsOverview}
        durationInFrames={90}
        fps={30}
        width={900}
        height={500}
        defaultProps={{
          currentStreak: 0,
          longestStreak: 0,
          totalReviewDays: 0,
          totalCards: 0,
          totalTopics: 0,
          totalConcepts: 0,
          avgRetention: 0,
        }}
      />
      <Composition
        id="StreakFlame"
        component={StreakFlame}
        durationInFrames={90}
        fps={30}
        width={900}
        height={500}
        defaultProps={{ currentStreak: 0, longestStreak: 0 }}
      />
      <Composition
        id="RetentionCurve"
        component={RetentionCurve}
        durationInFrames={90}
        fps={30}
        width={900}
        height={500}
        defaultProps={{ topics: [] }}
      />
      <Composition
        id="RatingDistribution"
        component={RatingDistribution}
        durationInFrames={90}
        fps={30}
        width={900}
        height={500}
        defaultProps={{ complete: 0, partial: 0, failed: 0 }}
      />
      <Composition
        id="ConceptRetention"
        component={ConceptRetention}
        durationInFrames={90}
        fps={30}
        width={900}
        height={500}
        defaultProps={{ distribution: {}, totalConcepts: 0 }}
      />
      <Composition
        id="TopicHealth"
        component={TopicHealth}
        durationInFrames={90}
        fps={30}
        width={900}
        height={500}
        defaultProps={{ topics: [] }}
      />
    </>
  );
};
