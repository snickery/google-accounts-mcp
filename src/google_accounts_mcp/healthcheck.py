"""Container liveness probe: a full HTTP round-trip to /mcp (the bearer 401 counts
as alive), so HealthOnFailure=kill can restart a wedged event loop."""

from mcp_server_kit.healthcheck import probe

from google_accounts_mcp.config import PORT


def main() -> None:
    probe(PORT)


if __name__ == "__main__":
    main()
