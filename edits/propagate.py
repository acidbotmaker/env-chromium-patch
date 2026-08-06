"""Forward the overrides to child processes as command-line switches.

Renderers inherit the environment block on Linux (through the zygote), macOS
and Windows, so reading the env directly in a child usually works. This is the
belt-and-braces path for launchers and service managers that scrub the child
environment: the browser re-emits whatever it resolved as --env-fp-* switches,
and the shared reader prefers the env but falls back to the switch.

chrome_content_browser_client.cc already includes blink's switches.h, so no
include edit is needed here.
"""

from . import Edit
from .shared import VALUES

PATH = "chrome/browser/chrome_content_browser_client.cc"

ANCHOR = """void ChromeContentBrowserClient::AppendExtraCommandLineSwitches(
    base::CommandLine* command_line,
    int child_process_id) {
  crash_keys::AppendStringAnnotationsCommandLineSwitch(command_line);
"""


def _forwarding_block() -> str:
    lines = []
    for name, _env, _switch, _doc in VALUES:
        lines.append(
            f"    if (const std::optional<std::string>& {_snake(name)} =\n"
            f"            blink::env_fingerprint::{name}();\n"
            f"        {_snake(name)}.has_value()) {{\n"
            f"      command_line->AppendSwitchASCII(\n"
            f"          blink::env_fingerprint::k{name}Switch, *{_snake(name)});\n"
            f"    }}"
        )
    return "\n".join(lines)


def _snake(name: str) -> str:
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index:
            out.append("_")
        out.append(char.lower())
    return "".join(out)


REPLACEMENT_TEMPLATE = """void ChromeContentBrowserClient::AppendExtraCommandLineSwitches(
    base::CommandLine* command_line,
    int child_process_id) {{
  crash_keys::AppendStringAnnotationsCommandLineSwitch(command_line);

  // ENV_FP: re-emit the resolved overrides so children still see them where
  // the environment block is not inherited.
  if (blink::env_fingerprint::IsActive()) {{
{forwarding}
    if (std::optional<unsigned> hardware_concurrency =
            blink::env_fingerprint::HardwareConcurrency();
        hardware_concurrency.has_value()) {{
      command_line->AppendSwitchASCII(
          blink::env_fingerprint::kHardwareConcurrencySwitch,
          base::NumberToString(*hardware_concurrency));
    }}
  }}

"""


def edits(ctx: dict) -> list:
    return [
        Edit(
            path=PATH,
            anchor=ANCHOR,
            replacement=REPLACEMENT_TEMPLATE.format(forwarding=_forwarding_block()),
            marker="ENV_FP: re-emit the resolved overrides",
            why="survive environments that scrub the child process environment",
        ),
    ]
