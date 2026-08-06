"""WebGL renderer/vendor strings.

The masked GL_RENDERER ("WebKit WebGL") and GL_VENDOR ("WebKit") constants are
deliberately left alone: stock Chrome returns exactly those, so changing them
would be a tell rather than a disguise.

GL_VERSION and GL_SHADING_LANGUAGE_VERSION do need patching even though they
are not gated behind WEBGL_debug_renderer_info, because they embed the real
driver string ("WebGL 1.0 (OpenGL ES 3.0 (ANGLE 2.1 ... NVIDIA ...))") and
would otherwise give away the GPU that the renderer override just hid. WebGL2
defines its own cases for these two, so both files need the same treatment.
"""

from . import Edit

WEBGL1 = "third_party/blink/renderer/modules/webgl/webgl_rendering_context_base.cc"
WEBGL2 = "third_party/blink/renderer/modules/webgl/webgl2_rendering_context_base.cc"

INCLUDE_ANCHOR = """#include "third_party/blink/public/common/features.h"
"""
INCLUDE_REPLACEMENT = """#include "third_party/blink/public/common/features.h"
#include "third_party/blink/public/common/switches.h"  // ENV_FP
"""

UNMASKED_ANCHOR = """    case WebGLDebugRendererInfo::kUnmaskedRendererWebgl:
      if (ExtensionEnabled(kWebGLDebugRendererInfoName)) {
        return WebGLAny(script_state,
                        String(ContextGL()->GetString(GL_RENDERER)));
      }
      SynthesizeGLError(
          GL_INVALID_ENUM, "getParameter",
          "invalid parameter name, WEBGL_debug_renderer_info not enabled");
      return ScriptValue::CreateNull(script_state->GetIsolate());
    case WebGLDebugRendererInfo::kUnmaskedVendorWebgl:
      if (ExtensionEnabled(kWebGLDebugRendererInfoName)) {
        return WebGLAny(script_state,
                        String(ContextGL()->GetString(GL_VENDOR)));
      }
      SynthesizeGLError(
          GL_INVALID_ENUM, "getParameter",
          "invalid parameter name, WEBGL_debug_renderer_info not enabled");
      return ScriptValue::CreateNull(script_state->GetIsolate());
"""

# The ExtensionEnabled() gate and its SynthesizeGLError branch stay exactly as
# they were: answering when the extension is disabled would be a behavioural
# difference from stock Chrome, which is the opposite of what this is for.
UNMASKED_REPLACEMENT = """    case WebGLDebugRendererInfo::kUnmaskedRendererWebgl:
      if (ExtensionEnabled(kWebGLDebugRendererInfoName)) {
        // ENV_FP: substitute the value, keep the extension gate intact.
        const std::optional<std::string>& env_renderer =
            blink::env_fingerprint::WebglRenderer();
        if (env_renderer.has_value()) {
          return WebGLAny(script_state,
                          String::FromUTF8(*env_renderer));
        }
        return WebGLAny(script_state,
                        String(ContextGL()->GetString(GL_RENDERER)));
      }
      SynthesizeGLError(
          GL_INVALID_ENUM, "getParameter",
          "invalid parameter name, WEBGL_debug_renderer_info not enabled");
      return ScriptValue::CreateNull(script_state->GetIsolate());
    case WebGLDebugRendererInfo::kUnmaskedVendorWebgl:
      if (ExtensionEnabled(kWebGLDebugRendererInfoName)) {
        // ENV_FP: substitute the value, keep the extension gate intact.
        const std::optional<std::string>& env_vendor =
            blink::env_fingerprint::WebglVendor();
        if (env_vendor.has_value()) {
          return WebGLAny(script_state,
                          String::FromUTF8(*env_vendor));
        }
        return WebGLAny(script_state,
                        String(ContextGL()->GetString(GL_VENDOR)));
      }
      SynthesizeGLError(
          GL_INVALID_ENUM, "getParameter",
          "invalid parameter name, WEBGL_debug_renderer_info not enabled");
      return ScriptValue::CreateNull(script_state->GetIsolate());
"""

WEBGL1_VERSION_ANCHOR = """    case GL_VERSION:
      return WebGLAny(
          script_state,
          StrCat({"WebGL 1.0 (", String(ContextGL()->GetString(GL_VERSION)),
                  ")"}));
"""

WEBGL1_VERSION_REPLACEMENT = """    case GL_VERSION: {
      // ENV_FP: the inner driver string leaks the real GPU with no extension
      // required, so it follows the renderer override.
      const std::optional<std::string>& env_version =
          blink::env_fingerprint::WebglVersion();
      return WebGLAny(
          script_state,
          StrCat({"WebGL 1.0 (",
                  env_version.has_value()
                      ? String::FromUTF8(*env_version)
                      : String(ContextGL()->GetString(GL_VERSION)),
                  ")"}));
    }
"""

WEBGL1_GLSL_ANCHOR = """    case GL_SHADING_LANGUAGE_VERSION:
      return WebGLAny(
          script_state,
          StrCat({"WebGL GLSL ES 1.0 (",
                  String(ContextGL()->GetString(GL_SHADING_LANGUAGE_VERSION)),
                  ")"}));
"""

WEBGL1_GLSL_REPLACEMENT = """    case GL_SHADING_LANGUAGE_VERSION: {
      // ENV_FP: as for GL_VERSION.
      const std::optional<std::string>& env_glsl =
          blink::env_fingerprint::WebglShadingLanguageVersion();
      return WebGLAny(
          script_state,
          StrCat({"WebGL GLSL ES 1.0 (",
                  env_glsl.has_value()
                      ? String::FromUTF8(*env_glsl)
                      : String(ContextGL()->GetString(
                            GL_SHADING_LANGUAGE_VERSION)),
                  ")"}));
    }
"""

WEBGL2_VERSION_ANCHOR = """    case GL_VERSION:
      return WebGLAny(
          script_state,
          StrCat({"WebGL 2.0 (", String(ContextGL()->GetString(GL_VERSION)),
                  ")"}));
"""

WEBGL2_VERSION_REPLACEMENT = """    case GL_VERSION: {
      // ENV_FP: as in the WebGL1 base class.
      const std::optional<std::string>& env_version =
          blink::env_fingerprint::WebglVersion();
      return WebGLAny(
          script_state,
          StrCat({"WebGL 2.0 (",
                  env_version.has_value()
                      ? String::FromUTF8(*env_version)
                      : String(ContextGL()->GetString(GL_VERSION)),
                  ")"}));
    }
"""

WEBGL2_GLSL_ANCHOR = """    case GL_SHADING_LANGUAGE_VERSION: {
      return WebGLAny(
          script_state,
          StrCat({"WebGL GLSL ES 3.00 (",
                  String(ContextGL()->GetString(GL_SHADING_LANGUAGE_VERSION)),
                  ")"}));
    }
"""

WEBGL2_GLSL_REPLACEMENT = """    case GL_SHADING_LANGUAGE_VERSION: {
      // ENV_FP: as in the WebGL1 base class.
      const std::optional<std::string>& env_glsl =
          blink::env_fingerprint::WebglShadingLanguageVersion();
      return WebGLAny(
          script_state,
          StrCat({"WebGL GLSL ES 3.00 (",
                  env_glsl.has_value()
                      ? String::FromUTF8(*env_glsl)
                      : String(ContextGL()->GetString(
                            GL_SHADING_LANGUAGE_VERSION)),
                  ")"}));
    }
"""


def edits(ctx: dict) -> list:
    return [
        Edit(
            path=WEBGL1,
            anchor=INCLUDE_ANCHOR,
            replacement=INCLUDE_REPLACEMENT,
            marker='#include "third_party/blink/public/common/switches.h"  // ENV_FP',
            why="pull in the shared config accessors",
        ),
        Edit(
            path=WEBGL1,
            anchor=UNMASKED_ANCHOR,
            replacement=UNMASKED_REPLACEMENT,
            marker="env_fingerprint::WebglRenderer()",
            why="UNMASKED_RENDERER_WEBGL / UNMASKED_VENDOR_WEBGL overrides",
        ),
        Edit(
            path=WEBGL1,
            anchor=WEBGL1_VERSION_ANCHOR,
            replacement=WEBGL1_VERSION_REPLACEMENT,
            marker='StrCat({"WebGL 1.0 (",\n                  env_version',
            why="GL_VERSION leaks the driver string without any extension",
        ),
        Edit(
            path=WEBGL1,
            anchor=WEBGL1_GLSL_ANCHOR,
            replacement=WEBGL1_GLSL_REPLACEMENT,
            marker='StrCat({"WebGL GLSL ES 1.0 (",\n                  env_glsl',
            why="GL_SHADING_LANGUAGE_VERSION leaks the driver string too",
        ),
        Edit(
            path=WEBGL2,
            anchor=INCLUDE_ANCHOR,
            replacement=INCLUDE_REPLACEMENT,
            marker='#include "third_party/blink/public/common/switches.h"  // ENV_FP',
            why="pull in the shared config accessors",
        ),
        Edit(
            path=WEBGL2,
            anchor=WEBGL2_VERSION_ANCHOR,
            replacement=WEBGL2_VERSION_REPLACEMENT,
            marker='StrCat({"WebGL 2.0 (",\n                  env_version',
            why="WebGL2 defines its own GL_VERSION case",
        ),
        Edit(
            path=WEBGL2,
            anchor=WEBGL2_GLSL_ANCHOR,
            replacement=WEBGL2_GLSL_REPLACEMENT,
            marker='StrCat({"WebGL GLSL ES 3.00 (",\n                  env_glsl',
            why="WebGL2 defines its own GL_SHADING_LANGUAGE_VERSION case",
        ),
    ]
