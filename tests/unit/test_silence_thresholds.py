"""The mutual-silence re-prompt adapts to the agent's measured reply latency."""

from gf.runner.call import silence_thresholds
from gf.sessions.schema import Limits


def test_floors_hold_until_two_replies_are_heard():
    lim = Limits(mutual_silence_reprompt_s=4, mutual_silence_abort_s=8)
    assert silence_thresholds(lim, []) == (4, 8)
    assert silence_thresholds(lim, [4600]) == (4, 8)


def test_slow_agent_raises_both_thresholds_fast_agent_keeps_the_floors():
    lim = Limits(mutual_silence_reprompt_s=4, mutual_silence_abort_s=8)
    assert silence_thresholds(lim, [4600, 4700, 4400]) == (6.9, 11.5)  # 1.5x and 2.5x the median
    assert silence_thresholds(lim, [1800, 2100, 1900]) == (4, 8)
