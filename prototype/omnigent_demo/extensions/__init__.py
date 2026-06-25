"""Third-party EXTENSIONS package.

Importing this package runs each extension module's ``@extension(...)``
decorator, which self-registers it under the ``omnigent.extensions`` entry-point
group (the demo's in-memory stand-in for installed package metadata). After this
import, ``host.discover()`` finds them with no further coordination — the host
never names them.
"""

from __future__ import annotations

from . import bytedesk_ext  # noqa: F401 — import side-effect = self-registration

__all__ = ["bytedesk_ext"]
