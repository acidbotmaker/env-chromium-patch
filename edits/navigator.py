"""navigator.platform, navigator.vendor and navigator.hardwareConcurrency."""

from . import Edit

BASE = "third_party/blink/renderer/core/execution_context/navigator_base.cc"
NAVIGATOR = "third_party/blink/renderer/core/frame/navigator.cc"
CONCURRENCY = "third_party/blink/renderer/core/frame/navigator_concurrent_hardware.cc"

# --- navigator.platform -------------------------------------------------
#
# NavigatorBase::platform() is the right chokepoint: it serves both Navigator
# (window) and WorkerNavigator. NavigatorID::platform() is effectively dead on
# desktop because NavigatorBase overrides it, and Navigator::platform() alone
# would miss workers.

BASE_INCLUDE_ANCHOR = """#include "third_party/blink/public/common/features.h"
#include "third_party/blink/renderer/core/execution_context/execution_context.h"
"""

BASE_INCLUDE_REPLACEMENT = """#include "third_party/blink/public/common/features.h"
#include "third_party/blink/public/common/switches.h"  // ENV_FP
#include "third_party/blink/renderer/core/execution_context/execution_context.h"
"""

BASE_PLATFORM_ANCHOR = """String NavigatorBase::platform() const {
#if BUILDFLAG(IS_ANDROID)
"""

BASE_PLATFORM_REPLACEMENT = """String NavigatorBase::platform() const {
  // ENV_FP: covers both Navigator and WorkerNavigator, so the value a worker
  // sees matches the main thread.
  const std::optional<std::string>& env_platform =
      blink::env_fingerprint::NavigatorPlatform();
  if (env_platform.has_value()) {
    return String::FromUTF8(*env_platform);
  }
#if BUILDFLAG(IS_ANDROID)
"""

# --- navigator.vendor ---------------------------------------------------

NAVIGATOR_INCLUDE_ANCHOR = """#include "third_party/blink/renderer/core/frame/navigator.h"

#include "third_party/blink/renderer/bindings/core/v8/script_controller.h"
"""

NAVIGATOR_INCLUDE_REPLACEMENT = """#include "third_party/blink/renderer/core/frame/navigator.h"

#include "third_party/blink/public/common/switches.h"  // ENV_FP
#include "third_party/blink/renderer/bindings/core/v8/script_controller.h"
"""

VENDOR_ANCHOR = """String Navigator::vendor() const {
  // Do not change without good cause. History:
  // https://code.google.com/p/chromium/issues/detail?id=276813
  // https://www.w3.org/Bugs/Public/show_bug.cgi?id=27786
  // https://groups.google.com/a/chromium.org/forum/#!topic/blink-dev/QrgyulnqvmE
  return "Google Inc.";
}
"""

VENDOR_REPLACEMENT = """String Navigator::vendor() const {
  // ENV_FP: stock value remains the default; only an explicit CHROME_ENV_VENDOR
  // changes it. The warning below still applies to anyone editing the default.
  const std::optional<std::string>& env_vendor =
      blink::env_fingerprint::NavigatorVendor();
  if (env_vendor.has_value()) {
    return String::FromUTF8(*env_vendor);
  }
  // Do not change without good cause. History:
  // https://code.google.com/p/chromium/issues/detail?id=276813
  // https://www.w3.org/Bugs/Public/show_bug.cgi?id=27786
  // https://groups.google.com/a/chromium.org/forum/#!topic/blink-dev/QrgyulnqvmE
  return "Google Inc.";
}
"""

# --- navigator.hardwareConcurrency --------------------------------------
#
# Deliberately NOT patching base::SysInfo::NumberOfProcessors(): that would
# resize the real renderer thread pool via content/common/thread_pool_util.cc.
# Only the JS-visible number changes. NavigatorConcurrentHardware is inherited
# once by NavigatorBase, so this covers window and worker scopes.

CONCURRENCY_ANCHOR = """#include "base/system/sys_info.h"

namespace blink {

unsigned NavigatorConcurrentHardware::hardwareConcurrency() const {
  return static_cast<unsigned>(base::SysInfo::NumberOfProcessors());
}
"""

CONCURRENCY_REPLACEMENT = """#include "base/system/sys_info.h"
#include "third_party/blink/public/common/switches.h"  // ENV_FP

namespace blink {

unsigned NavigatorConcurrentHardware::hardwareConcurrency() const {
  // ENV_FP: JS-visible value only. The real thread pool still sizes itself
  // from the actual processor count.
  std::optional<unsigned> env_concurrency =
      blink::env_fingerprint::HardwareConcurrency();
  if (env_concurrency.has_value()) {
    return *env_concurrency;
  }
  return static_cast<unsigned>(base::SysInfo::NumberOfProcessors());
}
"""


def edits(ctx: dict) -> list:
    return [
        Edit(
            path=BASE,
            anchor=BASE_INCLUDE_ANCHOR,
            replacement=BASE_INCLUDE_REPLACEMENT,
            marker='#include "third_party/blink/public/common/switches.h"  // ENV_FP',
            why="pull in the shared config accessors",
        ),
        Edit(
            path=BASE,
            anchor=BASE_PLATFORM_ANCHOR,
            replacement=BASE_PLATFORM_REPLACEMENT,
            marker="env_fingerprint::NavigatorPlatform()",
            why="CHROME_ENV_PLATFORM overrides navigator.platform",
        ),
        Edit(
            path=NAVIGATOR,
            anchor=NAVIGATOR_INCLUDE_ANCHOR,
            replacement=NAVIGATOR_INCLUDE_REPLACEMENT,
            marker='#include "third_party/blink/public/common/switches.h"  // ENV_FP',
            why="pull in the shared config accessors",
        ),
        Edit(
            path=NAVIGATOR,
            anchor=VENDOR_ANCHOR,
            replacement=VENDOR_REPLACEMENT,
            marker="env_fingerprint::NavigatorVendor()",
            why="CHROME_ENV_VENDOR overrides navigator.vendor",
        ),
        Edit(
            path=CONCURRENCY,
            anchor=CONCURRENCY_ANCHOR,
            replacement=CONCURRENCY_REPLACEMENT,
            marker="env_fingerprint::HardwareConcurrency()",
            why="CHROME_ENV_HARDWARE_CONCURRENCY overrides navigator.hardwareConcurrency",
        ),
    ]
