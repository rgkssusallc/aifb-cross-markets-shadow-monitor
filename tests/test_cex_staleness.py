"""What `age_ms` must mean on each Coinbase transport.

This is the guard that decides whether a route is costed at all, and it was
measuring the wrong thing: time since this product's book last CHANGED. On
the level2 stream that punishes an asset for being quiet, because the channel
is a complete sequence-checked incremental feed and silence about a product
means that book did not move. Nine of ten screened candidates logged nothing
on a connection that was never unhealthy.

So the rule differs by transport, and these tests pin both:
  STREAM -- staleness is time since the last message on the CONNECTION, and
            an unhealthy feed is infinitely stale whatever the book says.
  REST   -- staleness is the age of the snapshot, because between polls the
            book really does move unobserved.

Run: python tests/test_cex_staleness.py     (no pytest required)
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from legs import Book, Level            # noqa: E402
from venues.cex import CoinbaseVenue    # noqa: E402


@dataclass
class FakeFeed:
    """Only the three attributes the venue is allowed to depend on."""
    last_msg_at: float = 0.0
    healthy: bool = True
    messages: int = 10
    confirmed: tuple = ("ETH-USD",)
    aliased: dict = field(default_factory=dict)


def _book(age_s: float) -> Book:
    return Book(product_id="ETH-USD", base="ETH", quote="USD",
                bids=(Level(Decimal("100"), Decimal("1")),),
                asks=(Level(Decimal("101"), Decimal("1")),),
                ts_local=time.time() - age_s)


def _venue(feed, book) -> CoinbaseVenue:
    v = CoinbaseVenue(product="ETH-USDC", use_ws=feed is not None)
    v.feed = feed
    v._book = book
    v._base, v._quote = "ETH", "USDC"
    return v


def test_stream_quiet_product_is_not_stale():
    """A 5s-old book on a live connection is CURRENT, not stale.

    This is the whole point: the feed said nothing about this product because
    nothing happened to it.
    """
    v = _venue(FakeFeed(last_msg_at=time.time() - 0.03), _book(5.0))
    assert v.age_ms() < 400, (
        f"a quiet book on a live feed must not read as stale, got "
        f"{v.age_ms():.0f}ms -- this is the bug that silenced nine of ten "
        "candidates")


def test_stream_dead_connection_is_infinitely_stale():
    """Losing contact is the real danger, and it must dominate a fresh book."""
    v = _venue(FakeFeed(last_msg_at=time.time() - 30.0), _book(0.01))
    assert v.age_ms() > 400, "30s without contact must be refused"


def test_unhealthy_feed_is_refused_however_new_the_book():
    """A sequence gap means the maintained book may be silently wrong."""
    v = _venue(FakeFeed(last_msg_at=time.time(), healthy=False), _book(0.0))
    assert v.age_ms() == float("inf"), (
        "an unhealthy feed must be infinitely stale: a book that may have "
        "dropped updates is worse than no book")


def test_feed_with_no_message_yet_is_refused():
    """Before the first message there is no evidence the connection works."""
    v = _venue(FakeFeed(last_msg_at=0.0), _book(0.0))
    assert v.age_ms() == float("inf")


def test_rest_still_ages_on_the_snapshot():
    """With no feed, the book's own age IS the exposure. Unchanged behaviour."""
    v = _venue(None, _book(2.0))
    assert 1900 < v.age_ms() < 2200, (
        f"REST must age on the snapshot, got {v.age_ms():.0f}ms")


def test_no_book_is_infinitely_stale():
    v = _venue(FakeFeed(last_msg_at=time.time()), None)
    assert v.age_ms() == float("inf")


def test_stream_does_not_read_the_book_timestamp_at_all():
    """Two very different book ages, one live feed, same verdict.

    Pins the mechanism rather than a threshold: if someone reintroduces
    ts_local into the stream path, these two stop agreeing.
    """
    now = time.time()
    fresh = _venue(FakeFeed(last_msg_at=now - 0.05), _book(0.01))
    old = _venue(FakeFeed(last_msg_at=now - 0.05), _book(60.0))
    assert abs(fresh.age_ms() - old.age_ms()) < 50, (
        "on the stream the book's own timestamp must not affect staleness")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"pass {t.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
