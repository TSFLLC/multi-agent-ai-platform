"""Notification / Realtime Gateway — Section 11. Fan-out of live status to
connected local UI clients via SSE. Serves only ``127.0.0.1`` by default
(app.config.settings.host) — never a public endpoint. Real
implementation: MA3."""

from app.services.base import BaseService


class NotificationGateway(BaseService):
    def publish(self, *args, **kwargs):
        raise NotImplementedError("NotificationGateway lands in MA3")
