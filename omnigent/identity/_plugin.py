"""First-party plugin for the ``omnigent.identity`` subpackage (BDP-2509).

CORE = KERNEL + first-party plugins. Per Section 9.1 of
``docs/EXTENSION_FRAMEWORK_ANALYSIS.md`` the ``omnigent/identity/`` subpackage
becomes the ``omnigent.identity`` first-party plugin, registering this
subpackage's already-existing default providers into the three identity kernel
seams it owns (all three are already :class:`~omnigent.kernel.pluggable.PluggableRegistry`
seams in :data:`omnigent.kernel.pluggable.manifest.SEAMS`):

  * ``assertion_verifier``  ← :class:`~omnigent.identity.verifiers.HmacAssertionVerifier`
  * ``outbound_credential`` ← :class:`~omnigent.identity.defaults.StaticSecretProvider`
  * ``authorizer``          ← :class:`~omnigent.identity.defaults.OwnerAllowAuthorizer`

This is the **dogfooding** argument (Section 9.2): the in-box defaults reach the
seams through the *same* ``OmnigentExtension`` hook contract a third party would
use — there is no privileged core wiring. The providers themselves are not moved
or rewritten here; this plugin only *registers* the existing concrete classes
through the seam hooks, mirroring the ``(name, factory)`` defaults already
declared in :mod:`omnigent.identity.registry`.

The hook methods return ``{name: factory}`` where ``factory`` is the zero-arg
callable the kernel's :meth:`PluggableRegistry.discover_extensions` passes to
``register(name, factory)`` (the registry calls ``factory()`` lazily on each
``get``). Heavy / sibling imports are deferred **inside** each hook to stay
circular-import-safe and kernel-light — the same deferred-import discipline the
hand-written :class:`bytedesk_omnigent.extension.BytedeskExtension` uses for its
identity-port hooks.

**Not yet wired into boot.** The Integration phase mounts first-party plugins;
this module only needs to import cleanly and expose correct hook returns. It is
authored on the :func:`omnigent.sdk.extension` facade so its instances satisfy
the kernel :class:`omnigent.kernel.extensions.OmnigentExtension` Protocol unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

from omnigent.sdk import extension


@extension(name="omnigent.identity")
class IdentityExtension:
    """First-party plugin: register the in-box identity defaults into their seams.

    The three identity-port hooks below are *not* SDK member-decorator seams
    (the SDK has no ``@assertion_verifier``-style decorator), so they are written
    by hand. The ``@extension`` decorator only fills an optional Protocol hook
    with an empty default when the class does *not* already define it
    (``_set_if_absent``), so these hand-written hooks are preserved verbatim
    while every other optional hook (tools, policies, routers, …) stays an
    empty no-op — this plugin contributes to the identity seams only.
    """

    def assertion_verifiers(self) -> dict[str, Callable[[], object]]:
        """Register the default inbound-trust verifier (``hmac``).

        Mirrors :func:`omnigent.identity.registry.build_assertion_verifier_registry`'s
        ``("hmac", HmacAssertionVerifier.from_env)`` default. ``from_env`` reads
        the signing secret from the environment (``None`` ⇒ unconfigured, which
        fail-closes on verify), so it is a valid zero-arg factory.
        """
        from omnigent.identity.verifiers import HmacAssertionVerifier

        return {"hmac": HmacAssertionVerifier.from_env}

    def outbound_credential_providers(self) -> dict[str, Callable[[], object]]:
        """Register the default act-as egress provider (``static_secret``).

        Mirrors :func:`omnigent.identity.registry.build_outbound_credential_registry`'s
        ``("static_secret", StaticSecretProvider)`` default.
        """
        from omnigent.identity.defaults import StaticSecretProvider

        return {"static_secret": StaticSecretProvider}

    def authorization_providers(self) -> dict[str, Callable[[], object]]:
        """Register the default authorization provider (``owner_allow``).

        Mirrors :func:`omnigent.identity.registry.build_authorizer_registry`'s
        ``("owner_allow", OwnerAllowAuthorizer)`` default (standalone single-user
        owner model — allow).
        """
        from omnigent.identity.defaults import OwnerAllowAuthorizer

        return {"owner_allow": OwnerAllowAuthorizer}


__all__ = ["IdentityExtension"]
