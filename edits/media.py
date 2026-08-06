"""navigator.mediaDevices.enumerateDevices().

Enumeration is entirely browser-process, IO thread, so one patch here cannot be
bypassed by a compromised renderer.

The injection point is MediaDevicesManager::DevicesEnumerated(), which every
low-level enumeration funnels through. Landing there means the synthetic list
reaches current_snapshot_ and so stays consistent across enumerateDevices(),
`devicechange` events, and getUserMedia's deviceId resolution.

It is also upstream of TranslateMediaDeviceInfo(), which buys two behaviours
for free:

  * device_id and group_id get per-origin HMAC hashing, so the fakes are
    indistinguishable from real devices; and
  * a page without camera/mic permission still gets exactly one blank entry per
    type, matching stock Chrome.

The consequence of the first is that JS sees a 64-char hex HMAC, not the
deviceId written in the JSON. Only "", "default" and "communications" pass
through verbatim.
"""

from . import Edit

SOURCE = "content/browser/renderer_host/media/media_devices_manager.cc"
HEADER = "content/browser/renderer_host/media/media_devices_manager.h"

HEADER_MEMBER_ANCHOR = """  bool use_fake_devices_;
  const raw_ptr<media::AudioSystem, DanglingUntriaged>
      audio_system_;  // not owned
"""

HEADER_MEMBER_REPLACEMENT = """  bool use_fake_devices_;
  // ENV_FP: parsed once from CHROME_ENV_MEDIA_DEVICES in the constructor.
  // Types absent from the JSON keep their real enumeration.
  std::array<std::optional<blink::WebMediaDeviceInfoArray>,
             static_cast<size_t>(MediaDeviceType::kNumMediaDeviceTypes)>
      env_devices_override_;
  const raw_ptr<media::AudioSystem, DanglingUntriaged>
      audio_system_;  // not owned
"""

HEADER_INCLUDE_ANCHOR = """#include <array>
#include <map>
#include <memory>
#include <string>
"""

HEADER_INCLUDE_REPLACEMENT = """#include <array>
#include <map>
#include <memory>
#include <optional>  // ENV_FP
#include <string>
"""

SOURCE_INCLUDE_ANCHOR = """#include "content/browser/renderer_host/media/media_devices_manager.h"
"""

SOURCE_INCLUDE_REPLACEMENT = """#include "content/browser/renderer_host/media/media_devices_manager.h"

// ENV_FP
#include "base/json/json_reader.h"
#include "base/values.h"
#include "third_party/blink/public/common/switches.h"
"""

CTOR_ANCHOR = """    : use_fake_devices_(base::CommandLine::ForCurrentProcess()->HasSwitch(
          switches::kUseFakeDeviceForMediaStream)),
"""

CTOR_REPLACEMENT = """    : use_fake_devices_(base::CommandLine::ForCurrentProcess()->HasSwitch(
          switches::kUseFakeDeviceForMediaStream)),
      env_devices_override_(ParseEnvMediaDevices()),  // ENV_FP
"""

DEVICES_ENUMERATED_ANCHOR = """void MediaDevicesManager::DevicesEnumerated(
    uint64_t request_id,
    MediaDeviceType type,
    const blink::WebMediaDeviceInfoArray& snapshot) {
  DCHECK_CURRENTLY_ON(BrowserThread::IO);
  DCHECK(blink::IsValidMediaDeviceType(type));
  UpdateSnapshot(type, snapshot);
"""

DEVICES_ENUMERATED_REPLACEMENT = """void MediaDevicesManager::DevicesEnumerated(
    uint64_t request_id,
    MediaDeviceType type,
    const blink::WebMediaDeviceInfoArray& snapshot) {
  DCHECK_CURRENTLY_ON(BrowserThread::IO);
  DCHECK(blink::IsValidMediaDeviceType(type));
  // ENV_FP: substituting here rather than at the point of reply keeps the
  // synthetic devices in current_snapshot_, so device-change notifications and
  // getUserMedia's deviceId lookup see the same list as enumerateDevices().
  const std::optional<blink::WebMediaDeviceInfoArray>& env_override =
      env_devices_override_[static_cast<size_t>(type)];
  UpdateSnapshot(type, env_override.has_value() ? *env_override : snapshot);
"""

# ProcessClientRequests() unconditionally overwrites every video device's
# group_id with a heuristic that guesses from matching audio labels. That would
# discard the groupId supplied in the JSON, so skip it when video input is
# overridden.
GROUP_ID_ANCHOR = """  if (cache_is_populated_[static_cast<size_t>(
          MediaDeviceType::kMediaVideoInput)]) {
    blink::WebMediaDeviceInfoArray video_devices =
        current_snapshot_[static_cast<size_t>(
            MediaDeviceType::kMediaVideoInput)];
"""

GROUP_ID_REPLACEMENT = """  // ENV_FP: the guessing heuristic below would overwrite the groupId values
  // supplied in CHROME_ENV_MEDIA_DEVICES.
  if (!env_devices_override_[static_cast<size_t>(
                                 MediaDeviceType::kMediaVideoInput)]
           .has_value() &&
      cache_is_populated_[static_cast<size_t>(
          MediaDeviceType::kMediaVideoInput)]) {
    blink::WebMediaDeviceInfoArray video_devices =
        current_snapshot_[static_cast<size_t>(
            MediaDeviceType::kMediaVideoInput)];
"""

# Parser, dropped into the existing anonymous namespace next to the
# GetFakeAudioDevices() helper it is modelled on.
PARSER_ANCHOR = """blink::WebMediaDeviceInfoArray GetFakeAudioDevices(bool is_input) {
"""


def _parser(ctx: dict) -> str:
    dict_type = ctx["dict_type"]
    list_type = ctx["list_type"]
    read_dict = (
        f"std::optional<{dict_type}> parsed =\n"
        f"      base::JSONReader::ReadDict(*json, base::JSON_PARSE_RFC);"
        if ctx["has_read_dict"]
        else
        "std::optional<base::Value> parsed_value =\n"
        "      base::JSONReader::Read(*json, base::JSON_PARSE_RFC);\n"
        f"  const {dict_type}* parsed_ptr =\n"
        "      parsed_value ? parsed_value->GetIfDict() : nullptr;"
    )
    if ctx["has_read_dict"]:
        parsed_guard = """  if (!parsed.has_value()) {
    LOG(ERROR) << "CHROME_ENV_MEDIA_DEVICES is not valid JSON; "
                  "falling back to real device enumeration";
    return result;
  }
  const auto& root = *parsed;"""
    else:
        parsed_guard = """  if (!parsed_ptr) {
    LOG(ERROR) << "CHROME_ENV_MEDIA_DEVICES is not a valid JSON object; "
                  "falling back to real device enumeration";
    return result;
  }
  const auto& root = *parsed_ptr;"""

    return f"""// ENV_FP: builds the synthetic device list from CHROME_ENV_MEDIA_DEVICES.
//
// Shape, with every key optional -- an absent key means "enumerate for real":
//
//   {{
//     "audioinput":  [{{"deviceId": "...", "label": "...", "groupId": "..."}}],
//     "videoinput":  [{{..., "facingMode": "user"|"environment"|"left"|"right"}}],
//     "audiooutput": [{{...}}]
//   }}
//
// deviceId and groupId are the *raw* ids; they are HMAC'd per origin
// downstream, so JS will not see these strings verbatim.
std::array<std::optional<blink::WebMediaDeviceInfoArray>,
           static_cast<size_t>(MediaDeviceType::kNumMediaDeviceTypes)>
ParseEnvMediaDevices() {{
  std::array<std::optional<blink::WebMediaDeviceInfoArray>,
             static_cast<size_t>(MediaDeviceType::kNumMediaDeviceTypes)>
      result;

  const std::optional<std::string>& json =
      blink::env_fingerprint::MediaDevicesJson();
  if (!json.has_value()) {{
    return result;
  }}

  {read_dict}
{parsed_guard}

  static constexpr std::pair<const char*, MediaDeviceType> kKeys[] = {{
      {{"audioinput", MediaDeviceType::kMediaAudioInput}},
      {{"videoinput", MediaDeviceType::kMediaVideoInput}},
      {{"audiooutput", MediaDeviceType::kMediaAudioOutput}},
  }};

  for (const auto& [key, device_type] : kKeys) {{
    const {list_type}* entries = root.FindList(key);
    if (!entries) {{
      continue;
    }}
    blink::WebMediaDeviceInfoArray devices;
    for (const base::Value& entry : *entries) {{
      const {dict_type}* device = entry.GetIfDict();
      if (!device) {{
        LOG(WARNING) << "Skipping non-object entry in CHROME_ENV_MEDIA_DEVICES."
                     << key;
        continue;
      }}
      const std::string* device_id = device->FindString("deviceId");
      const std::string* label = device->FindString("label");
      const std::string* group_id = device->FindString("groupId");
      if (!device_id || !label) {{
        LOG(WARNING) << "Skipping CHROME_ENV_MEDIA_DEVICES." << key
                     << " entry without both deviceId and label";
        continue;
      }}

      blink::mojom::FacingMode facing = blink::mojom::FacingMode::kNone;
      if (const std::string* facing_name = device->FindString("facingMode")) {{
        if (*facing_name == "user") {{
          facing = blink::mojom::FacingMode::kUser;
        }} else if (*facing_name == "environment") {{
          facing = blink::mojom::FacingMode::kEnvironment;
        }} else if (*facing_name == "left") {{
          facing = blink::mojom::FacingMode::kLeft;
        }} else if (*facing_name == "right") {{
          facing = blink::mojom::FacingMode::kRight;
        }} else {{
          LOG(WARNING) << "Unknown facingMode \\"" << *facing_name << "\\"";
        }}
      }}

      devices.emplace_back(*device_id, *label,
                           group_id ? *group_id : std::string(),
                           media::VideoCaptureControlSupport(), facing);
    }}
    result[static_cast<size_t>(device_type)] = std::move(devices);
  }}

  return result;
}}

blink::WebMediaDeviceInfoArray GetFakeAudioDevices(bool is_input) {{
"""


def edits(ctx: dict) -> list:
    return [
        Edit(
            path=HEADER,
            anchor=HEADER_INCLUDE_ANCHOR,
            replacement=HEADER_INCLUDE_REPLACEMENT,
            marker="#include <optional>  // ENV_FP",
            why="std::optional used by the override member",
        ),
        Edit(
            path=HEADER,
            anchor=HEADER_MEMBER_ANCHOR,
            replacement=HEADER_MEMBER_REPLACEMENT,
            marker="env_devices_override_",
            why="hold the parsed device list for the manager's lifetime",
        ),
        Edit(
            path=SOURCE,
            anchor=SOURCE_INCLUDE_ANCHOR,
            replacement=SOURCE_INCLUDE_REPLACEMENT,
            marker="// ENV_FP\n#include \"base/json/json_reader.h\"",
            why="JSON parsing and the shared config accessors",
        ),
        Edit(
            path=SOURCE,
            anchor=PARSER_ANCHOR,
            replacement=_parser(ctx),
            marker="ParseEnvMediaDevices",
            why="parse CHROME_ENV_MEDIA_DEVICES once",
        ),
        Edit(
            path=SOURCE,
            anchor=CTOR_ANCHOR,
            replacement=CTOR_REPLACEMENT,
            marker="env_devices_override_(ParseEnvMediaDevices())",
            why="parse at construction, matching how use_fake_devices_ is read",
        ),
        Edit(
            path=SOURCE,
            anchor=DEVICES_ENUMERATED_ANCHOR,
            replacement=DEVICES_ENUMERATED_REPLACEMENT,
            marker="ENV_FP: substituting here rather than at the point of reply",
            why="substitute the synthetic list into the snapshot",
        ),
        Edit(
            path=SOURCE,
            anchor=GROUP_ID_ANCHOR,
            replacement=GROUP_ID_REPLACEMENT,
            marker="would overwrite the groupId values",
            why="preserve JSON-supplied groupId values",
        ),
    ]
