#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""haproxy-operator charm file."""

import logging
import typing
from enum import StrEnum

import ops
from charms.grafana_agent.v0.cos_agent import COSAgentProvider
from charms.tls_certificates_interface.v3.tls_certificates import (
    AllCertificatesInvalidatedEvent,
    CertificateAvailableEvent,
    CertificateExpiringEvent,
    CertificateInvalidatedEvent,
    TLSCertificatesRequiresV3,
)
from charms.traefik_k8s.v2.ingress import (
    IngressPerAppDataProvidedEvent,
    IngressPerAppDataRemovedEvent,
    IngressPerAppProvider,
)
from ops.charm import ActionEvent, RelationJoinedEvent

from haproxy import HAProxyService
from http_interface import (
    HTTPBackendAvailableEvent,
    HTTPBackendRemovedEvent,
    HTTPProvider,
    HTTPRequirer,
)
from state.config import CharmConfig
from state.ingress import IngressRequirersInformation
from state.tls import TLSInformation, TLSNotReadyError
from state.validation import validate_config_and_tls
from tls_relation import TLSRelationService, get_hostname_from_cert

logger = logging.getLogger(__name__)

INGRESS_RELATION = "ingress"
TLS_CERT_RELATION = "certificates"
REVERSE_PROXY_RELATION = "reverseproxy"
WEBSITE_RELATION = "website"


class ProxyMode(StrEnum):
    """StrEnum of possible http_route types.

    Attrs:
        INGRESS: when ingress is related.
        LEGACY: when reverseproxy is related.
        NOPROXY: when haproxy should return a default page.
        INVALID: when the charm state is invalid.
    """

    INGRESS = "ingress"
    LEGACY = "legacy"
    NOPROXY = "noproxy"
    INVALID = "invalid"


class HAProxyCharm(ops.CharmBase):
    """Charm haproxy."""

    def __init__(self, *args: typing.Any):
        """Initialize the charm and register event handlers.

        Args:
            args: Arguments to initialize the charm base.
        """
        super().__init__(*args)
        self.haproxy_service = HAProxyService()
        self.certificates = TLSCertificatesRequiresV3(self, TLS_CERT_RELATION)
        self._tls = TLSRelationService(self.model, self.certificates)
        self._ingress_provider = IngressPerAppProvider(charm=self, relation_name=INGRESS_RELATION)
        self.reverseproxy_requirer = HTTPRequirer(self, REVERSE_PROXY_RELATION)
        self.website_requirer = HTTPProvider(self, WEBSITE_RELATION)

        self._grafana_agent = COSAgentProvider(
            self,
            metrics_endpoints=[
                {"path": "/metrics", "port": 9123},
            ],
            dashboard_dirs=["./src/grafana_dashboards"],
        )

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.get_certificate_action, self._on_get_certificate_action)
        self.framework.observe(
            self.on.certificates_relation_joined, self._on_certificates_relation_joined
        )
        self.framework.observe(
            self.certificates.on.certificate_available, self._on_certificate_available
        )
        self.framework.observe(
            self.certificates.on.certificate_expiring, self._on_certificate_expiring
        )
        self.framework.observe(
            self.certificates.on.certificate_invalidated, self._on_certificate_invalidated
        )
        self.framework.observe(
            self.certificates.on.all_certificates_invalidated,
            self._on_all_certificate_invalidated,
        )
        self.framework.observe(
            self.reverseproxy_requirer.on.http_backend_available, self._on_http_backend_available
        )
        self.framework.observe(
            self.reverseproxy_requirer.on.http_backend_removed, self._on_http_backend_removed
        )
        self.framework.observe(
            self._ingress_provider.on.data_provided, self._on_ingress_data_provided
        )
        self.framework.observe(
            self._ingress_provider.on.data_removed, self._on_ingress_data_removed
        )

    def _on_install(self, _: typing.Any) -> None:
        """Install the haproxy package."""
        self.haproxy_service.install()
        self.unit.status = ops.MaintenanceStatus("Waiting for haproxy to be configured.")

    @validate_config_and_tls(defer=False, block_on_tls_not_ready=False)
    def _on_config_changed(self, _: typing.Any) -> None:
        """Handle the config-changed event."""
        self._reconcile()
        self._reconcile_certificates()

    @validate_config_and_tls(defer=True, block_on_tls_not_ready=True)
    def _on_certificates_relation_joined(self, _: RelationJoinedEvent) -> None:
        """Handle certificates relation joined event."""
        self._reconcile_certificates()

    @validate_config_and_tls(defer=True, block_on_tls_not_ready=True)
    def _on_certificate_available(self, event: CertificateAvailableEvent) -> None:
        """Handle the TLS Certificate available event.

        Args:
            event: Juju event
        """
        TLSInformation.validate(self)
        self._tls.certificate_available(event.certificate)
        self._reconcile()

    @validate_config_and_tls(defer=True, block_on_tls_not_ready=True)
    def _on_certificate_expiring(self, event: CertificateExpiringEvent) -> None:
        """Handle the TLS Certificate expiring event.

        Args:
            event: The event that fires this method.
        """
        TLSInformation.validate(self)
        self._tls.certificate_expiring(event.certificate)
        hostname = get_hostname_from_cert(event.certificate)
        self.unit.status = ops.MaintenanceStatus(
            f"Waiting for new certificate for hostname: {hostname}"
        )

    @validate_config_and_tls(defer=True, block_on_tls_not_ready=True)
    def _on_certificate_invalidated(self, event: CertificateInvalidatedEvent) -> None:
        """Handle the TLS Certificate invalidation event.

        Args:
            event: The event that fires this method.
        """
        TLSInformation.validate(self)
        if event.reason == "revoked":
            self._tls.certificate_invalidated(event.certificate)
        if event.reason == "expired":
            self._tls.certificate_expiring(event.certificate)
        self.unit.status = ops.MaintenanceStatus("Waiting for new certificate")
        hostname = get_hostname_from_cert(event.certificate)
        self.unit.status = ops.MaintenanceStatus(
            f"Waiting for new certificate for hostname: {hostname}"
        )

    @validate_config_and_tls(defer=False, block_on_tls_not_ready=False)
    def _on_all_certificate_invalidated(self, _: AllCertificatesInvalidatedEvent) -> None:
        """Handle the TLS Certificate invalidation event."""
        self._tls.all_certificate_invalidated()
        self._reconcile()

    def _on_get_certificate_action(self, event: ActionEvent) -> None:
        """Triggered when users run the `get-certificate` Juju action.

        Args:
            event: Juju event
        """
        TLSInformation.validate(self)

        hostname = event.params["hostname"]
        if provider_cert := self._tls.get_provider_cert_with_hostname(hostname):
            event.set_results(
                {
                    "certificate": provider_cert.certificate,
                    "ca": provider_cert.ca,
                    "chain": provider_cert.chain_as_pem(),
                }
            )
            return

        event.fail(f"Missing or incomplete certificate data for {hostname}")

    @validate_config_and_tls(defer=False, block_on_tls_not_ready=False)
    def _on_http_backend_available(self, _: HTTPBackendAvailableEvent) -> None:
        """Handle http_backend_available event for reverseproxy integration."""
        self._reconcile()

    @validate_config_and_tls(defer=False, block_on_tls_not_ready=False)
    def _on_http_backend_removed(self, _: HTTPBackendRemovedEvent) -> None:
        """Handle data_removed event for reverseproxy integration."""
        self._reconcile()

    def _reconcile(self) -> None:
        """Render the haproxy config and restart the service."""
        proxy_mode = self._validate_state()
        if proxy_mode == ProxyMode.INVALID:
            # We don't raise any exception/set status here as it should already be handled
            # by the _validate_state method
            return

        config = CharmConfig.from_charm(self)
        match proxy_mode:
            case ProxyMode.INGRESS:
                ingress_requirers_information = IngressRequirersInformation.from_provider(
                    self._ingress_provider
                )
                tls_information = TLSInformation.from_charm(self, self.certificates)
                self.haproxy_service.reconcile_ingress(
                    config, ingress_requirers_information, tls_information.external_hostname
                )
            case ProxyMode.LEGACY:
                self.haproxy_service.reconcile_legacy(
                    config, self.reverseproxy_requirer.get_services()
                )
            case _:
                self.haproxy_service.reconcile_default(config)
        self.unit.status = ops.ActiveStatus()

    def _reconcile_certificates(self) -> None:
        """Request new certificates if needed to match the configured hostname."""
        tls_information = TLSInformation.from_charm(self, self.certificates)
        current_certificate = None
        for certificate in self.certificates.get_provider_certificates():
            if (
                get_hostname_from_cert(certificate.certificate)
                != tls_information.external_hostname
            ):
                self.certificates.request_certificate_revocation(
                    certificate_signing_request=certificate.csr.encode()
                )
            else:
                current_certificate = certificate
        if not current_certificate:
            logger.info("Certificate not in provider's relation data, creating csr.")
            self._tls.generate_private_key(tls_information.external_hostname)
            self._tls.request_certificate(tls_information.external_hostname)

    @validate_config_and_tls(defer=True, block_on_tls_not_ready=True)
    def _on_ingress_data_provided(self, event: IngressPerAppDataProvidedEvent) -> None:
        """Handle the data-provided event.

        Args:
            event: Juju event.
        """
        self._reconcile()
        tls_information = TLSInformation.from_charm(self, self.certificates)
        integration_data = self._ingress_provider.get_data(event.relation)
        path_prefix = f"{integration_data.app.model}-{integration_data.app.name}"

        self._ingress_provider.publish_url(
            event.relation, f"https://{tls_information.external_hostname}/{path_prefix}/"
        )

    @validate_config_and_tls(defer=False, block_on_tls_not_ready=True)
    def _on_ingress_data_removed(self, _: IngressPerAppDataRemovedEvent) -> None:
        """Handle the data-removed event."""
        self._reconcile()

    def _validate_state(self) -> ProxyMode:
        """Validate if all the necessary preconditions are fulfilled.

        Returns:
            tuple[bool, ProxyMode]: Whether the preconditions are fulfilled
            and the resulting proxy mode.
        """
        is_ingress_related = bool(self._ingress_provider.relations)
        is_legacy_related = bool(self.reverseproxy_requirer.relations)

        if is_ingress_related and is_legacy_related:
            logger.error("Both ingress and reverseproxy is related.")
            self.unit.status = ops.BlockedStatus("Both ingress and reverseproxy is related.")
            return ProxyMode.INVALID

        if is_ingress_related:
            try:
                TLSInformation.validate(self)
            except TLSNotReadyError as exc:
                logger.exception("Invalid hostname configuration and/or relation data.")
                self.unit.status = ops.BlockedStatus(str(exc))
                return ProxyMode.INVALID

            return ProxyMode.INGRESS

        if is_legacy_related:
            return ProxyMode.LEGACY

        return ProxyMode.NOPROXY


if __name__ == "__main__":  # pragma: nocover
    ops.main(HAProxyCharm)
