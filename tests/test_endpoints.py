"""Tests for the endpoint parsing helpers in ``utils/endpoints``."""

import pytest

from mercury_ocip_fast.utils.endpoints import override_url_port, split_host_port


class TestSplitHostPort:
    def test_bare_host_uses_default(self):
        assert split_host_port("broadworks.example.com", None, 2209) == (
            "broadworks.example.com",
            2209,
        )

    def test_host_port_pair(self):
        assert split_host_port("host:2208", None, 2209) == ("host", 2208)

    def test_explicit_port_wins(self):
        # The explicit port has priority over the port in the endpoint.
        assert split_host_port("host:2208", 5000, 2209) == ("host", 5000)

    def test_scheme_url(self):
        assert split_host_port("tcp://host:2210", None, 2209) == ("host", 2210)

    def test_ipv6_in_brackets(self):
        assert split_host_port("[::1]:2209", None, 2209) == ("::1", 2209)

    def test_no_host_raises(self):
        with pytest.raises(ValueError):
            split_host_port("", None, 2209)


class TestOverrideUrlPort:
    def test_none_keeps_url(self):
        url = "https://host/webservice/services/ProvisioningService"
        assert override_url_port(url, None) == url

    def test_sets_port(self):
        result = override_url_port("https://host/webservice", 8443)
        assert result == "https://host:8443/webservice"

    def test_replaces_existing_port(self):
        result = override_url_port("https://host:443/webservice", 8443)
        assert result == "https://host:8443/webservice"

    def test_no_host_raises(self):
        with pytest.raises(ValueError):
            override_url_port("not-a-url", 8443)
