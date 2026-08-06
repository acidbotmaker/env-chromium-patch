#!/usr/bin/env python3
"""Serve the test pages and print the identity headers of every request.

    python3 test/serve.py [port]        # default 8000

Two reasons this exists rather than `python3 -m http.server`:

Half of what the patch does is only visible on the wire. navigator.userAgent
can be read from JS, but the outgoing User-Agent and the Sec-CH-UA* client
hints cannot, and those are the ones that reveal a UA string and a brand list
that disagree. Every request is logged with them.

It also replies with Accept-CH, which is what makes the browser send the
high-entropy hints (platform version, full version list) on the *next* request
to this origin. Those travel through the include_high_entropy path of the
patch, so without this they are never exercised. Load the page, then reload it:
the first request shows the low-entropy set, the reload shows everything.

Serving over http://localhost also matters. localhost is a secure context, so
enumerateDevices() and getUserMedia behave as they would on a real site, which
file:// does not reliably reproduce.
"""

import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Requested from the browser so that the high-entropy hints appear on reload.
ACCEPT_CH = ", ".join([
    "Sec-CH-UA-Platform-Version",
    "Sec-CH-UA-Full-Version-List",
    "Sec-CH-UA-Full-Version",
    "Sec-CH-UA-Arch",
    "Sec-CH-UA-Bitness",
    "Sec-CH-UA-Model",
])

# Logged in this order; everything else in the request is noise here.
INTERESTING = [
    "User-Agent",
    "Sec-CH-UA",
    "Sec-CH-UA-Mobile",
    "Sec-CH-UA-Platform",
    "Sec-CH-UA-Platform-Version",
    "Sec-CH-UA-Full-Version-List",
    "Sec-CH-UA-Arch",
    "Sec-CH-UA-Bitness",
    "Sec-CH-UA-Model",
]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def end_headers(self):
        self.send_header("Accept-CH", ACCEPT_CH)
        self.send_header("Critical-CH", ACCEPT_CH)
        super().end_headers()

    def log_message(self, format, *args):  # noqa: A002 - base class spelling
        pass  # Replaced by the per-request dump below.

    def send_response(self, code, message=None):
        super().send_response(code, message)
        # Only worth dumping for navigations; subresources repeat the same set.
        if self.path.endswith((".html", "/")):
            print(f"\n=== {self.command} {self.path} -> {code} ===")
            for name in INTERESTING:
                value = self.headers.get(name)
                if value is not None:
                    print(f"  {name}: {value}")
            missing = [n for n in INTERESTING if self.headers.get(n) is None]
            if missing:
                print(f"  (not sent: {', '.join(missing)})")
            sys.stdout.flush()


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving {Path(__file__).parent} on http://localhost:{port}")
    print(f"  http://localhost:{port}/fingerprint.html")
    print(f"  http://localhost:{port}/capture-profile.html")
    print("Reload the page once to see the high-entropy client hints.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
