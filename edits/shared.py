"""The shared env/switch config reader, appended to blink's switches.{h,cc}.

Why here rather than a new component: ``blink_common`` is already linked into
the browser process, the renderer, and the GPU process, and both
``components/embedder_support`` and ``content/browser`` already depend on it.
Appending to two existing files means this patch adds no new source files and
touches no BUILD.gn, which is what keeps it applicable across milestones.  An
upstream CL would grow its own component instead.
"""

from . import Edit

HEADER = "third_party/blink/public/common/switches.h"
SOURCE = "third_party/blink/common/switches.cc"

# (accessor name, env var, switch name, doc)
VALUES = [
    ("UserAgent", "CHROME_ENV_UA", "env-fp-ua",
     "Full navigator.userAgent and outgoing User-Agent header."),
    ("UaPlatform", "CHROME_ENV_UA_PLATFORM", "env-fp-ua-platform",
     "navigator.userAgentData.platform and the Sec-CH-UA-Platform header."),
    ("UaPlatformVersion", "CHROME_ENV_UA_PLATFORM_VERSION", "env-fp-ua-platform-version",
     "Sec-CH-UA-Platform-Version and userAgentData platformVersion."),
    ("NavigatorPlatform", "CHROME_ENV_PLATFORM", "env-fp-platform",
     "navigator.platform, e.g. \"Win32\"."),
    ("NavigatorVendor", "CHROME_ENV_VENDOR", "env-fp-vendor",
     "navigator.vendor."),
    ("WebglRenderer", "CHROME_ENV_WEBGL_RENDERER", "env-fp-webgl-renderer",
     "UNMASKED_RENDERER_WEBGL."),
    ("WebglVendor", "CHROME_ENV_WEBGL_VENDOR", "env-fp-webgl-vendor",
     "UNMASKED_VENDOR_WEBGL."),
    ("WebglVersion", "CHROME_ENV_WEBGL_VERSION", "env-fp-webgl-version",
     "Driver string embedded in the GL_VERSION parameter."),
    ("WebglShadingLanguageVersion", "CHROME_ENV_WEBGL_SHADING_LANGUAGE_VERSION",
     "env-fp-webgl-glsl-version",
     "Driver string embedded in GL_SHADING_LANGUAGE_VERSION."),
    ("WebgpuVendor", "CHROME_ENV_WEBGPU_VENDOR", "env-fp-webgpu-vendor",
     "GPUAdapterInfo.vendor."),
    ("WebgpuArchitecture", "CHROME_ENV_WEBGPU_ARCHITECTURE", "env-fp-webgpu-architecture",
     "GPUAdapterInfo.architecture."),
    ("WebgpuDescription", "CHROME_ENV_WEBGPU_DESCRIPTION", "env-fp-webgpu-description",
     "GPUAdapterInfo.description (only exposed with WebGPU developer features)."),
    ("MediaDevicesJson", "CHROME_ENV_MEDIA_DEVICES", "env-fp-media-devices",
     "JSON describing the result of navigator.mediaDevices.enumerateDevices()."),
]

# navigator.platform and UA-CH platform use different vocabularies, so one is
# derived from the other rather than shared.
PLATFORM_MAP = [
    ("Win32", "Windows"),
    ("MacIntel", "macOS"),
    ("Linux x86_64", "Linux"),
    ("Linux i686", "Linux"),
    ("Linux armv81", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
]

HEADER_INCLUDE_ANCHOR = """#include "build/build_config.h"
#include "third_party/blink/public/common/common_export.h"
"""

HEADER_INCLUDE_REPLACEMENT = """#include <optional>
#include <string>

#include "build/build_config.h"
#include "third_party/blink/public/common/common_export.h"
"""

HEADER_TAIL_ANCHOR = """}  // namespace switches
}  // namespace blink

#endif  // THIRD_PARTY_BLINK_PUBLIC_COMMON_SWITCHES_H_
"""


def _header_tail() -> str:
    decls = []
    for name, env, switch, doc in VALUES:
        decls.append(f"// {doc}\n"
                     f"// Env: {env}   Switch: --{switch}\n"
                     f"BLINK_COMMON_EXPORT const std::optional<std::string>& {name}();\n")
    switch_decls = "\n".join(
        f"BLINK_COMMON_EXPORT extern const char k{name}Switch[];" for name, _, _, _ in VALUES
    )
    accessors = "\n".join(decls)
    return f"""}}  // namespace switches

// ===== ENV_FP: env fingerprint overrides (downstream patch) =====
//
// Launch-time overrides for the JS-visible device identity.  Values come from
// environment variables; each also has a command-line switch, because the
// browser forwards them to child processes for environments where the child's
// environment block is scrubbed.
//
// Every accessor returns nullopt when both the variable and the switch are
// unset, and callers fall back to stock behaviour.  An empty value counts as
// unset, so `CHROME_ENV_UA= chrome` behaves like not setting it at all.
//
// Values are read once and cached; these are called from hot JS entry points
// on several threads and getenv() is not safe against a concurrent setenv().
namespace env_fingerprint {{

{switch_decls}

{accessors}
// navigator.hardwareConcurrency. Clamped to [1, 1024].
// Env: CHROME_ENV_HARDWARE_CONCURRENCY   Switch: --env-fp-hardware-concurrency
BLINK_COMMON_EXPORT std::optional<unsigned> HardwareConcurrency();

// True if any override at all is configured. Cheap early-out for callers that
// would otherwise do work just to discover nothing is overridden.
BLINK_COMMON_EXPORT bool IsActive();

}}  // namespace env_fingerprint

}}  // namespace blink

#endif  // THIRD_PARTY_BLINK_PUBLIC_COMMON_SWITCHES_H_
"""


SOURCE_TAIL_ANCHOR = """}  // namespace switches
}  // namespace blink
"""


def _read_env_body(ctx: dict) -> str:
    """base::Environment::GetVar changed shape; emit whichever the tree has."""
    if ctx["optional_getvar"]:
        return """  base::Environment environment;
  std::optional<std::string> value = environment.GetVar(env_name);
  if (value.has_value() && !value->empty()) {
    return value;
  }"""
    return """  std::unique_ptr<base::Environment> environment(base::Environment::Create());
  std::string value;
  if (environment->GetVar(env_name, &value) && !value.empty()) {
    return value;
  }"""


def _source_tail(ctx: dict) -> str:
    switch_defs = "\n".join(
        f'const char k{name}Switch[] = "{switch}";' for name, _, switch, _ in VALUES
    )
    fields = "\n".join(
        f"  std::optional<std::string> {_snake(name)};" for name, _, _, _ in VALUES
    )
    init = "\n".join(_init_line(name, env) for name, env, _, _ in VALUES)
    accessors = "\n\n".join(
        f"""const std::optional<std::string>& {name}() {{
  return GetConfig().{_snake(name)};
}}"""
        for name, _, _, _ in VALUES
    )
    platform_map = "\n".join(
        f'          {{"{nav}", "{uach}"}},' for nav, uach in PLATFORM_MAP
    )

    return f"""}}  // namespace switches

// ===== ENV_FP: env fingerprint overrides (downstream patch) =====

namespace env_fingerprint {{

{switch_defs}

namespace {{

// Reads one value: environment variable first, then the matching switch.
std::optional<std::string> ReadValue(const char* env_name,
                                     const char* switch_name) {{
{_read_env_body(ctx)}

  // CommandLine is not initialised this early in every process type, and
  // asking for it before Init() would DCHECK.
  if (base::CommandLine::InitializedForCurrentProcess()) {{
    const base::CommandLine* command_line =
        base::CommandLine::ForCurrentProcess();
    if (command_line->HasSwitch(switch_name)) {{
      std::string from_switch = command_line->GetSwitchValueASCII(switch_name);
      if (!from_switch.empty()) {{
        return from_switch;
      }}
    }}
  }}

  return std::nullopt;
}}

struct Config {{
  Config() {{
{init}
    std::optional<std::string> concurrency =
        ReadValue("CHROME_ENV_HARDWARE_CONCURRENCY", kHardwareConcurrencySwitch);
    if (concurrency.has_value()) {{
      unsigned parsed = 0;
      if (base::StringToUint(*concurrency, &parsed) && parsed >= 1u) {{
        hardware_concurrency = std::min(parsed, 1024u);
      }} else {{
        LOG(WARNING) << "Ignoring invalid CHROME_ENV_HARDWARE_CONCURRENCY: "
                     << *concurrency;
      }}
    }}

    // navigator.platform says "Win32"; UA client hints say "Windows". Derive
    // one from the other so callers only have to set the common case.
    if (!ua_platform.has_value() && navigator_platform.has_value()) {{
      static constexpr std::pair<const char*, const char*> kPlatformMap[] = {{
{platform_map}
      }};
      for (const auto& [navigator_name, client_hint_name] : kPlatformMap) {{
        if (*navigator_platform == navigator_name) {{
          ua_platform = client_hint_name;
          break;
        }}
      }}
    }}
  }}

{fields}
  std::optional<unsigned> hardware_concurrency;
}};

const Config& GetConfig() {{
  static const base::NoDestructor<Config> config;
  return *config;
}}

}}  // namespace

{accessors}

std::optional<unsigned> HardwareConcurrency() {{
  return GetConfig().hardware_concurrency;
}}

bool IsActive() {{
  const Config& config = GetConfig();
  return {" ||\n         ".join(f"config.{_snake(name)}.has_value()" for name, _, _, _ in VALUES)} ||
         config.hardware_concurrency.has_value();
}}

}}  // namespace env_fingerprint

}}  // namespace blink
"""


SOURCE_INCLUDE_ANCHOR = """#include "third_party/blink/public/common/switches.h"

namespace blink {
namespace switches {
"""

SOURCE_INCLUDE_REPLACEMENT = """#include "third_party/blink/public/common/switches.h"

// ENV_FP: needed by the env fingerprint override block at the end of this file.
#include <algorithm>
#include <memory>
#include <utility>

#include "base/command_line.h"
#include "base/environment.h"
#include "base/logging.h"
#include "base/no_destructor.h"
#include "base/strings/string_number_conversions.h"

namespace blink {
namespace switches {
"""


def _init_line(name: str, env: str) -> str:
    """One Config field initialiser, wrapped to Chromium's 80 columns."""
    single = f'    {_snake(name)} = ReadValue("{env}", k{name}Switch);'
    if len(single) <= 80:
        return single
    return (f'    {_snake(name)} =\n'
            f'        ReadValue("{env}",\n'
            f'                  k{name}Switch);')


def _snake(name: str) -> str:
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


def edits(ctx: dict) -> list:
    header_tail = _header_tail()
    # kHardwareConcurrencySwitch is declared alongside the generated ones.
    header_tail = header_tail.replace(
        "BLINK_COMMON_EXPORT extern const char kMediaDevicesJsonSwitch[];",
        "BLINK_COMMON_EXPORT extern const char kMediaDevicesJsonSwitch[];\n"
        "BLINK_COMMON_EXPORT extern const char kHardwareConcurrencySwitch[];",
    )
    source_tail = _source_tail(ctx).replace(
        'const char kMediaDevicesJsonSwitch[] = "env-fp-media-devices";',
        'const char kMediaDevicesJsonSwitch[] = "env-fp-media-devices";\n'
        'const char kHardwareConcurrencySwitch[] = "env-fp-hardware-concurrency";',
    )

    return [
        Edit(
            path=HEADER,
            anchor=HEADER_INCLUDE_ANCHOR,
            replacement=HEADER_INCLUDE_REPLACEMENT,
            marker="#include <optional>\n#include <string>\n\n#include \"build/build_config.h\"",
            why="std::optional / std::string used by the accessor declarations",
        ),
        Edit(
            path=HEADER,
            anchor=HEADER_TAIL_ANCHOR,
            replacement=header_tail,
            marker="namespace env_fingerprint {",
            why="declare the env fingerprint accessors",
        ),
        Edit(
            path=SOURCE,
            anchor=SOURCE_INCLUDE_ANCHOR,
            replacement=SOURCE_INCLUDE_REPLACEMENT,
            marker="ENV_FP: needed by the env fingerprint override block",
            why="includes for env, command line and caching",
        ),
        Edit(
            path=SOURCE,
            anchor=SOURCE_TAIL_ANCHOR,
            replacement=source_tail,
            marker="namespace env_fingerprint {",
            why="define the env fingerprint accessors",
        ),
    ]
