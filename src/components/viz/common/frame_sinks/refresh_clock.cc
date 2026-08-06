// Copyright 2011 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "components/viz/common/frame_sinks/refresh_clock.h"

#include <algorithm>
#include <cmath>
#include <memory>
#include <numbers>
#include <optional>
#include <string>

#include "base/command_line.h"
#include "base/environment.h"
#include "base/logging.h"
#include "base/rand_util.h"
#include "base/strings/string_number_conversions.h"
#include "components/viz/common/switches.h"

namespace viz {

namespace {

// Environment variables. This code runs in the GPU process, which inherits the
// environment block but not the browser's command line. The switches below are
// forwarded explicitly via kSwitchNames in gpu_process_host.cc; the
// environment needs no such plumbing, which is why it takes precedence and is
// the recommended channel.
constexpr char kEnableEnv[] = "CHROME_ENV_REFRESH_CLOCK";
constexpr char kSeedEnv[] = "CHROME_ENV_REFRESH_SEED";
constexpr char kJitterStrengthEnv[] = "CHROME_ENV_REFRESH_JITTER_STRENGTH";
constexpr char kDriftStrengthEnv[] = "CHROME_ENV_REFRESH_DRIFT_STRENGTH";
constexpr char kDriftSigmaEnv[] = "CHROME_ENV_REFRESH_DRIFT_SIGMA_MS";
constexpr char kDriftReversionEnv[] = "CHROME_ENV_REFRESH_DRIFT_REVERSION";
constexpr char kDropProbabilityEnv[] = "CHROME_ENV_REFRESH_DROP_PROBABILITY";
constexpr char kMaxDriftEnv[] = "CHROME_ENV_REFRESH_MAX_DRIFT_MS";
constexpr char kNoiseStddevEnv[] = "CHROME_ENV_REFRESH_NOISE_STDDEV";
constexpr char kDeterministicEnv[] = "CHROME_ENV_REFRESH_DETERMINISTIC";

// Rate at which accumulated mean error is repaid, per frame. Small enough that
// the correction stays well under the fine noise and does not show up as a
// periodic sawtooth in the autocorrelation.
constexpr double kMeanCorrectionGain = 0.02;

// Hard ceiling on a single frame's mean correction, as a fraction of the base
// interval (0.05 ms at 60 Hz). Repaying faster than this would show up as a
// visible dip in the frames following a drop.
constexpr double kMaxMeanCorrectionFraction = 0.003;

// Probability of extending an in-progress drop burst. Real drops cluster;
// this is what produces bursts of two or three rather than isolated events.
constexpr double kBurstContinuationProbability = 0.35;

// Multiplier applied to the base drop probability per load level. These are
// plausible rather than measured -- see the limitations section of the docs.
constexpr double kLoadMultiplierLow = 1.0;
constexpr double kLoadMultiplierModerate = 8.0;
constexpr double kLoadMultiplierHigh = 40.0;

// Reads a value from the environment, falling back to a command-line switch.
std::optional<std::string> ReadSetting(const char* env_name,
                                       const char* switch_name) {
  @ENV_GETVAR@

  if (base::CommandLine::InitializedForCurrentProcess()) {
    const base::CommandLine* command_line =
        base::CommandLine::ForCurrentProcess();
    if (command_line->HasSwitch(switch_name)) {
      std::string from_switch = command_line->GetSwitchValueASCII(switch_name);
      if (!from_switch.empty()) {
        return from_switch;
      }
    }
  }

  return std::nullopt;
}

// Applies a double setting in place, leaving the default alone when unset or
// unparseable. A malformed value is loud rather than silently ignored,
// because a silently-defaulted knob invalidates an experiment.
void ApplyDouble(const char* env_name,
                 const char* switch_name,
                 double* target) {
  std::optional<std::string> raw = ReadSetting(env_name, switch_name);
  if (!raw.has_value()) {
    return;
  }
  double parsed = 0.0;
  if (base::StringToDouble(*raw, &parsed)) {
    *target = parsed;
  } else {
    LOG(ERROR) << "RefreshClock: ignoring unparseable value for " << env_name
               << ": " << *raw;
  }
}

bool ReadBool(const char* env_name, const char* switch_name, bool fallback) {
  std::optional<std::string> raw = ReadSetting(env_name, switch_name);
  if (!raw.has_value()) {
    // A bare --enable-human-refresh-clock with no value still counts.
    return base::CommandLine::InitializedForCurrentProcess() &&
                   base::CommandLine::ForCurrentProcess()->HasSwitch(
                       switch_name)
               ? true
               : fallback;
  }
  return *raw == "1" || *raw == "true" || *raw == "yes";
}

// SplitMix64, used to expand a single seed into xoshiro's four words. Seeding
// a xoshiro state directly from a small integer leaves it poorly mixed for the
// first few outputs; SplitMix64 is the author-recommended remedy.
uint64_t SplitMix64(uint64_t* state) {
  uint64_t z = (*state += UINT64_C(0x9E3779B97F4A7C15));
  z = (z ^ (z >> 30)) * UINT64_C(0xBF58476D1CE4E5B9);
  z = (z ^ (z >> 27)) * UINT64_C(0x94D049BB133111EB);
  return z ^ (z >> 31);
}

uint64_t RotateLeft(uint64_t x, int k) {
  return (x << k) | (x >> (64 - k));
}

}  // namespace

// static
RefreshClockConfig RefreshClockConfig::FromEnvironmentAndCommandLine() {
  RefreshClockConfig config;
  config.enabled = ReadBool(kEnableEnv, switches::kEnableHumanRefreshClock, /*fallback=*/false);
  if (!config.enabled) {
    return config;
  }

  ApplyDouble(kJitterStrengthEnv, switches::kRefreshJitterStrength,
              &config.jitter_strength);
  ApplyDouble(kDriftStrengthEnv, switches::kRefreshDriftStrength, &config.drift_strength);
  ApplyDouble(kDriftReversionEnv, switches::kRefreshDriftReversion,
              &config.drift_reversion);
  ApplyDouble(kDriftSigmaEnv, switches::kRefreshDriftSigmaMs, &config.drift_sigma_ms);
  ApplyDouble(kMaxDriftEnv, switches::kRefreshMaxDriftMs, &config.max_drift_ms);
  ApplyDouble(kNoiseStddevEnv, switches::kRefreshNoiseStddev,
              &config.fine_noise_stddev_ms);
  ApplyDouble(kDropProbabilityEnv, switches::kRefreshDropProbability,
              &config.drop_probability);
  config.deterministic =
      ReadBool(kDeterministicEnv, switches::kRefreshDeterministic, /*fallback=*/true);

  return config;
}

bool RefreshClockConfig::RailIsTooTight() const {
  return max_drift_ms < 4.0 * drift_sigma_ms * drift_strength;
}

uint64_t ResolveRefreshClockSeed() {
  std::optional<std::string> raw = ReadSetting(kSeedEnv, switches::kRefreshClockSeed);
  if (raw.has_value()) {
    uint64_t parsed = 0;
    if (base::StringToUint64(*raw, &parsed)) {
      return parsed;
    }
    LOG(ERROR) << "RefreshClock: unparseable seed \"" << *raw
               << "\", falling back to a random one";
  }
  return base::RandUint64();
}

RefreshClock::RefreshClock(uint64_t seed,
                           double refresh_rate_hz,
                           const RefreshClockConfig& config)
    : seed_(seed),
      config_(config),
      base_interval_ms_(refresh_rate_hz > 0.0 ? 1000.0 / refresh_rate_hz
                                              : 1000.0 / 60.0),
      mean_error_ms_(0.0),
      frame_counter_(0),
      consecutive_drops_(0),
      rail_clamp_count_(0) {
  if (!config_.deterministic) {
    // Mix rather than replace, so a supplied seed still influences the run.
    seed_ ^= base::RandUint64();
  }

  // Spread the reversion rates geometrically so the summed process
  // approximates 1/f across the band. Clamp to keep each component stable:
  // x -= theta * x diverges outside 0 < theta < 2, and anything above ~0.9
  // decays so fast it contributes no memory.
  for (int i = 0; i < kDriftComponents; ++i) {
    theta_[i] = std::clamp(config_.drift_reversion * std::pow(kThetaSpacing, i),
                           1e-4, 0.9);
  }

  // Equal variance per component sums to the requested total variance.
  component_sigma_ms_ = config_.drift_sigma_ms * config_.drift_strength /
                        std::sqrt(static_cast<double>(kDriftComponents));

  if (config_.RailIsTooTight()) {
    LOG(WARNING) << "RefreshClock: max_drift_ms (" << config_.max_drift_ms
                 << ") is under 4 sigma (" << 4.0 * config_.drift_sigma_ms
                 << "). The rail will clip the drift distribution and pile "
                    "probability at the boundary, which is detectable.";
  }

  SeedState(seed_);
}

RefreshClock::~RefreshClock() = default;

void RefreshClock::SeedState(uint64_t seed) {
  uint64_t mixer = seed;
  for (uint64_t& word : rng_state_) {
    word = SplitMix64(&mixer);
  }
  drift_ms_.fill(0.0);
  mean_error_ms_ = 0.0;
  frame_counter_ = 0;
  consecutive_drops_ = 0;
  rail_clamp_count_ = 0;
  last_sample_ = Sample();
}

void RefreshClock::ResetForTesting(uint64_t seed) {
  seed_ = seed;
  SeedState(seed);
}

uint64_t RefreshClock::NextRandomUint64() {
  // xoshiro256++.
  const uint64_t result = RotateLeft(rng_state_[0] + rng_state_[3], 23) +
                          rng_state_[0];
  const uint64_t t = rng_state_[1] << 17;

  rng_state_[2] ^= rng_state_[0];
  rng_state_[3] ^= rng_state_[1];
  rng_state_[1] ^= rng_state_[2];
  rng_state_[0] ^= rng_state_[3];
  rng_state_[2] ^= t;
  rng_state_[3] = RotateLeft(rng_state_[3], 45);

  return result;
}

double RefreshClock::NextUniform() {
  // Top 53 bits give a uniform double in [0, 1) with full mantissa coverage.
  return static_cast<double>(NextRandomUint64() >> 11) *
         (1.0 / 9007199254740992.0);
}

double RefreshClock::NextGaussian() {
  // Box-Muller. u1 is nudged off zero so the log is finite.
  const double u1 = std::max(NextUniform(), 1e-12);
  const double u2 = NextUniform();
  return std::sqrt(-2.0 * std::log(u1)) *
         std::cos(2.0 * std::numbers::pi * u2);
}

double RefreshClock::LoadMultiplier(LoadHint hint) const {
  switch (hint) {
    case LoadHint::kHigh:
      return kLoadMultiplierHigh;
    case LoadHint::kModerate:
      return kLoadMultiplierModerate;
    case LoadHint::kLow:
    case LoadHint::kUnknown:
      return kLoadMultiplierLow;
  }
  return kLoadMultiplierLow;
}

base::TimeDelta RefreshClock::base_interval() const {
  return base::Microseconds(base_interval_ms_ * 1000.0);
}

void RefreshClock::SetBaseInterval(base::TimeDelta interval) {
  if (interval.is_positive()) {
    base_interval_ms_ = interval.InMillisecondsF();
  }
}

base::TimeDelta RefreshClock::NextInterval(LoadHint hint) {
  // Exactly 2 * kDriftComponents draws for the drift, 2 for the fine noise,
  // and 1 for the drop test: fixed, bounded, allocation-free, lock-free.

  // --- long-memory drift -------------------------------------------------
  double drift_total_ms = 0.0;
  for (int i = 0; i < kDriftComponents; ++i) {
    // Discrete Ornstein-Uhlenbeck. Reverts to zero softly, so the walk stays
    // bounded without the hard wall that a raw clamp would impose.
    drift_ms_[i] += -theta_[i] * drift_ms_[i] +
                    std::sqrt(2.0 * theta_[i]) * component_sigma_ms_ *
                        NextGaussian();
    drift_total_ms += drift_ms_[i];
  }

  bool rail_clamped = false;
  if (std::abs(drift_total_ms) > config_.max_drift_ms) {
    drift_total_ms = std::clamp(drift_total_ms, -config_.max_drift_ms,
                                config_.max_drift_ms);
    rail_clamped = true;
    ++rail_clamp_count_;
  }

  // --- fine noise --------------------------------------------------------
  const double fine_noise_ms = config_.fine_noise_stddev_ms *
                               config_.jitter_strength * NextGaussian();

  // --- drops -------------------------------------------------------------
  double drop_probability = config_.drop_probability * LoadMultiplier(hint);
  if (consecutive_drops_ >= kMaxDropBurst) {
    drop_probability = 0.0;
  } else if (consecutive_drops_ > 0) {
    drop_probability = kBurstContinuationProbability;
  }
  const bool dropped = NextUniform() < drop_probability;
  consecutive_drops_ = dropped ? consecutive_drops_ + 1 : 0;

  // A dropped frame means the next presentation lands a whole period later.
  const double drop_adjust_ms = dropped ? base_interval_ms_ : 0.0;

  // --- mean correction ---------------------------------------------------
  // A real display's vblank rate does not slow down because a frame was
  // dropped, so the time added above is repaid over subsequent frames rather
  // than left to bias the long-run cadence.
  //
  // Only the drop adjustment is repaid. Feeding the drift and fine noise into
  // this accumulator as well -- which is the obvious reading of the PRD --
  // turns the correction into integral feedback over the whole signal, and
  // integral feedback is a high-pass filter. It cancels exactly the
  // low-frequency content that the multi-component drift model exists to
  // produce, collapsing the lag-1 autocorrelation from ~0.7 to ~0.08 and
  // driving the lag-100 autocorrelation negative. Both terms are zero-mean by
  // construction and need no correction; correcting them destroys the 1/f
  // structure. Measured before and after in docs/refresh-clock.md.
  const double correction_limit = base_interval_ms_ * kMaxMeanCorrectionFraction;
  const double mean_correction_ms = -std::clamp(
      mean_error_ms_ * kMeanCorrectionGain, -correction_limit, correction_limit);

  double interval_ms = base_interval_ms_ + drift_total_ms + fine_noise_ms +
                       drop_adjust_ms + mean_correction_ms;

  // Never emit a non-positive or absurdly short interval; the scheduler would
  // spin. Two percent of the base period is far outside the model's range.
  interval_ms = std::max(interval_ms, base_interval_ms_ * 0.02);

  // Only the drop adjustment and its own repayment enter the accumulator; see
  // the comment above for why the drift and noise terms must not.
  mean_error_ms_ += drop_adjust_ms + mean_correction_ms;

  ++frame_counter_;
  last_sample_ = Sample{interval_ms,       drift_total_ms,  fine_noise_ms,
                        drop_adjust_ms,    mean_correction_ms, dropped,
                        rail_clamped,      frame_counter_};

  return base::Microseconds(interval_ms * 1000.0);
}

}  // namespace viz
