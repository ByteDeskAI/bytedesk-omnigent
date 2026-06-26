"""First-party plugin — the ``omnigent.secrets`` secret-backend plugin (BDP-2509).

This dogfoods the kernel seam contract for the ``omnigent/onboarding/secrets.py``
subpackage (Section 9.1 of ``docs/EXTENSION_FRAMEWORK_ANALYSIS.md``). Instead of
the ``LocalBackend`` being an implicit, always-present tail of the chain that the
secrets module hard-codes, it is expressed as a *first-party plugin* registered
into the kernel's ``secret_backends`` seam through the exact same
:class:`omnigent.kernel.extensions.OmnigentExtension` Protocol contract a third-party
package (``bytedesk_omnigent``) uses for its Infisical backend.

Dogfooding argument (Section 9.2): if the seam cannot host core's own
``LocalBackend`` default, it cannot host a third-party backend either. Shipping
the first-party default *through* the seam proves the seam works and proves core
holds no privileged secret backend.

Boot order (Section 9.3): ``omnigent.secrets`` depends on the kernel only — it
registers the local backend chain late in the boot sequence
(``→ secrets plugin registers [secret backend chain]``).

This plugin is **additive and not yet wired into boot** — the Integration phase
mounts it. Here it only needs to import cleanly and expose correct hook returns.

Circular-import safety: the concrete provider (:class:`LocalBackend`) lives in
:mod:`omnigent.onboarding.secrets`, which itself imports the extension seam
lazily. To stay symmetric and avoid any import-time coupling, this plugin imports
the provider **lazily inside the hook method**, never at module scope. The
provider class is *reused, not duplicated* — this plugin only registers it.
"""

from __future__ import annotations

from ..sdk import extension


@extension(name="omnigent.secrets")
class SecretsExtension:
    """First-party plugin contributing the default :class:`LocalBackend`.

    The ``secret_backends`` seam has no SDK member-decorator (there is no
    ``@secret_backend`` marker), so the hook is written by hand. The
    :func:`omnigent.sdk.extension` decorator fills *absent* optional Protocol
    members with behaviour-neutral no-ops, but it leaves a hand-written hook
    intact (``_set_if_absent`` only writes when the attribute is missing from the
    class body) — so this method is what the kernel's ``hasattr``-probe
    aggregator (:func:`omnigent.kernel.extensions.extension_secret_backends`) finds.
    """

    def secret_backends(self) -> list[object]:
        """Return the first-party secret backends for the chain.

        Reuses the subpackage's existing concrete default,
        :class:`omnigent.onboarding.secrets.LocalBackend` — the historical
        keyring-then-``0600``-file store that is always available. Imported
        lazily inside the hook so importing this plugin module stays
        kernel-light and circular-import-safe.

        Returns a list of :class:`~omnigent.onboarding.secrets.SecretBackend`
        instances, matching the shape
        :func:`omnigent.kernel.extensions.extension_secret_backends` aggregates (it
        ``.extend()``s each extension's ``secret_backends()`` return).
        """
        from .secrets import LocalBackend

        return [LocalBackend()]
