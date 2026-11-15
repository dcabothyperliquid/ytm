"""fine_banner_scan appends (t, kind) tuples whose t can equal a coarse-scan t while the kind differs (None vs 'four'):
sorting the tuples directly raised TypeError and killed the whole highlight run. Banners must be ordered by time only."""
import hl_scoreboard as sb


def test_banner_onsets_with_equal_timestamps_and_none_kinds():
    banners = [(16.0, None), (20.0, None), (16.0, "four"), (17.0, "four"), (20.0, "six")]
    on = sb.banner_onsets(sorted(banners, key=lambda b: b[0]))
    kinds = [k for _, k in on]
    assert "four" in kinds and "six" in kinds
