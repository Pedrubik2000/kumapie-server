"""known.lists(): the newest import wins; its words go back only to a device with an older one.
Run: .venv\\Scripts\\python.exe services\\feed\\test_known_lists.py"""
import tempfile
from pathlib import Path

import known

known.PEOPLE = {"pedro": {"ja": "pedro-ja"}}
with tempfile.TemporaryDirectory() as d:
    go = lambda imp: known.lists({"person": "pedro", "lang": "ja", "imported": imp}, Path(d))
    assert go({"time": 0}) == {}
    assert go({"time": 10, "words": ["猫", "犬"]}) == {"imported": {"time": 10}}  # the tablet imports
    assert go({"time": 0}) == {"imported": {"time": 10, "words": ["犬", "猫"]}}  # the empty Boox gets them
    assert go({"time": 10}) == {"imported": {"time": 10}}  # nothing new: no words sent back
    go({"time": 20, "words": []})  # the Boox removes the import later: the tablet gets the empty list
    assert go({"time": 10}) == {"imported": {"time": 20, "words": []}}
    go({"time": 15, "words": ["鳥"]})  # an older upload doesn't win
    assert go({"time": 0})["imported"]["words"] == []
    for bad in ({"words": []}, {"time": True}, {"time": 1, "words": ["a\nb"]}, None):
        try:
            go(bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
print("ok")
