"""googleapiclient, loaded on first use.

The client library adds ~8 MiB to a server process (measured 2026-10-25), and most
client sessions never call a Google tool, so nothing imports it until a tool does.
`build` stays a module-level name so tests can patch it.
"""


def build(*args, **kwargs):
    from googleapiclient.discovery import build as _build

    return _build(*args, **kwargs)


def http_error():
    """googleapiclient's HttpError, for `except http_error() as e:`. An except
    expression is evaluated only once an exception is in flight."""
    from googleapiclient.errors import HttpError

    return HttpError


# Named like the classes they construct, so call sites (and tests that patch them) are unchanged.
def MediaInMemoryUpload(*args, **kwargs):
    from googleapiclient.http import MediaInMemoryUpload as _Upload

    return _Upload(*args, **kwargs)


def MediaIoBaseDownload(*args, **kwargs):
    from googleapiclient.http import MediaIoBaseDownload as _Download

    return _Download(*args, **kwargs)
