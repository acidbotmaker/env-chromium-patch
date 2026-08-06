"""WebGPU adapter info.

A completely separate code path from WebGL, so a WebGL override alone leaves
navigator.gpu reporting the real hardware. Only `vendor` and `architecture` are
exposed by default; `description` (and device/driver/backend) require
WebGPUDeveloperFeatures, and that gating is left as-is — only the values change.

The members are assigned in place rather than threaded through as locals so
that both the developer-features and the default GPUAdapterInfo construction
below pick them up from one edit.
"""

from . import Edit

PATH = "third_party/blink/renderer/modules/webgpu/gpu_adapter.cc"

INCLUDE_ANCHOR = """#include "services/metrics/public/cpp/ukm_builders.h"
#include "third_party/blink/renderer/bindings/core/v8/script_promise_resolver.h"
"""

INCLUDE_REPLACEMENT = """#include "services/metrics/public/cpp/ukm_builders.h"
#include "third_party/blink/public/common/switches.h"  // ENV_FP
#include "third_party/blink/renderer/bindings/core/v8/script_promise_resolver.h"
"""

ADAPTER_INFO_ANCHOR = """GPUAdapterInfo* GPUAdapter::CreateAdapterInfoForAdapter() {
  bool is_fallback_adapter = adapter_type_ == wgpu::AdapterType::CPU;
"""

ADAPTER_INFO_REPLACEMENT = """GPUAdapterInfo* GPUAdapter::CreateAdapterInfoForAdapter() {
  bool is_fallback_adapter = adapter_type_ == wgpu::AdapterType::CPU;

  // ENV_FP: applied before either GPUAdapterInfo construction below, so the
  // developer-features path and the default path agree.
  const std::optional<std::string>& env_vendor =
      blink::env_fingerprint::WebgpuVendor();
  if (env_vendor.has_value()) {
    vendor_ = String::FromUTF8(*env_vendor);
  }
  const std::optional<std::string>& env_architecture =
      blink::env_fingerprint::WebgpuArchitecture();
  if (env_architecture.has_value()) {
    architecture_ = String::FromUTF8(*env_architecture);
  }
  const std::optional<std::string>& env_description =
      blink::env_fingerprint::WebgpuDescription();
  if (env_description.has_value()) {
    description_ = String::FromUTF8(*env_description);
  }
"""


def edits(ctx: dict) -> list:
    return [
        Edit(
            path=PATH,
            anchor=INCLUDE_ANCHOR,
            replacement=INCLUDE_REPLACEMENT,
            marker='#include "third_party/blink/public/common/switches.h"  // ENV_FP',
            why="pull in the shared config accessors",
        ),
        Edit(
            path=PATH,
            anchor=ADAPTER_INFO_ANCHOR,
            replacement=ADAPTER_INFO_REPLACEMENT,
            marker="env_fingerprint::WebgpuVendor()",
            why="override GPUAdapterInfo vendor/architecture/description",
        ),
    ]
