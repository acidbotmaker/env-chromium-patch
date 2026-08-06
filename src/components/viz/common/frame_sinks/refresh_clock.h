// Copyright 2011 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#ifndef COMPONENTS_VIZ_COMMON_FRAME_SINKS_REFRESH_CLOCK_H_
#define COMPONENTS_VIZ_COMMON_FRAME_SINKS_REFRESH_CLOCK_H_

#include <stdint.h>

#include <array>

#include "base/time/time.h"
#include "components/viz/common/viz_common_export.h"

namespace viz {

// Coarse description of how loaded the frame delivery path looked recently.
//
// The only load signal available at this seam is how late the compositor
// thread's own timer task was delivered. That is a genuine signal -- a busy
// compositor thread does run its tasks late -- but it is a proxy for
// compositor-thread scheduling pressure, not for GPU or raster work. Load
// carried entirely by the GPU process's other threads, or by raster workers,
// will not show up here. See the limitations section of docs/refresh-clock.md.
enum class LoadHint {
  kUnknown,
  kLow,
  kModerate,
  kHigh,
};

struct VIZ_COMMON_EXPORT RefreshClockConfig {
  bool enabled = false;

  double jitter_strength = 1.0;
  double drift_strength = 1.0;

  // Ornstein-Uhlenbeck mean-reversion rate per frame, for the slowest drift
  // component. Faster components are derived from it (see kThetaSpacing).
  double drift_reversion = 0.01;

  // Target stationary standard deviation of the summed drift term, in ms.
  double drift_sigma_ms = 0.06;

  // Safety rail only. Should be >= 4 * drift_sigma_ms so it does not clip the
  // distribution; clipping piles probability at the boundary and is itself
  // detectable. RefreshClock warns if this is set too tight.
  double max_drift_ms = 0.30;

  double fine_noise_stddev_ms = 0.02;

  // Baseline per-frame drop probability under low load. Scales up with the
  // load hint, and bursts are handled separately.
  double drop_probability = 0.00005;

  // When false, the supplied seed is mixed with a nondeterministic value so
  // repeated runs differ. Emitted-sequence determinism tests require true.
  bool deterministic = true;

  // Reads configuration from the environment and the command line, in that
  // order of precedence. Environment variables are used as the primary channel
  // because this code runs in the GPU process, which inherits the environment
  // block but does not automatically inherit browser command-line switches.
  static RefreshClockConfig FromEnvironmentAndCommandLine();

  // True if `max_drift_ms` is tight enough to visibly clip the drift
  // distribution, which would defeat the point of the mean-reverting model.
  bool RailIsTooTight() const;
};

// Resolves the seed from CHROME_ENV_REFRESH_SEED or --refresh-clock-seed.
// When neither is set, generates one with base::RandUint64() so the run is
// still reproducible from the startup log.
VIZ_COMMON_EXPORT uint64_t ResolveRefreshClockSeed();

// Generates a deterministic, seed-driven sequence of frame intervals whose
// statistics resemble a real compositor delivery path rather than a perfect
// metronome.
//
// This models the *software delivery path* -- PLL drift plus interrupt latency
// plus scheduler latency plus compositor delay plus GPU scheduling variance --
// and not the bare hardware VBlank crystal, whose frame-to-frame jitter is
// far smaller than anything produced here.
//
// Threading: lives on, and is used only from, the compositor thread that owns
// the DelayBasedTimeSource. Holds no shared mutable state and takes no locks.
//
// Determinism: the sequence returned by successive NextInterval() calls is a
// pure function of (seed, refresh rate, config, load hints). The timing a web
// page observes is NOT, because the OS delivers the scheduler's delayed tasks
// late by a real and variable amount. Assert determinism on the emitted
// sequence only.
class VIZ_COMMON_EXPORT RefreshClock {
 public:
  // Number of summed Ornstein-Uhlenbeck components. A single component has an
  // exponentially decaying autocorrelation (a Lorentzian spectrum) that a
  // detector fitting the PSD can distinguish from the near-1/f spectrum of a
  // real delivery path. Summing components with geometrically spaced
  // reversion rates approximates 1/f across the band of interest.
  static constexpr int kDriftComponents = 4;

  // Ratio between adjacent components' reversion rates. With the default
  // drift_reversion of 0.01 this yields thetas of 0.01, 0.04, 0.16 and 0.64,
  // i.e. correlation times of roughly 100, 25, 6 and 1.5 frames.
  static constexpr double kThetaSpacing = 4.0;

  // Longest run of consecutive dropped frames. Real displays drop in short
  // bursts under load; a strict "never consecutive" rule is itself a
  // signature of a model.
  static constexpr int kMaxDropBurst = 3;

  // Per-frame diagnostics for the verbose log and for tests.
  struct Sample {
    double interval_ms = 0.0;
    double drift_ms = 0.0;
    double fine_noise_ms = 0.0;
    double drop_adjust_ms = 0.0;
    double mean_correction_ms = 0.0;
    bool dropped = false;
    bool rail_clamped = false;
    uint64_t frame_counter = 0;
  };

  RefreshClock(uint64_t seed,
               double refresh_rate_hz,
               const RefreshClockConfig& config);
  ~RefreshClock();

  RefreshClock(const RefreshClock&) = delete;
  RefreshClock& operator=(const RefreshClock&) = delete;

  // Returns the next target frame interval. Draws a fixed, bounded number of
  // RNG values, allocates nothing, and takes no locks.
  base::TimeDelta NextInterval(LoadHint hint = LoadHint::kUnknown);

  // Updates the nominal interval, e.g. when the display's reported refresh
  // rate changes. Does not disturb drift state or the RNG stream.
  void SetBaseInterval(base::TimeDelta interval);

  base::TimeDelta base_interval() const;
  uint64_t seed() const { return seed_; }
  const RefreshClockConfig& config() const { return config_; }
  const Sample& last_sample() const { return last_sample_; }
  uint64_t frame_counter() const { return frame_counter_; }

  // Number of times the safety rail clamped the drift. Should stay at or near
  // zero; a growing count means the configuration is wrong.
  uint64_t rail_clamp_count() const { return rail_clamp_count_; }

  // Test-only. Production code constructs the clock once and never reseeds.
  void ResetForTesting(uint64_t seed);

 private:
  // xoshiro256++. Chosen over PCG64 (the PRD's first preference) because
  // PCG64's 128-bit state needs __uint128_t, which is not portable across all
  // of Chromium's toolchains. xoshiro256++ is pure 64-bit arithmetic, is the
  // PRD's second preference, and is more than adequate here: this stream feeds
  // a jitter model, not cryptography.
  uint64_t NextRandomUint64();

  // Uniform in [0, 1).
  double NextUniform();

  // Standard normal via Box-Muller. Deliberately does not cache the second
  // variate, so the number of RNG draws per frame stays fixed rather than
  // alternating -- a fixed draw count is what makes the emitted sequence
  // reproducible independent of call history.
  double NextGaussian();

  void SeedState(uint64_t seed);
  double LoadMultiplier(LoadHint hint) const;

  uint64_t seed_;
  std::array<uint64_t, 4> rng_state_;

  RefreshClockConfig config_;
  double base_interval_ms_;

  std::array<double, kDriftComponents> drift_ms_;
  std::array<double, kDriftComponents> theta_;
  double component_sigma_ms_;

  // Accumulated difference between emitted intervals and the nominal
  // interval, repaid slowly so the long-run mean stays on target without a
  // visible sawtooth.
  double mean_error_ms_;

  uint64_t frame_counter_;
  int consecutive_drops_;
  uint64_t rail_clamp_count_;
  Sample last_sample_;
};

}  // namespace viz

#endif  // COMPONENTS_VIZ_COMMON_FRAME_SINKS_REFRESH_CLOCK_H_
