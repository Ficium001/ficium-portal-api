# =============================================================================
# ficium-portal-api — SSRF guard tests
#
# Locks in that webhook URL validation blocks the classic SSRF targets
# (cloud metadata, loopback, private ranges, link-local) while allowing
# legitimate public HTTPS endpoints.
# =============================================================================

import pytest

from app.core.ssrf import validate_webhook_url, WebhookUrlError


class TestBlockedTargets:
    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",   # AWS/GCP metadata
        "https://169.254.169.254/",                    # metadata over https
        "http://localhost:6379",                        # internal Redis
        "https://localhost/hook",
        "http://127.0.0.1/hook",                        # loopback
        "http://127.0.0.1:8000/internal",
        "http://10.0.0.5/hook",                         # private 10/8
        "https://192.168.1.10/hook",                    # private 192.168/16
        "http://172.16.5.5/hook",                       # private 172.16/12
        "http://[::1]/hook",                            # IPv6 loopback
        "https://metadata.google.internal/",            # GCP metadata hostname
        "http://0.0.0.0/hook",                          # unspecified
    ])
    def test_blocked(self, url):
        with pytest.raises(WebhookUrlError):
            validate_webhook_url(url)


class TestSchemeEnforcement:
    def test_http_rejected_when_https_required(self):
        with pytest.raises(WebhookUrlError):
            validate_webhook_url("http://example.com/hook", require_https=True)

    def test_non_http_scheme_rejected(self):
        for url in ("ftp://example.com", "file:///etc/passwd", "gopher://x"):
            with pytest.raises(WebhookUrlError):
                validate_webhook_url(url, require_https=False)

    def test_no_host_rejected(self):
        with pytest.raises(WebhookUrlError):
            validate_webhook_url("https:///nohost")


class TestAllowedTargets:
    @pytest.mark.parametrize("url", [
        "https://example.com/webhooks/ficium",
        "https://example.org/callbacks",
        "https://example.net:8443/ingest",
    ])
    def test_public_https_allowed(self, url):
        # example.com/org/net are IANA-reserved and always resolve to public IPs.
        validate_webhook_url(url)

    def test_public_http_allowed_when_https_not_required(self):
        validate_webhook_url("http://example.com/hook", require_https=False)
