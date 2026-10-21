import re

from wb2api.wabi import thread_title


def test_titles_are_distinct_private_and_not_branded():
    titles = [thread_title(kind) for kind in ('chat', 'image', 'video') for _ in range(10)]
    assert len(set(titles)) == len(titles)
    assert all(re.fullmatch(r'(Chat|Image|Video) \d{8}-\d{6} [0-9a-f]{8}', title) for title in titles)
    assert all('2api' not in title.lower() and 'group' not in title.lower() for title in titles)
