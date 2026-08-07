// Copyright 2011 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "components/viz/common/frame_sinks/refresh_clock.h"

#include <algorithm>
#include <cmath>
#include <numeric>
#include <vector>

#include "testing/gtest/include/gtest/gtest.h"

namespace viz {
namespace {

constexpr double kRefreshHz = 60.0;
constexpr double kBaseIntervalMs = 1000.0 / kRefreshHz;

RefreshClockConfig DefaultConfig() {
  RefreshClockConfig config;
  config.enabled = true;
  config.deterministic = true;
  return config;
}

std::vector<double> Collect(RefreshClock* clock,
                            int count,
                            LoadHint hint = LoadHint::kUnknown) {
  std::vector<double> intervals;
  intervals.reserve(count);
  for (int i = 0; i < count; ++i) {
    intervals.push_back(clock->NextInterval(hint).InMillisecondsF());
  }
  return intervals;
}

double Mean(const std::vector<double>& values) {
  return std::accumulate(values.begin(), values.end(), 0.0) / values.size();
}

double StdDev(const std::vector<double>& values) {
  const double mean = Mean(values);
  double sum_sq = 0.0;
  for (double value : values) {
    sum_sq += (value - mean) * (value - mean);
  }
  return std::sqrt(sum_sq / (values.size() - 1));
}

// Lag-k autocorrelation of the series.
double Autocorrelation(const std::vector<double>& values, size_t lag) {
  const double mean = Mean(values);
  double numerator = 0.0;
  double denominator = 0.0;
  for (size_t i = 0; i < values.size(); ++i) {
    const double centered = values[i] - mean;
    denominator += centered * centered;
    if (i + lag < values.size()) {
      numerator += centered * (values[i + lag] - mean);
    }
  }
  return denominator > 0.0 ? numerator / denominator : 0.0;
}

// Intervals that carry a whole extra frame period are the dropped ones.
size_t CountDrops(const std::vector<double>& intervals) {
  return std::count_if(intervals.begin(), intervals.end(), [](double value) {
    return value > kBaseIntervalMs * 1.5;
  });
}

// PRD 25: same seed produces an identical emitted sequence.
TEST(RefreshClockTest, SameSeedProducesIdenticalSequence) {
  RefreshClock first(12345, kRefreshHz, DefaultConfig());
  RefreshClock second(12345, kRefreshHz, DefaultConfig());

  const std::vector<double> a = Collect(&first, 5000);
  const std::vector<double> b = Collect(&second, 5000);

  EXPECT_EQ(a, b);
}

TEST(RefreshClockTest, ResetForTestingRestartsTheSequence) {
  RefreshClock clock(999, kRefreshHz, DefaultConfig());
  const std::vector<double> first = Collect(&clock, 1000);

  clock.ResetForTesting(999);
  const std::vector<double> second = Collect(&clock, 1000);

  EXPECT_EQ(first, second);
  EXPECT_EQ(1000u, clock.frame_counter());
}

// PRD 25: different seeds produce different sequences.
TEST(RefreshClockTest, DifferentSeedsProduceDifferentSequences) {
  RefreshClock first(1, kRefreshHz, DefaultConfig());
  RefreshClock second(2, kRefreshHz, DefaultConfig());

  const std::vector<double> a = Collect(&first, 20000);
  const std::vector<double> b = Collect(&second, 20000);

  EXPECT_NE(a, b);

  // Not merely different -- statistically independent. A shared-state bug
  // could yield sequences that differ only by an offset.
  //
  // Correlate first differences rather than levels. The level series has long
  // memory by design, so its effective sample size is roughly n/100 and the
  // sample correlation between two independent runs is genuinely noisy: at
  // n=5000 two unrelated seeds routinely show |r| around 0.15. Differencing
  // whitens the series so the usual 1/sqrt(n) error applies.
  std::vector<double> diff_a;
  std::vector<double> diff_b;
  for (size_t i = 1; i < a.size(); ++i) {
    diff_a.push_back(a[i] - a[i - 1]);
    diff_b.push_back(b[i] - b[i - 1]);
  }

  double covariance = 0.0;
  const double mean_a = Mean(diff_a);
  const double mean_b = Mean(diff_b);
  for (size_t i = 0; i < diff_a.size(); ++i) {
    covariance += (diff_a[i] - mean_a) * (diff_b[i] - mean_b);
  }
  covariance /= diff_a.size();
  const double correlation = covariance / (StdDev(diff_a) * StdDev(diff_b));
  EXPECT_LT(std::abs(correlation), 0.1) << "correlation was " << correlation;
}

// PRD 16 / 25: the rolling mean converges within +/-0.05%.
TEST(RefreshClockTest, MeanStaysWithinBudget) {
  RefreshClock clock(4242, kRefreshHz, DefaultConfig());
  const std::vector<double> intervals = Collect(&clock, 100000);

  const double mean = Mean(intervals);
  const double error_fraction = std::abs(mean - kBaseIntervalMs) / kBaseIntervalMs;
  EXPECT_LT(error_fraction, 0.0005) << "mean was " << mean;
}

// PRD 16: standard deviation lands in the delivery-path target range.
TEST(RefreshClockTest, StandardDeviationInTargetRange) {
  RefreshClock clock(7, kRefreshHz, DefaultConfig());
  // Exclude dropped frames; a whole extra period would dominate the variance
  // and says nothing about the jitter model.
  std::vector<double> intervals;
  for (double value : Collect(&clock, 100000)) {
    if (value < kBaseIntervalMs * 1.5) {
      intervals.push_back(value);
    }
  }

  const double stddev = StdDev(intervals);
  EXPECT_GE(stddev, 0.05) << "stddev was " << stddev;
  EXPECT_LE(stddev, 0.12) << "stddev was " << stddev;
}

// PRD 13 / 25: the safety rail is never exceeded, and with a sane config it
// should essentially never even engage.
TEST(RefreshClockTest, DriftStaysWithinSafetyRail) {
  RefreshClockConfig config = DefaultConfig();
  RefreshClock clock(31337, kRefreshHz, config);

  for (int i = 0; i < 200000; ++i) {
    clock.NextInterval();
    EXPECT_LE(std::abs(clock.last_sample().drift_ms), config.max_drift_ms);
  }

  // The default rail sits at 5 sigma, so clamping should be vanishingly rare.
  EXPECT_LT(clock.rail_clamp_count(), 200000u / 1000u);
}

// PRD 13: the distribution must not pile up at the rail. That is the failure
// mode the mean-reverting model exists to avoid.
TEST(RefreshClockTest, NoBoundaryPileUpAtTheRail) {
  RefreshClockConfig config = DefaultConfig();
  RefreshClock clock(2024, kRefreshHz, config);

  size_t near_rail = 0;
  constexpr int kSamples = 200000;
  for (int i = 0; i < kSamples; ++i) {
    clock.NextInterval();
    if (std::abs(clock.last_sample().drift_ms) > config.max_drift_ms * 0.98) {
      ++near_rail;
    }
  }

  EXPECT_LT(static_cast<double>(near_rail) / kSamples, 0.001);
}

// PRD 14 / 25: correlated, not white. A single-frame-independent model would
// show autocorrelation near zero at every lag.
TEST(RefreshClockTest, JitterIsTemporallyCorrelated) {
  RefreshClock clock(555, kRefreshHz, DefaultConfig());

  // Subtract the whole extra period that a dropped frame carries. Drops are a
  // separate phenomenon with their own tests, and they are enormous next to
  // the jitter: eight drops in 100,000 frames raise the series' standard
  // deviation from 0.068 ms to 0.163 ms and pull the measured lag-1
  // autocorrelation from 0.68 down to 0.11. Measuring the jitter model on the
  // raw series measures the drops instead.
  std::vector<double> intervals;
  intervals.reserve(100000);
  for (int i = 0; i < 100000; ++i) {
    const double interval_ms = clock.NextInterval().InMillisecondsF();
    intervals.push_back(interval_ms - clock.last_sample().drop_adjust_ms);
  }

  const double lag1 = Autocorrelation(intervals, 1);
  const double lag10 = Autocorrelation(intervals, 10);
  const double lag100 = Autocorrelation(intervals, 100);

  EXPECT_GT(lag1, 0.5) << "lag-1 autocorrelation was " << lag1;
  EXPECT_GT(lag10, 0.15) << "lag-10 autocorrelation was " << lag10;
  // Long memory: still clearly positive 100 frames out, which a single
  // fast-reverting component would not manage.
  EXPECT_GT(lag100, 0.02) << "lag-100 autocorrelation was " << lag100;
  // Monotone decay, not an oscillation.
  EXPECT_GT(lag1, lag10);
  EXPECT_GT(lag10, lag100);
}

// PRD 25: the drop rate matches the configuration.
TEST(RefreshClockTest, DropRateMatchesConfiguration) {
  RefreshClockConfig config = DefaultConfig();
  config.drop_probability = 0.001;
  RefreshClock clock(88, kRefreshHz, config);

  constexpr int kSamples = 500000;
  const std::vector<double> intervals = Collect(&clock, kSamples);
  const double observed = static_cast<double>(CountDrops(intervals)) / kSamples;

  // Bursts lift the realised rate above the per-frame probability, so allow a
  // generous band; this test guards the order of magnitude, not the decimal.
  EXPECT_GT(observed, config.drop_probability * 0.5);
  EXPECT_LT(observed, config.drop_probability * 3.0);
}

// PRD 15 / 25: bursts occur but stay bounded.
TEST(RefreshClockTest, DropBurstsStayWithinLimit) {
  RefreshClockConfig config = DefaultConfig();
  config.drop_probability = 0.01;
  RefreshClock clock(2718, kRefreshHz, config);

  int run = 0;
  int longest_run = 0;
  bool saw_a_burst = false;
  for (int i = 0; i < 500000; ++i) {
    clock.NextInterval();
    if (clock.last_sample().dropped) {
      ++run;
      longest_run = std::max(longest_run, run);
      if (run >= 2) {
        saw_a_burst = true;
      }
    } else {
      run = 0;
    }
  }

  EXPECT_LE(longest_run, RefreshClock::kMaxDropBurst);
  // The whole point of the burst model is that drops are not independent.
  EXPECT_TRUE(saw_a_burst);
}

// PRD 15: drops scale with load. This is the coupling that Section 22's first
// adversarial check probes.
TEST(RefreshClockTest, DropsScaleWithLoadHint) {
  RefreshClockConfig config = DefaultConfig();
  config.drop_probability = 0.0001;

  RefreshClock quiet(11, kRefreshHz, config);
  RefreshClock busy(11, kRefreshHz, config);

  const size_t low_drops = CountDrops(Collect(&quiet, 200000, LoadHint::kLow));
  const size_t high_drops = CountDrops(Collect(&busy, 200000, LoadHint::kHigh));

  EXPECT_GT(high_drops, low_drops * 5);
}

// The mean must stay on target even when drops are frequent enough to bias it,
// which is the interaction PRD 16 called out as broken in v1.0.
TEST(RefreshClockTest, MeanSurvivesAggressiveDropping) {
  RefreshClockConfig config = DefaultConfig();
  config.drop_probability = 0.002;
  RefreshClock clock(60606, kRefreshHz, config);

  const std::vector<double> intervals = Collect(&clock, 200000);
  const double mean = Mean(intervals);
  const double error_fraction = std::abs(mean - kBaseIntervalMs) / kBaseIntervalMs;

  EXPECT_LT(error_fraction, 0.0005)
      << "mean " << mean << " drifted after " << CountDrops(intervals)
      << " drops";
}

// Intervals must never go non-positive; the scheduler would spin.
TEST(RefreshClockTest, IntervalsAlwaysPositive) {
  RefreshClockConfig config = DefaultConfig();
  // Deliberately absurd noise, far outside any sane configuration.
  config.fine_noise_stddev_ms = 50.0;
  config.drift_sigma_ms = 20.0;
  config.max_drift_ms = 100.0;
  RefreshClock clock(5, kRefreshHz, config);

  for (int i = 0; i < 100000; ++i) {
    EXPECT_TRUE(clock.NextInterval().is_positive());
  }
}

TEST(RefreshClockTest, RespectsNonSixtyHertzRates) {
  for (double hz : {30.0, 90.0, 120.0, 144.0, 240.0}) {
    RefreshClock clock(1, hz, DefaultConfig());
    const std::vector<double> intervals = Collect(&clock, 50000);
    const double expected = 1000.0 / hz;
    const double error = std::abs(Mean(intervals) - expected) / expected;
    EXPECT_LT(error, 0.0005) << "at " << hz << " Hz";
  }
}

TEST(RefreshClockTest, SetBaseIntervalRetargetsWithoutResettingRng) {
  RefreshClock clock(77, kRefreshHz, DefaultConfig());
  Collect(&clock, 1000);
  const uint64_t frames_before = clock.frame_counter();

  clock.SetBaseInterval(base::Milliseconds(1000.0 / 120.0));
  const std::vector<double> intervals = Collect(&clock, 50000);

  EXPECT_EQ(frames_before, 1000u);
  const double expected = 1000.0 / 120.0;
  EXPECT_LT(std::abs(Mean(intervals) - expected) / expected, 0.001);
}

// A tight rail is a configuration error, and RefreshClockConfig should be able
// to say so rather than silently producing a clipped distribution.
TEST(RefreshClockTest, DetectsRailSetTooTight) {
  RefreshClockConfig config = DefaultConfig();
  config.drift_sigma_ms = 0.06;

  config.max_drift_ms = 0.30;
  EXPECT_FALSE(config.RailIsTooTight());

  config.max_drift_ms = 0.15;  // the v1.0 value: 2.5 sigma
  EXPECT_TRUE(config.RailIsTooTight());
}

}  // namespace
}  // namespace viz
