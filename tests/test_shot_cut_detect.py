import pytest
import av.shot_cut_detect as mod

CASES = [
    ("dual_threshold_cut", (1,)),
    ("display_order_detect", (1,)),
    ("fade_separate", (1,)),
    ("cross_file_timecode", (1,)),
    ("frame_after_cut", (1,)),
    ("task_isolation", (1,)),
    ("load_clip", (1,)),
    ("scan_diff", (1,)),
    ("segment", (1,)),
    ("present", (1,)),
    ("validate_cut", (1,)),
    ("metrics", (1,)),
    ("audit", (1,)),
    ("fallback", (1,)),
    ("determinism", (1,)),
]

@pytest.mark.parametrize("fn_name,args", CASES)
def test_contract(fn_name, args):
    fn = getattr(mod, fn_name)
    assert fn(*args) is not None
