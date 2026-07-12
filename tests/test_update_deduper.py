"""UpdateDeduper tests: the small seen-set that collapses N identical group
updates (one per helper bot's polling stream) down to a single processing.

the properties that matter:
- the first sighting of a (chat_id, message_id) is NOT a duplicate; every
  repeat is
- the key is the (chat_id, message_id) pair - same message_id in a different
  chat is a distinct message
- bounded memory: past maxlen, the oldest keys are evicted (FIFO)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.providers.platforms.telegram_bot import UpdateDeduper  # noqa: E402


def test_first_sighting_is_not_a_duplicate_repeats_are():
    d = UpdateDeduper()
    assert d.is_duplicate(-100, 5) is False   # first bot delivers it
    assert d.is_duplicate(-100, 5) is True    # second bot's identical update
    assert d.is_duplicate(-100, 5) is True    # ...and every one after


def test_key_is_chat_and_message_id_pair():
    d = UpdateDeduper()
    assert d.is_duplicate(-100, 5) is False
    assert d.is_duplicate(-200, 5) is False   # same message_id, different chat
    assert d.is_duplicate(-100, 6) is False   # same chat, different message_id
    assert d.is_duplicate(-200, 5) is True     # now a repeat


def test_evicts_oldest_past_maxlen():
    d = UpdateDeduper(maxlen=3)
    for mid in (1, 2, 3):
        assert d.is_duplicate(0, mid) is False
    # inserting a 4th (a distinct message) evicts (0, 1), the oldest
    assert d.is_duplicate(0, 4) is False
    # the three most recent are still remembered...
    assert d.is_duplicate(0, 2) is True
    assert d.is_duplicate(0, 3) is True
    assert d.is_duplicate(0, 4) is True
    # ...but the evicted oldest is seen as new again
    assert d.is_duplicate(0, 1) is False


def test_resighting_does_not_refresh_recency():
    # a duplicate hit must not move the key to the newest slot (it stays subject
    # to eviction on its original insertion order).
    d = UpdateDeduper(maxlen=2)
    assert d.is_duplicate(0, 1) is False
    assert d.is_duplicate(0, 2) is False
    assert d.is_duplicate(0, 1) is True    # duplicate, ordering unchanged
    assert d.is_duplicate(0, 3) is False   # evicts the oldest, (0, 1)
    assert d.is_duplicate(0, 1) is False   # so (0, 1) is new again
