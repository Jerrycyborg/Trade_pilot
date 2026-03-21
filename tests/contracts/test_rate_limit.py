"""Tests for token bucket rate limiter."""

from contracts.rate_limit import TokenBucket


def test_token_bucket_allows_requests():
    bucket = TokenBucket(max_tokens=5, refill_rate_per_minute=60)
    for _ in range(5):
        assert bucket.consume("127.0.0.1") is True


def test_token_bucket_rejects_when_exhausted():
    bucket = TokenBucket(max_tokens=3, refill_rate_per_minute=60)
    for _ in range(3):
        bucket.consume("127.0.0.1")
    assert bucket.consume("127.0.0.1") is False


def test_token_bucket_different_ips_isolated():
    bucket = TokenBucket(max_tokens=1, refill_rate_per_minute=60)
    assert bucket.consume("1.2.3.4") is True
    assert bucket.consume("5.6.7.8") is True
    assert bucket.consume("1.2.3.4") is False
