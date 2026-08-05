"""Container liveness probe (referenced by the Quadlet HealthCmd).

A blocked/wedged event loop is the failure mode we care about: the kernel
still accepts the TCP connection, but uvicorn never completes the HTTP
response. A bare TCP connect would therefore look healthy while the server
is hung. So we require a full HTTP round-trip within a short timeout. The
bearer middleware answers this unauthenticated probe with 401 — that is a
*completed* response, which is exactly the liveness signal we want. Only a
transport-level failure or a timeout marks the container unhealthy, at which
point ``HealthOnFailure=kill`` + ``Restart=always`` self-heal it.
"""

import sys
import urllib.error
import urllib.request

from google_accounts_mcp.config import PORT


def main() -> None:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/mcp", timeout=5)
    except urllib.error.HTTPError:
        # Any HTTP status (incl. 401) means the loop served the request.
        return
    except Exception as exc:  # noqa: BLE001 - any transport failure = unhealthy
        print(f"health: unreachable on :{PORT}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
