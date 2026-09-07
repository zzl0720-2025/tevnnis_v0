"""The news-url sanitizer.

A news url goes into a world-readable file, so it must not be able to carry an
auth or session token out with it. The sanitizer closes that by REBUILDING the
url from scheme + host + path rather than by stripping known-bad parts, so a
component nobody anticipated cannot survive by not being on a blacklist.

The host allow-list is a separate matter and defaults to EMPTY (any https host)
— see `test_an_external_publisher_url_survives_by_default`, which is the
regression guard for that decision.
"""

from __future__ import annotations

import pytest

from tevnnis_core.snapshot import sanitize_news_url


class TestTokensAndTracking:
    def test_a_token_query_param_is_stripped(self):
        assert (
            sanitize_news_url("https://longbridge.com/news/1?token=SUPERSECRET99")
            == "https://longbridge.com/news/1"
        )

    def test_every_query_param_is_stripped_by_default(self):
        assert (
            sanitize_news_url(
                "https://www.reuters.com/x?utm_source=a&utm_campaign=b&sid=c&token=d"
            )
            == "https://www.reuters.com/x"
        )

    def test_a_fragment_is_dropped(self):
        assert (
            sanitize_news_url("https://longbridge.com/news/1#access_token=abc")
            == "https://longbridge.com/news/1"
        )

    def test_a_named_param_can_be_kept_without_keeping_the_rest(self):
        kept = sanitize_news_url(
            "https://longbridge.com/news?id=123&token=SUPERSECRET99",
            keep_params=["id"],
        )
        assert kept == "https://longbridge.com/news?id=123"
        assert "token" not in kept
        assert "SUPERSECRET99" not in kept


class TestTransportAndAuthority:
    def test_http_is_dropped_not_upgraded(self):
        # Upgrading would publish a link we never verified resolves over TLS.
        assert sanitize_news_url("http://longbridge.com/news/1") is None

    def test_userinfo_is_refused(self):
        assert sanitize_news_url("https://user:pass@longbridge.com/news/1") is None
        assert sanitize_news_url("https://token@longbridge.com/news/1") is None

    def test_a_non_default_port_is_refused(self):
        assert sanitize_news_url("https://longbridge.com:8443/news/1") is None

    def test_an_explicit_443_is_accepted_and_normalized(self):
        assert (
            sanitize_news_url("https://longbridge.com:443/news/1")
            == "https://longbridge.com/news/1"
        )

    def test_the_host_is_lowercased(self):
        assert (
            sanitize_news_url("https://NEWS.Reuters.COM/Business/X")
            == "https://news.reuters.com/Business/X"
        )

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "   ",
            "not a url",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "ftp://files.example.com/x",
            "//longbridge.com/news/1",
            "https://",
            "https:///news/1",
        ],
    )
    def test_unusable_input_yields_none(self, url):
        assert sanitize_news_url(url) is None

    def test_an_absurdly_long_url_is_refused(self):
        assert sanitize_news_url("https://longbridge.com/" + "a" * 600) is None


class TestHostAllowList:
    """Empty by default. See the module docstring and PublicSnapshotConfig."""

    def test_an_external_publisher_url_survives_by_default(self):
        """THE regression guard for the default.

        A Longbridge-only host list would silently null every external article
        link — the clickthrough requirement would break with no error anywhere,
        while adding no security the other rules do not already provide.
        """
        for url in (
            "https://www.reuters.com/business/nvidia-earnings",
            "https://www.bloomberg.com/news/articles/2026-09-06/x",
            "https://apnews.com/article/markets-abc123",
            "https://some-publisher-we-never-heard-of.co.uk/story/1",
        ):
            assert sanitize_news_url(url) is not None, url

    def test_a_configured_list_admits_the_named_host(self):
        assert (
            sanitize_news_url(
                "https://longbridge.com/news/1", allowed_hosts=["longbridge.com"]
            )
            == "https://longbridge.com/news/1"
        )

    def test_a_configured_list_admits_subdomains(self):
        assert (
            sanitize_news_url(
                "https://news.longbridge.com/1", allowed_hosts=["longbridge.com"]
            )
            == "https://news.longbridge.com/1"
        )

    def test_a_configured_list_refuses_anything_else(self):
        assert (
            sanitize_news_url("https://evil.example.com/x", allowed_hosts=["longbridge.com"])
            is None
        )

    def test_a_suffix_that_is_not_a_subdomain_is_refused(self):
        # "notlongbridge.com" ends with "longbridge.com" as a STRING but is a
        # different registrable domain — the classic allow-list bypass.
        assert (
            sanitize_news_url(
                "https://notlongbridge.com/x", allowed_hosts=["longbridge.com"]
            )
            is None
        )


def test_the_rebuild_drops_anything_it_was_not_told_to_keep():
    """One url carrying every trick at once."""
    hostile = (
        "https://user:pw@www.reuters.com:8443/business/x"
        "?token=abc&id=1#access_token=def"
    )
    assert sanitize_news_url(hostile) is None  # userinfo + port stop it outright

    milder = "https://www.reuters.com/business/x?token=abc&id=1#access_token=def"
    cleaned = sanitize_news_url(milder)
    assert cleaned == "https://www.reuters.com/business/x"
    for leak in ("token", "abc", "access_token", "def", "#", "?"):
        assert leak not in cleaned
