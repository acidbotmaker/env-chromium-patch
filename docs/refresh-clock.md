# Human-like refresh clock

Replaces Chromium's perfectly periodic delay-based BeginFrame schedule with a
deterministic, seed-driven timing model whose statistics resemble a real
compositor delivery path.

The claim is narrow. This defeats single-signal static checks that read the
BeginFrame cadence in isolation. It is **not** undetectable, and several of the
checks below break it on purpose. Where it breaks is recorded here rather than
hidden.

## Enabling

```sh
CHROME_ENV_REFRESH_CLOCK=1 CHROME_ENV_REFRESH_SEED=918273645 \
  out/Release/chrome --user-data-dir=/tmp/rc
```

| Variable | Switch | Default |
|---|---|---|
| `CHROME_ENV_REFRESH_CLOCK` | `--enable-human-refresh-clock` | off |
| `CHROME_ENV_REFRESH_SEED` | `--refresh-clock-seed` | random, logged |
| `CHROME_ENV_REFRESH_JITTER_STRENGTH` | `--refresh-jitter-strength` | 1.0 |
| `CHROME_ENV_REFRESH_DRIFT_STRENGTH` | `--refresh-drift-strength` | 1.0 |
| `CHROME_ENV_REFRESH_DRIFT_SIGMA_MS` | `--refresh-drift-sigma-ms` | 0.06 |
| `CHROME_ENV_REFRESH_DRIFT_REVERSION` | `--refresh-drift-reversion` | 0.01 |
| `CHROME_ENV_REFRESH_MAX_DRIFT_MS` | `--refresh-max-drift-ms` | 0.30 |
| `CHROME_ENV_REFRESH_NOISE_STDDEV` | `--refresh-noise-stddev` | 0.02 |
| `CHROME_ENV_REFRESH_DROP_PROBABILITY` | `--refresh-drop-probability` | 0.00005 |
| `CHROME_ENV_REFRESH_DETERMINISTIC` | `--refresh-deterministic` | true |

**Prefer the environment variables.** `DelayBasedTimeSource` runs in the GPU
process, which inherits the environment block but not the browser's command
line. The switches only work because this patch adds them to `kSwitchNames[]`
in `content/browser/gpu/gpu_process_host.cc`; if that edit is ever dropped in a
rebase, the switches silently stop working while the env vars keep working.

## The seam

`DelayBasedTimeSource::PostNextTickTask` normally computes

```cpp
next_tick_time_ = now.SnappedToNextTick(timebase_, interval_);
```

which pins every tick to a fixed phase grid. **Varying `interval_` alone does
not work** — the grid snaps the jitter straight back out. The patch replaces
the snap with a free-running phase that advances from the previous *target*
time by a generated interval, so OS delivery jitter does not accumulate into
the emitted mean.

This is also the only correct place to inject. `BeginFrameArgs::frame_time` is
not `Now()`; `DelayBasedBeginFrameSource::OnTimerTick` derives it from
`max(LastTickTime(), NextTickTime() - Interval())`. Jitter injected into the
*timer delay* rather than into `next_tick_time_` would be invisible downstream.

Two invariants the code preserves, both load-bearing:

* Ticks stay strictly increasing. If `frame_time` ever went backwards,
  `AnimationClock::UpdateTime` silently discards the update and the rAF
  timestamp repeats the previous value, producing a spurious 0 ms delta.
* Consecutive `frame_time`s never compress below half an interval, or
  `DelayBasedBeginFrameSource`'s double-tick filter drops the frame entirely.

## Findings

### 1. The mean correction and the drift model were in direct conflict

PRD section 16 asks for a `mean_correction` term that repays accumulated mean
error; section 14 asks for long-memory, near-1/f drift. Implemented literally —
accumulating every deviation and repaying it — the correction is **integral
feedback over the whole signal, which is a high-pass filter**. It cancels
exactly the low-frequency content section 14 exists to create.

Measured, at otherwise identical settings:

| | lag-1 | lag-10 | lag-100 |
|---|---|---|---|
| correcting the whole signal | 0.084 | 0.026 | −0.005 |
| correcting only drops | **0.684** | **0.361** | **0.085** |

The resolution: the drift and fine-noise terms are zero-mean by construction
and need no correction at all. Only dropped frames introduce a systematic
offset, so only the drop adjustment enters the accumulator. This is
physically right as well — a real display's vblank rate does not slow down
because a frame was dropped, so the time genuinely is repaid.

### 2. The rAF timestamp is quantised to 100 µs, which is larger than the jitter

This is the most important practical finding and it constrains the whole
design.

`PageAnimator::ServiceScriptedAnimations` passes the frame time through
`blink::TimeClamper::ClampTimeResolution`. The granularity is:

* **100 µs** by default (`kCoarseResolutionMicroseconds`)
* **5 µs** when the page is cross-origin isolated (`kFineResolutionMicroseconds`)

The design's jitter has a standard deviation of about 60 µs — **below one
bucket**. So on a page that is not cross-origin isolated, the injected jitter
is not resolvable on any individual frame.

It is not erased, though. The rounding is deterministic dithering: the
threshold within each bucket sits at a fixed pseudorandom position, so a
sequence of frame times drifting across a bucket flips between two adjacent
reported values, and the *fraction* of frames in each bucket encodes the
sub-bucket position. Sub-quantum jitter therefore leaks statistically over
hundreds of frames even though it is invisible per frame.

Consequences:

* Any measurement harness should be served **cross-origin isolated**
  (`Cross-Origin-Opener-Policy: same-origin` plus
  `Cross-Origin-Embedder-Policy: require-corp`) to get 5 µs resolution.
  `test/refresh-clock.html` detects and reports this.
* A detector that only reads per-frame rAF deltas without cross-origin
  isolation cannot see this model directly — but it also cannot see the
  *unpatched* perfect periodicity clearly, which is the signal being hidden.
  Quantifying that trade-off is a genuine result for the paper.
* Raising `CHROME_ENV_REFRESH_DRIFT_SIGMA_MS` to 0.2–0.5 makes the jitter
  resolvable without cross-origin isolation, at the cost of realism: real
  delivery paths do not jitter that much.

### 3. A handful of drops destroys the measured autocorrelation

Eight dropped frames in 100,000 raise the trace's standard deviation from
0.068 ms to 0.163 ms and pull the measured lag-1 autocorrelation from 0.68 to
0.11, because each drop contributes a whole 16.67 ms period. Any analysis of
the jitter model must remove the drop periods first. Both the unit tests and
`tools/analyze-intervals.py` do this; an analysis that does not is measuring
the drops.

### 4. The spectral check discriminates; the autocorrelation check does not

PRD section 21 check 8 asks that the PSD approximate 1/f and that a single
AR(1) baseline visibly fail. Measured over 262,144 emitted intervals, fitting
log-log over f ∈ [0.001, 0.05] cycles/frame:

| model | PSD slope | R² | lag-1 | lag-10 | lag-100 |
|---|---|---|---|---|---|
| 4-component OU (shipped) | **−0.930** | 0.987 | 0.685 | 0.364 | 0.097 |
| single OU, same σ | −1.727 | 0.993 | 0.891 | 0.812 | 0.337 |

The four-component sum is convincingly 1/f-like. The single-OU baseline
approaches the −2 Lorentzian asymptote and fails the spectral check —
**while passing every autocorrelation check with room to spare**, since its
autocorrelation is in fact *stronger*. Autocorrelation alone cannot tell the
two apart. Only the spectrum can. Report both in the paper.

Reproduce with `tools/analyze-intervals.py`.

### 5. Load coupling is partial, and this is where the model breaks

PRD section 8 flagged this as the hard, possibly open goal. It remains partly
open.

The only load signal available at the seam is **how late the compositor
thread's own timer task was delivered**, which `ClassifyLoad` turns into a
three-level `LoadHint` that scales the drop probability. That is a real signal:
a busy compositor thread genuinely does run its tasks late.

It is also a proxy for one thing only — compositor-thread scheduling pressure.
Load carried by raster workers, by the GPU process's other threads, or by GPU
queue depth does not show up. So:

* **Drop rate does respond to load**, via the hint. Verified in the unit tests
  (`DropsScaleWithLoadHint`).
* **Jitter magnitude does not.** The drift and noise terms are free-running and
  identical under any load. A detector that drives heavy rendering and measures
  whether cadence *variance* rises will separate this from real hardware.

The load-multiplier constants (1×, 8×, 40×) are plausible rather than measured.
Calibrating them against a real display is future work.

### 6. Configurations that bypass the patch entirely

`RootCompositorFrameSinkImpl::Create` chooses the BeginFrameSource. The patch
only affects `DelayBasedBeginFrameSource`, which is what Linux/Xvfb gets. These
configurations use something else and the clock will have no effect at all:

* `--enable-begin-frame-control`, and headless runs driven through DevTools
  `HeadlessExperimental.beginFrame` → `ExternalBeginFrameSourceMojo`
* `--disable-frame-rate-limit` → `BackToBackBeginFrameSource`
* macOS, Windows with DirectComposition, Android, iOS → platform-specific
  external sources

Check the startup log for the `Human Refresh Clock` banner. No banner means no
`DelayBasedTimeSource` was constructed and nothing is being modelled.

### 7. `BeginFrameArgs::interval` stays nominal — a deliberate cross-signal gap

`args.interval` comes from `time_source_->Interval()`, the stored nominal
value, and the patch leaves it alone. This is realistic (real hardware reports
its nominal refresh rate while actual vblank deltas vary around it) and
necessary (cc::Scheduler deadlines and CompositorFrameReporter trust the
field). But it does mean a detector with access to both the reported interval
and the observed cadence sees a mismatch that a perfectly-periodic build would
not show. Nothing exposes `args.interval` to JavaScript today.

## Validating

```sh
# unit + statistical tests
autoninja -C out/Release viz_unittests
./out/Release/viz_unittests --gtest_filter='RefreshClock*'

# spectral analysis of an emitted or observed trace
python3 tools/analyze-intervals.py trace.txt --expect-hz 60
python3 tools/analyze-intervals.py raf-trace.txt --expect-hz 60 --observed
```

Then open `test/refresh-clock.html` in the patched build — ideally served
cross-origin isolated, per finding 2 — and run the adversarial checks from PRD
section 22.

Baseline first: with the feature off, confirm the trace is perfectly periodic
and every check reports the stock behaviour. The patch must be inert by
default.

## Performance

Per frame: 2 Gaussians per drift component (4) plus 2 for the fine noise plus 1
uniform for the drop test — 11 `uint64` draws, 5 `log`/`cos` pairs, no
allocation, no locks, no atomics. State is 4 doubles of drift plus a 4-word RNG
state, well under 1 KB per time source.
