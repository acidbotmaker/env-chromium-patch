#!/usr/bin/env python3
"""Statistical validation for a captured frame-interval trace.

Implements the checks in PRD sections 16 and 21, including the spectral check
that a single AR(1) baseline is supposed to fail.

Input is one interval in milliseconds per line (blank lines and lines starting
with '#' are ignored). Both the emitted trace from the C++ side and a
requestAnimationFrame trace captured in the browser are accepted; analyse them
separately, since the observed trace carries real OS scheduler jitter on top.

    python3 tools/analyze-intervals.py emitted.txt --expect-hz 60
    python3 tools/analyze-intervals.py raf.txt --expect-hz 60 --observed

Uses numpy when available and falls back to a pure-Python FFT otherwise.
"""

import argparse
import cmath
import math
import sys
from pathlib import Path

try:
    import numpy as _np
except ImportError:
    _np = None


# --- basic statistics ----------------------------------------------------

def mean(values):
    return sum(values) / len(values)


def stddev(values):
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def autocorrelation(values, lag):
    m = mean(values)
    centered = [v - m for v in values]
    denom = sum(c * c for c in centered)
    if denom == 0:
        return 0.0
    numer = sum(centered[i] * centered[i + lag]
                for i in range(len(centered) - lag))
    return numer / denom


# --- spectrum ------------------------------------------------------------

def _fft(values):
    """Iterative radix-2 Cooley-Tukey. len(values) must be a power of two."""
    n = len(values)
    data = [complex(v) for v in values]

    # Bit-reversal permutation.
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            data[i], data[j] = data[j], data[i]

    length = 2
    while length <= n:
        angle = -2j * math.pi / length
        step = cmath.exp(angle)
        for start in range(0, n, length):
            w = 1 + 0j
            half = length // 2
            for k in range(start, start + half):
                even = data[k]
                odd = data[k + half] * w
                data[k] = even + odd
                data[k + half] = even - odd
                w *= step
        length <<= 1
    return data


def welch_psd(values, segment=4096):
    """Averaged periodogram. Returns (frequencies, power), DC excluded.

    Frequencies are in cycles per frame, so 0.5 is the Nyquist limit.
    """
    if len(values) < segment:
        segment = 1 << (len(values).bit_length() - 1)
    if segment < 64:
        raise SystemExit("need at least 64 samples for a spectrum")

    m = mean(values)
    centered = [v - m for v in values]

    # Hann window, 50% overlap.
    window = [0.5 - 0.5 * math.cos(2 * math.pi * i / segment)
              for i in range(segment)]
    window_power = sum(w * w for w in window)

    step = segment // 2
    starts = range(0, len(centered) - segment + 1, step)
    accum = [0.0] * (segment // 2)
    count = 0

    for start in starts:
        chunk = centered[start:start + segment]
        if _np is not None:
            arr = _np.asarray(chunk) * _np.asarray(window)
            spec = _np.fft.rfft(arr)[: segment // 2]
            power = (spec.real ** 2 + spec.imag ** 2) / window_power
            for i in range(segment // 2):
                accum[i] += float(power[i])
        else:
            windowed = [chunk[i] * window[i] for i in range(segment)]
            spec = _fft(windowed)
            for i in range(segment // 2):
                accum[i] += (spec[i].real ** 2 + spec[i].imag ** 2) / window_power
        count += 1

    if count == 0:
        raise SystemExit("not enough samples for one spectral segment")

    freqs = [i / segment for i in range(1, segment // 2)]
    power = [accum[i] / count for i in range(1, segment // 2)]
    return freqs, power


def loglog_slope(freqs, power, low, high):
    """Least-squares slope of log10(power) vs log10(freq) over a band.

    Returns (slope, r_squared, n_points). A 1/f process gives a slope near -1
    with a good fit; a single AR(1)/OU process is flat at low frequency and
    falls at -2 at high frequency, so a straight-line fit over a wide band
    yields an intermediate slope with a visibly worse r_squared.
    """
    xs, ys = [], []
    for f, p in zip(freqs, power):
        if low <= f <= high and p > 0:
            xs.append(math.log10(f))
            ys.append(math.log10(p))
    if len(xs) < 8:
        return float("nan"), float("nan"), len(xs)

    mx, my = mean(xs), mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sxy / sxx
    intercept = my - slope * mx

    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r_squared = 1 - ss_res / ss_tot if ss_tot else float("nan")
    return slope, r_squared, len(xs)


# --- report --------------------------------------------------------------

def load(path):
    values = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate CSV: take the last numeric field.
        for field in reversed(line.replace(",", " ").split()):
            try:
                values.append(float(field))
                break
            except ValueError:
                continue
    if not values:
        raise SystemExit(f"no numeric samples found in {path}")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace", help="file of intervals in ms, one per line")
    parser.add_argument("--expect-hz", type=float, default=60.0)
    parser.add_argument("--observed", action="store_true",
                        help="trace came from the browser, so relax the checks "
                             "that only apply to the emitted sequence")
    parser.add_argument("--segment", type=int, default=4096,
                        help="FFT segment length (power of two)")
    args = parser.parse_args()

    values = load(args.trace)
    base = 1000.0 / args.expect_hz

    # A dropped frame carries a whole extra period. It is a different
    # phenomenon from the jitter and dominates the variance if left in: a
    # handful of drops in 100k frames can triple the standard deviation and
    # cut the measured lag-1 autocorrelation by a factor of six.
    drop_threshold = base * 1.5
    dropped = [v for v in values if v >= drop_threshold]
    jitter = [v for v in values if v < drop_threshold]
    # De-dropped series keeps the sample positions, so lags stay meaningful.
    dedropped = [v - base * round((v - base) / base) if v >= drop_threshold else v
                 for v in values]

    failures = []

    def check(ok, label, detail):
        print(f"  [{'PASS' if ok else 'FAIL'}] {label:<44} {detail}")
        if not ok:
            failures.append(label)

    print(f"\n{args.trace}: {len(values)} samples, target {base:.6f} ms "
          f"({args.expect_hz:g} Hz)\n")

    print("Marginal statistics")
    observed_mean = mean(values)
    error_pct = abs(observed_mean - base) / base * 100
    check(error_pct < 0.05, "mean within 0.05% of target",
          f"{observed_mean:.6f} ms  ({error_pct:.4f}%)")

    sd = stddev(jitter) if len(jitter) > 1 else float("nan")
    lo, hi = (0.05, 0.12) if not args.observed else (0.05, 0.60)
    check(lo <= sd <= hi, f"stddev in [{lo}, {hi}] ms (drops excluded)",
          f"{sd:.4f} ms")

    drop_rate = len(dropped) / len(values)
    print(f"  [ -- ] {'dropped frames':<44} {len(dropped)} "
          f"({drop_rate * 100:.4f}%)")

    print("\nDistribution shape")
    m, s = mean(jitter), sd
    within1 = sum(1 for v in jitter if abs(v - m) <= s) / len(jitter)
    within2 = sum(1 for v in jitter if abs(v - m) <= 2 * s) / len(jitter)
    check(0.60 <= within1 <= 0.75, "~68% within 1 sigma (Gaussian body)",
          f"{within1 * 100:.1f}%")
    check(0.93 <= within2 <= 0.98, "~95% within 2 sigma",
          f"{within2 * 100:.1f}%")

    # Boundary pile-up: the outermost histogram bins must not be overfull,
    # which is what a hard clamp on the drift would produce.
    bins = 60
    edges_lo, edges_hi = m - 4 * s, m + 4 * s
    width = (edges_hi - edges_lo) / bins
    counts = [0] * bins
    for v in jitter:
        idx = int((v - edges_lo) / width)
        if 0 <= idx < bins:
            counts[idx] += 1
    peak = max(counts)
    edge_mass = (counts[0] + counts[-1]) / max(peak, 1)
    check(edge_mass < 0.10, "no pile-up in outermost bins",
          f"edge/peak = {edge_mass:.3f}")

    print("\nTemporal structure (drops removed)")
    for lag in (1, 10, 100):
        ac = autocorrelation(dedropped, lag)
        floor = {1: 0.5, 10: 0.15, 100: 0.02}[lag]
        if args.observed:
            floor = {1: 0.05, 10: 0.02, 100: -1.0}[lag]
        check(ac > floor, f"autocorrelation lag-{lag} > {floor}", f"{ac:.4f}")

    print("\nSpectrum")
    freqs, power = welch_psd(dedropped, segment=args.segment)
    # Fit across the band the drift components actually span: from a few
    # segment bins above DC up to where the white fine-noise floor takes over.
    slope, r2, n = loglog_slope(freqs, power, low=4.0 / args.segment, high=0.05)
    print(f"  [ -- ] {'log-log fit points':<44} {n}")
    check(-1.6 < slope < -0.4, "PSD slope near -1 (1/f-like)",
          f"slope = {slope:.3f}, R^2 = {r2:.3f}")

    print("\nDecade-by-decade power (a flat run means a Lorentzian knee,")
    print("a steady fall means 1/f):")
    for lo_f, hi_f in ((0.0005, 0.002), (0.002, 0.008),
                       (0.008, 0.03), (0.03, 0.12)):
        band = [p for f, p in zip(freqs, power) if lo_f <= f < hi_f]
        if band:
            print(f"    f in [{lo_f:.4f}, {hi_f:.4f})  "
                  f"mean power {mean(band):.4e}")

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
