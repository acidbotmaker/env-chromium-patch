"""User agent string and User-Agent Client Hints.

``components/embedder_support/user_agent_utils.cc`` is the browser-process
chokepoint for both.  It feeds the UA pushed to renderers over
``InitializeRenderer``, the network stack's ``user_agent``, worker global
scopes, and every ``Sec-CH-UA*`` request header, so overriding here keeps
``navigator.userAgent``, ``navigator.appVersion``, workers and outgoing headers
in agreement without any extra plumbing.

``navigator.appVersion`` needs no edit of its own: ``NavigatorID::appVersion()``
is defined as everything past the first '/' of the UA string, so it follows.
"""

from . import Edit

PATH = "components/embedder_support/user_agent_utils.cc"

INCLUDE_ANCHOR = """#include "third_party/blink/public/common/features.h"
#include "third_party/blink/public/common/user_agent/user_agent_metadata.h"
"""

INCLUDE_REPLACEMENT = """#include "third_party/blink/public/common/features.h"
#include "third_party/blink/public/common/switches.h"  // ENV_FP
#include "third_party/blink/public/common/user_agent/user_agent_metadata.h"
"""

GET_UA_ANCHOR = """std::string GetUserAgent() {
  std::optional<std::string> custom_ua = GetUserAgentFromCommandLine();
  if (custom_ua.has_value()) {
    return custom_ua.value();
  }

  return GetUserAgentInternal();
}
"""

GET_UA_REPLACEMENT = """// ENV_FP: same shape and same validation as GetUserAgentFromCommandLine(),
// reading CHROME_ENV_UA instead. An invalid value is ignored rather than
// allowed to produce a malformed request header.
std::optional<std::string> GetUserAgentFromEnvironment() {
  const std::optional<std::string>& env_ua = blink::env_fingerprint::UserAgent();
  if (!env_ua.has_value()) {
    return std::nullopt;
  }
  if (!net::HttpUtil::IsValidHeaderValue(*env_ua)) {
    LOG(WARNING) << "Ignored invalid value for CHROME_ENV_UA";
    return std::nullopt;
  }
  return env_ua;
}

std::string GetUserAgent() {
  // ENV_FP: the environment wins over --user-agent when both are present.
  std::optional<std::string> env_ua = GetUserAgentFromEnvironment();
  if (env_ua.has_value()) {
    return env_ua.value();
  }

  std::optional<std::string> custom_ua = GetUserAgentFromCommandLine();
  if (custom_ua.has_value()) {
    return custom_ua.value();
  }

  return GetUserAgentInternal();
}
"""

METADATA_HELPER_ANCHOR = """blink::UserAgentMetadata GetUserAgentMetadata(bool only_low_entropy_ch) {
  blink::UserAgentMetadata metadata;
"""

METADATA_HELPER_REPLACEMENT = """namespace {

// ENV_FP: rewrites client hints so they agree with the spoofed UA string.
//
// The brand lists are rewritten by matching on the real version rather than by
// brand name, which leaves the GREASE entry (whose version is unrelated)
// alone while catching both "Chromium" and "Google Chrome".
void ApplyEnvFingerprintMetadataOverrides(blink::UserAgentMetadata* metadata,
                                          bool include_high_entropy) {
  const std::optional<std::string>& ua_platform =
      blink::env_fingerprint::UaPlatform();
  if (ua_platform.has_value()) {
    metadata->platform = *ua_platform;
  }

  if (!include_high_entropy) {
    return;
  }

  const std::optional<std::string>& platform_version =
      blink::env_fingerprint::UaPlatformVersion();
  if (platform_version.has_value()) {
    metadata->platform_version = *platform_version;
  }

  // Align the advertised Chrome version with whatever the spoofed UA claims,
  // otherwise Sec-CH-UA reports the real build while the UA string reports
  // another.
  const std::optional<std::string>& env_ua = blink::env_fingerprint::UserAgent();
  if (!env_ua.has_value()) {
    return;
  }
  static constexpr char kChromeToken[] = "Chrome/";
  size_t token_pos = env_ua->find(kChromeToken);
  if (token_pos == std::string::npos) {
    return;
  }
  size_t version_start = token_pos + (sizeof(kChromeToken) - 1);
  size_t version_end = env_ua->find(' ', version_start);
  std::string spoofed_full_version =
      env_ua->substr(version_start, version_end == std::string::npos
                                        ? std::string::npos
                                        : version_end - version_start);
  if (spoofed_full_version.empty()) {
    return;
  }
  std::string spoofed_major_version =
      spoofed_full_version.substr(0, spoofed_full_version.find('.'));

  const std::string real_major_version(version_info::GetMajorVersionNumber());
  const std::string real_full_version(version_info::GetVersionNumber());

  for (auto& brand_version : metadata->brand_version_list) {
    if (brand_version.version == real_major_version) {
      brand_version.version = spoofed_major_version;
    }
  }
  for (auto& brand_version : metadata->brand_full_version_list) {
    if (brand_version.version == real_full_version) {
      brand_version.version = spoofed_full_version;
    }
  }
  if (metadata->full_version == real_full_version) {
    metadata->full_version = spoofed_full_version;
  }
}

}  // namespace

blink::UserAgentMetadata GetUserAgentMetadata(bool only_low_entropy_ch) {
  blink::UserAgentMetadata metadata;
"""

METADATA_EARLY_RETURN_ANCHOR = """  std::optional<std::string> custom_ua = GetUserAgentFromCommandLine();
  if (custom_ua.has_value()) {
    return base::FeatureList::IsEnabled(blink::features::kUACHOverrideBlank)
               ? blink::UserAgentMetadata()
               : metadata;
  }

  if (only_low_entropy_ch) {
    return metadata;
  }
"""

METADATA_EARLY_RETURN_REPLACEMENT = """  // ENV_FP: the truncate-to-low-entropy (or blank) behaviour below is right for
  // --user-agent but wrong for us. A browser that sends a full UA string
  // alongside empty client hints is trivially identifiable, so the env-driven
  // path skips it and rewrites the hints to match instead.
  if (!blink::env_fingerprint::UserAgent().has_value()) {
    std::optional<std::string> custom_ua = GetUserAgentFromCommandLine();
    if (custom_ua.has_value()) {
      return base::FeatureList::IsEnabled(blink::features::kUACHOverrideBlank)
                 ? blink::UserAgentMetadata()
                 : metadata;
    }
  }

  if (only_low_entropy_ch) {
    ApplyEnvFingerprintMetadataOverrides(&metadata,
                                         /*include_high_entropy=*/false);
    return metadata;
  }
"""

METADATA_TAIL_ANCHOR = """  metadata.platform_version = GetPlatformVersion();
  return metadata;
}
"""

METADATA_TAIL_REPLACEMENT = """  metadata.platform_version = GetPlatformVersion();
  ApplyEnvFingerprintMetadataOverrides(&metadata,
                                       /*include_high_entropy=*/true);  // ENV_FP
  return metadata;
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
            anchor=GET_UA_ANCHOR,
            replacement=GET_UA_REPLACEMENT,
            marker="GetUserAgentFromEnvironment",
            why="CHROME_ENV_UA overrides the user agent string",
        ),
        Edit(
            path=PATH,
            anchor=METADATA_HELPER_ANCHOR,
            replacement=METADATA_HELPER_REPLACEMENT,
            marker="ApplyEnvFingerprintMetadataOverrides",
            why="helper that rewrites client hints to match the spoofed UA",
        ),
        Edit(
            path=PATH,
            anchor=METADATA_EARLY_RETURN_ANCHOR,
            replacement=METADATA_EARLY_RETURN_REPLACEMENT,
            marker="the env-driven\n  // path skips it",
            why="do not blank client hints when the override comes from the env",
        ),
        Edit(
            path=PATH,
            anchor=METADATA_TAIL_ANCHOR,
            replacement=METADATA_TAIL_REPLACEMENT,
            marker="/*include_high_entropy=*/true);  // ENV_FP",
            why="apply overrides on the high-entropy path",
        ),
    ]
