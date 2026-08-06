"""Human-like refresh clock for the delay-based BeginFrame scheduler.

Integration notes that were verified against real source rather than assumed:

* The seam is `DelayBasedTimeSource::PostNextTickTask`, which computes
  `next_tick_time_ = now.SnappedToNextTick(timebase_, interval_)`. That is a
  fixed phase grid. Varying `interval_` alone does not work -- the grid snaps
  the jitter straight back out. The free-running path below replaces the snap.

* `BeginFrameArgs::frame_time` is NOT `Now()`. `DelayBasedBeginFrameSource::
  OnTimerTick` builds it from `max(LastTickTime(), NextTickTime() -
  Interval())`. So jitter injected into the *timer delay* would be invisible
  downstream; it has to go into `next_tick_time_` itself, which is what this
  does.

* `BeginFrameArgs::interval` is taken from `time_source_->Interval()`, the
  stored nominal value. It is deliberately left nominal: real hardware reports
  its nominal refresh rate while actual vblank deltas vary around it, and
  downstream consumers (cc::Scheduler deadlines, CompositorFrameReporter)
  trust this field.

* Ticks must stay strictly increasing and must not compress below half an
  interval, or `DelayBasedBeginFrameSource`'s double-tick filter silently
  drops the frame and Blink's AnimationClock repeats the previous rAF
  timestamp. The guards below preserve both properties.
"""

from . import Edit, NewFile

BUILD = "components/viz/common/BUILD.gn"
SWITCHES_H = "components/viz/common/switches.h"
SWITCHES_CC = "components/viz/common/switches.cc"
DBTS_H = "components/viz/common/frame_sinks/delay_based_time_source.h"
DBTS_CC = "components/viz/common/frame_sinks/delay_based_time_source.cc"
GPU_HOST = "content/browser/gpu/gpu_process_host.cc"

# (constant name, switch string)
SWITCHES = [
    ("kEnableHumanRefreshClock", "enable-human-refresh-clock"),
    ("kRefreshClockSeed", "refresh-clock-seed"),
    ("kRefreshJitterStrength", "refresh-jitter-strength"),
    ("kRefreshDriftStrength", "refresh-drift-strength"),
    ("kRefreshDriftSigmaMs", "refresh-drift-sigma-ms"),
    ("kRefreshDriftReversion", "refresh-drift-reversion"),
    ("kRefreshDropProbability", "refresh-drop-probability"),
    ("kRefreshMaxDriftMs", "refresh-max-drift-ms"),
    ("kRefreshNoiseStddev", "refresh-noise-stddev"),
    ("kRefreshDeterministic", "refresh-deterministic"),
]

# --- BUILD.gn ------------------------------------------------------------

BUILD_SOURCES_ANCHOR = """    "frame_sinks/delay_based_time_source.cc",
    "frame_sinks/delay_based_time_source.h",
"""

BUILD_SOURCES_REPLACEMENT = """    "frame_sinks/delay_based_time_source.cc",
    "frame_sinks/delay_based_time_source.h",
    "frame_sinks/refresh_clock.cc",
    "frame_sinks/refresh_clock.h",
"""

BUILD_TESTS_ANCHOR = """    "frame_sinks/delay_based_time_source_unittest.cc",
"""

BUILD_TESTS_REPLACEMENT = """    "frame_sinks/delay_based_time_source_unittest.cc",
    "frame_sinks/refresh_clock_unittest.cc",
"""

# --- switches ------------------------------------------------------------

SWITCHES_H_ANCHOR = """}  // namespace switches

#endif  // COMPONENTS_VIZ_COMMON_SWITCHES_H_
"""

SWITCHES_CC_ANCHOR = """}  // namespace switches
"""


def _switches_h_replacement() -> str:
    decls = "\n".join(
        f"VIZ_COMMON_EXPORT extern const char {name}[];" for name, _ in SWITCHES
    )
    return f"""
// Human-like refresh clock. See components/viz/common/frame_sinks/
// refresh_clock.h. Each also has a CHROME_ENV_REFRESH_* environment variable,
// which is the preferred channel because the GPU process inherits the
// environment but not the browser's command line.
{decls}

}}  // namespace switches

#endif  // COMPONENTS_VIZ_COMMON_SWITCHES_H_
"""


def _switches_cc_replacement() -> str:
    defs = "\n".join(
        f'const char {name}[] = "{value}";' for name, value in SWITCHES
    )
    return f"""
// Human-like refresh clock; see refresh_clock.h for what each one does.
{defs}

}}  // namespace switches
"""


# --- the seam ------------------------------------------------------------

DBTS_H_INCLUDE_ANCHOR = """#include "base/timer/timer.h"
#include "components/viz/common/viz_common_export.h"
"""

DBTS_H_INCLUDE_REPLACEMENT = """#include "base/timer/timer.h"
#include "components/viz/common/frame_sinks/refresh_clock.h"  // ENV_FP
#include "components/viz/common/viz_common_export.h"
"""

DBTS_H_MEMBER_ANCHOR = """  base::RepeatingClosure tick_closure_;
  base::DeadlineTimer timer_;
};
"""

DBTS_H_MEMBER_REPLACEMENT = """  base::RepeatingClosure tick_closure_;
  base::DeadlineTimer timer_;

  // ENV_FP: null unless the human refresh clock is enabled, in which case the
  // fixed phase grid in PostNextTickTask is replaced by a free-running one.
  std::unique_ptr<RefreshClock> refresh_clock_;
};
"""

DBTS_CC_INCLUDE_ANCHOR = """#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string>

#include "base/check_op.h"
#include "base/functional/bind.h"
#include "base/location.h"
"""

DBTS_CC_INCLUDE_REPLACEMENT = """#include <algorithm>
#include <cmath>
#include <cstdint>
#include <memory>  // ENV_FP
#include <string>

#include "base/check_op.h"
#include "base/functional/bind.h"
#include "base/location.h"
#include "base/logging.h"  // ENV_FP
"""

DBTS_CC_HELPERS_ANCHOR = """namespace viz {

// The following methods correspond to the DelayBasedTimeSource that uses
// the base::TimeTicks::Now as the timebase.
"""

DBTS_CC_HELPERS_REPLACEMENT = """namespace viz {

namespace {

// ENV_FP: a tick landing within this window of `now` would be delivered
// immediately and read as a double tick downstream. Mirrors the judder
// allowance the snapped path uses.
constexpr base::TimeDelta kRefreshClockTickGuard = base::Microseconds(500);

// Bound on how many whole frames may be skipped in one go when the timer task
// came back very late. Also stops the catch-up loop from spinning if the phase
// is nonsense.
constexpr int kRefreshClockMaxCatchUpFrames = 64;

// Turns the delivery lateness of the timer task into a coarse load signal.
// This is the only load information available at this seam. It reflects
// pressure on the compositor thread, which is real but partial: work carried
// by raster workers or by the GPU's own queues does not show up here.
LoadHint ClassifyLoad(base::TimeDelta lateness, base::TimeDelta interval) {
  if (interval.is_zero() || !lateness.is_positive()) {
    return LoadHint::kLow;
  }
  const double ratio = lateness / interval;
  if (ratio > 0.5) {
    return LoadHint::kHigh;
  }
  if (ratio > 0.15) {
    return LoadHint::kModerate;
  }
  return LoadHint::kLow;
}

// Config is process-wide and read once; the clock itself is per time source.
const RefreshClockConfig& GetRefreshClockConfig() {
  static const RefreshClockConfig config =
      RefreshClockConfig::FromEnvironmentAndCommandLine();
  return config;
}

std::unique_ptr<RefreshClock> MaybeCreateRefreshClock(
    base::TimeDelta interval) {
  const RefreshClockConfig& config = GetRefreshClockConfig();
  if (!config.enabled) {
    return nullptr;
  }

  // Distinct time sources (multiple displays) must not share a stream, or
  // their cadences would be identical rather than merely similar.
  static int instance_index = 0;
  static const uint64_t base_seed = ResolveRefreshClockSeed();
  const uint64_t seed = base_seed + static_cast<uint64_t>(instance_index++);

  const double refresh_hz =
      interval.is_positive() ? 1000.0 / interval.InMillisecondsF() : 60.0;

  LOG(WARNING) << "Human Refresh Clock\\n"
               << "Enabled: true\\n"
               << "Seed: " << seed << "\\n"
               << "Refresh: " << refresh_hz << " Hz\\n"
               << "Base Interval: " << interval.InMillisecondsF() << " ms\\n"
               << "Drift Sigma: " << config.drift_sigma_ms << " ms\\n"
               << "Drift Reversion: " << config.drift_reversion << "\\n"
               << "Max Drift (rail): " << config.max_drift_ms << " ms\\n"
               << "Noise StdDev: " << config.fine_noise_stddev_ms << " ms\\n"
               << "Drop Probability (base): " << config.drop_probability << "\\n"
               << "Drift Components: " << RefreshClock::kDriftComponents;

  return std::make_unique<RefreshClock>(seed, refresh_hz, config);
}

}  // namespace

// The following methods correspond to the DelayBasedTimeSource that uses
// the base::TimeTicks::Now as the timebase.
"""

DBTS_CC_CTOR_ANCHOR = """      tick_closure_(base::BindRepeating(&DelayBasedTimeSource::OnTimerTick,
                                        base::Unretained(this))) {}
"""

DBTS_CC_CTOR_REPLACEMENT = """      tick_closure_(base::BindRepeating(&DelayBasedTimeSource::OnTimerTick,
                                        base::Unretained(this))),
      refresh_clock_(MaybeCreateRefreshClock(interval_)) {}  // ENV_FP
"""

DBTS_CC_SETINTERVAL_ANCHOR = """void DelayBasedTimeSource::SetTimebaseAndInterval(base::TimeTicks timebase,
                                                  base::TimeDelta interval) {
  interval_ = interval;
  timebase_ = timebase;
}
"""

DBTS_CC_SETINTERVAL_REPLACEMENT = """void DelayBasedTimeSource::SetTimebaseAndInterval(base::TimeTicks timebase,
                                                  base::TimeDelta interval) {
  interval_ = interval;
  timebase_ = timebase;
  // ENV_FP: retarget without disturbing drift state or the RNG stream.
  if (refresh_clock_) {
    refresh_clock_->SetBaseInterval(interval);
  }
}
"""

DBTS_CC_SEAM_ANCHOR = """void DelayBasedTimeSource::PostNextTickTask(base::TimeTicks now) {
  if (interval_.is_zero()) {
"""

DBTS_CC_SEAM_REPLACEMENT = """void DelayBasedTimeSource::PostNextTickTask(base::TimeTicks now) {
  // ENV_FP: free-running phase. The snapped path below computes
  // `now.SnappedToNextTick(timebase_, interval_)`, which pins every tick to a
  // fixed grid; a varying interval fights that grid and the jitter is snapped
  // straight back out. Here the phase advances by the generated interval
  // instead, so it tracks rather than resists.
  //
  // The phase advances from the previous *target* time, not from `now`, so
  // that real OS delivery jitter does not accumulate into the emitted mean.
  if (refresh_clock_ && !interval_.is_zero()) {
    base::TimeTicks phase = last_tick_time_;
    LoadHint hint = LoadHint::kUnknown;

    // last_tick_time_ sits at the epoch before the first tick and after
    // SetActive(false); re-anchor rather than trying to catch up from 1970.
    const bool phase_usable =
        !phase.is_null() &&
        phase > now - interval_ * kRefreshClockMaxCatchUpFrames;
    if (phase_usable) {
      hint = ClassifyLoad(now - phase, interval_);
    } else {
      phase = now;
    }

    next_tick_time_ = phase + refresh_clock_->NextInterval(hint);

    // If the task came back late, skip whole frames -- which is exactly what
    // real hardware does -- rather than firing immediately.
    for (int i = 0; i < kRefreshClockMaxCatchUpFrames &&
                    next_tick_time_ <= now + kRefreshClockTickGuard;
         ++i) {
      next_tick_time_ += refresh_clock_->NextInterval(hint);
    }
    if (next_tick_time_ <= now + kRefreshClockTickGuard) {
      // Hopelessly behind. Re-anchor instead of spinning.
      next_tick_time_ = now + interval_;
    }

    DCHECK_GT(next_tick_time_, now);
    timer_.Start(FROM_HERE, next_tick_time_, tick_closure_,
                 base::subtle::DelayPolicy::kPrecise);
    return;
  }

  if (interval_.is_zero()) {
"""

# --- GPU process forwarding ---------------------------------------------
#
# DelayBasedTimeSource runs in the GPU process. A browser command-line switch
# does not reach it unless it is copied here, so without this edit the
# --refresh-* switches would silently do nothing in the normal multi-process
# configuration. (The environment variables work regardless, which is why they
# are the recommended channel.)

GPU_HOST_ANCHOR = """    switches::kDoubleBufferCompositing,
"""


def _gpu_host_replacement() -> str:
    forwarded = "\n".join(f"    switches::{name}," for name, _ in SWITCHES)
    return f"""    switches::kDoubleBufferCompositing,
    // ENV_FP: the human refresh clock lives in viz, which runs here.
{forwarded}
"""


def edits(ctx: dict) -> list:
    return [
        NewFile("components/viz/common/frame_sinks/refresh_clock.h"),
        NewFile("components/viz/common/frame_sinks/refresh_clock.cc"),
        NewFile("components/viz/common/frame_sinks/refresh_clock_unittest.cc"),
        Edit(
            path=BUILD,
            anchor=BUILD_SOURCES_ANCHOR,
            replacement=BUILD_SOURCES_REPLACEMENT,
            marker='"frame_sinks/refresh_clock.cc",',
            why="compile refresh_clock into viz_common",
        ),
        Edit(
            path=BUILD,
            anchor=BUILD_TESTS_ANCHOR,
            replacement=BUILD_TESTS_REPLACEMENT,
            marker='"frame_sinks/refresh_clock_unittest.cc",',
            why="add the unit tests to viz_unittests",
        ),
        Edit(
            path=SWITCHES_H,
            anchor=SWITCHES_H_ANCHOR,
            replacement=_switches_h_replacement(),
            marker="kEnableHumanRefreshClock",
            why="declare the refresh clock switches",
        ),
        Edit(
            path=SWITCHES_CC,
            anchor=SWITCHES_CC_ANCHOR,
            replacement=_switches_cc_replacement(),
            marker="kEnableHumanRefreshClock",
            why="define the refresh clock switches",
        ),
        Edit(
            path=DBTS_H,
            anchor=DBTS_H_INCLUDE_ANCHOR,
            replacement=DBTS_H_INCLUDE_REPLACEMENT,
            marker="refresh_clock.h\"  // ENV_FP",
            why="RefreshClock type for the member below",
        ),
        Edit(
            path=DBTS_H,
            anchor=DBTS_H_MEMBER_ANCHOR,
            replacement=DBTS_H_MEMBER_REPLACEMENT,
            marker="refresh_clock_;",
            why="hold the clock for the time source's lifetime",
        ),
        Edit(
            path=DBTS_CC,
            anchor=DBTS_CC_INCLUDE_ANCHOR,
            replacement=DBTS_CC_INCLUDE_REPLACEMENT,
            marker="#include <memory>  // ENV_FP",
            why="std::make_unique and LOG() used by the helpers below",
        ),
        Edit(
            path=DBTS_CC,
            anchor=DBTS_CC_HELPERS_ANCHOR,
            replacement=DBTS_CC_HELPERS_REPLACEMENT,
            marker="MaybeCreateRefreshClock",
            why="load classification, config, construction and startup log",
        ),
        Edit(
            path=DBTS_CC,
            anchor=DBTS_CC_CTOR_ANCHOR,
            replacement=DBTS_CC_CTOR_REPLACEMENT,
            marker="refresh_clock_(MaybeCreateRefreshClock",
            why="create the clock alongside the time source",
        ),
        Edit(
            path=DBTS_CC,
            anchor=DBTS_CC_SETINTERVAL_ANCHOR,
            replacement=DBTS_CC_SETINTERVAL_REPLACEMENT,
            marker="refresh_clock_->SetBaseInterval",
            why="track display refresh rate changes",
        ),
        Edit(
            path=DBTS_CC,
            anchor=DBTS_CC_SEAM_ANCHOR,
            replacement=DBTS_CC_SEAM_REPLACEMENT,
            marker="ENV_FP: free-running phase",
            why="THE SEAM: replace the fixed phase grid with a free-running one",
        ),
        Edit(
            path=GPU_HOST,
            anchor=GPU_HOST_ANCHOR,
            replacement=_gpu_host_replacement(),
            marker="ENV_FP: the human refresh clock lives in viz",
            why="forward the switches to the GPU process, where viz runs",
        ),
    ]
