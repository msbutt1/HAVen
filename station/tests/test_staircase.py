"""Staircase rule tests on synthetic response sequences (pure logic, no engine).

These verify the stepping, reversal and stopping rules only. They say nothing
about real perception.
"""

import pytest

from vibeguard_station.config import StationConfig
from vibeguard_station.staircase import Staircase

# start 50, step 10: felt/not-felt sequence below visits 50,40,30,40,30,40,30,40,30,40
CFG = StationConfig.full(start_setting=50, step=10, catch_probability=0.0)
EIGHT_REVERSALS = [True, True, False, True, False, True, False, True, False, True]


def run(cfg, felt_seq):
    s = Staircase(cfg)
    settings = []
    for felt in felt_seq:
        settings.append(s.setting)
        s.record(felt)
    return s, settings


# -- stepping -------------------------------------------------------------------

def test_felt_steps_down_and_not_felt_steps_up():
    s = Staircase(CFG)
    assert s.record(True).next_setting == 40
    assert s.record(False).next_setting == 50


def test_first_response_sets_direction_but_is_not_a_reversal():
    s = Staircase(CFG)
    out = s.record(False)
    assert out.reversal is False and s.reversals == 0 and s.direction == "up"


def test_reversal_level_is_the_trial_that_flipped_the_direction():
    s, settings = run(CFG, [True, True, False, True])
    # 50 felt, 40 felt, 30 NOT felt (valley -> reversal at 30), 40 felt (peak -> reversal at 40)
    assert settings == [50, 40, 30, 40]
    assert s.reversal_levels == [30, 40]


def test_same_direction_repeated_makes_no_reversal():
    s, _ = run(CFG, [True, True, True, True])
    assert s.reversals == 0 and s.setting == 10


# -- stopping and threshold -------------------------------------------------------

def test_eight_reversals_stop_and_threshold_is_mean_of_last_six():
    s, settings = run(CFG, EIGHT_REVERSALS)
    assert settings == [50, 40, 30, 40, 30, 40, 30, 40, 30, 40]
    assert s.reversal_levels == [30, 40, 30, 40, 30, 40, 30, 40]
    assert s.stop_reason() == "threshold_estimated"
    assert s.levels_used() == [30, 40, 30, 40, 30, 40]
    assert s.threshold() == 35.0


def test_threshold_uses_only_the_last_six_not_all_eight():
    cfg = StationConfig.full(start_setting=50, step=10, catch_probability=0.0)
    s = Staircase(cfg)
    s.reversal_levels = [100, 100, 30, 40, 30, 40, 30, 40]  # first two are outliers
    assert s.threshold() == 35.0


def test_no_stop_and_no_threshold_before_eight_reversals():
    s, _ = run(CFG, EIGHT_REVERSALS[:-1])
    assert s.reversals == 7
    assert s.stop_reason() is None and s.threshold() is None


def test_preview_stops_after_four_reversals_and_averages_the_last_two():
    cfg = StationConfig.preview(start_setting=50, step=10)
    s, _ = run(cfg, EIGHT_REVERSALS[:6])
    assert s.reversals == 4 and s.stop_reason() == "threshold_estimated"
    assert s.threshold() == 35.0 and s.levels_used() == [30, 40]


# -- range limits -----------------------------------------------------------------

def test_level_is_clamped_to_the_setting_range():
    cfg = StationConfig.full(start_setting=5, step=10, min_setting=1, max_setting=100, catch_probability=0.0)
    s = Staircase(cfg)
    assert s.record(True).next_setting == 1
    cfg2 = StationConfig.full(start_setting=95, step=10, catch_probability=0.0)
    assert Staircase(cfg2).record(False).next_setting == 100


def test_felt_at_lowest_setting_repeatedly_ends_with_floor_reached():
    cfg = StationConfig.full(start_setting=1, step=5, catch_probability=0.0)
    s = Staircase(cfg)
    for _ in range(2):
        s.record(True)
        assert s.stop_reason() is None
    s.record(True)
    assert s.stop_reason() == "floor_reached" and s.threshold() is None


def test_not_felt_at_highest_setting_repeatedly_ends_with_ceiling_reached():
    cfg = StationConfig.full(start_setting=100, step=5, catch_probability=0.0)
    s = Staircase(cfg)
    for _ in range(3):
        s.record(False)
    assert s.stop_reason() == "ceiling_reached"


def test_boundary_counter_resets_once_the_level_can_move_again():
    cfg = StationConfig.full(start_setting=1, step=5, catch_probability=0.0)
    s = Staircase(cfg)
    s.record(True)
    s.record(True)            # two floor hits
    s.record(False)           # moves up to 6: counter resets
    assert s.boundary_repeats == 0
    s.record(True)            # back to 1, not stuck yet
    assert s.stop_reason() is None


@pytest.mark.parametrize("bad", [
    dict(min_setting=0), dict(start_setting=200), dict(step=0), dict(reversals_averaged=9),
    dict(max_trials=5), dict(catch_probability=1.0), dict(response_window_s=0), dict(mode="x"),
])
def test_invalid_config_is_rejected(bad):
    with pytest.raises(ValueError):
        Staircase(StationConfig.full(**bad))
