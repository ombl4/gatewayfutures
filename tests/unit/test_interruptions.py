"""Spec T3.4: barge-in is planned per agent turn with seeded, reproducible draws."""

from gf.caller.simulator import InterruptPlanner


def test_zero_probability_never_interrupts():
    p = InterruptPlanner(0.0, seed=1)
    assert all(p.decide() is None for _ in range(50))


def test_full_probability_always_interrupts_within_bounds():
    p = InterruptPlanner(1.0, seed=1)
    delays = [p.decide() for _ in range(50)]
    assert all(d is not None for d in delays)
    assert all(InterruptPlanner.MIN_DELAY_S <= d <= InterruptPlanner.MAX_DELAY_S for d in delays)


def test_same_seed_same_plan_different_seed_differs():
    pa, pb, pc = (
        InterruptPlanner(0.5, seed=7),
        InterruptPlanner(0.5, seed=7),
        InterruptPlanner(0.5, seed=8),
    )
    a = [pa.decide() for _ in range(30)]
    b = [pb.decide() for _ in range(30)]
    c = [pc.decide() for _ in range(30)]
    assert a == b
    assert a != c
    made = sum(1 for d in a if d is not None)
    assert 5 <= made <= 25  # roughly half at p = 0.5


def test_probability_is_clamped():
    assert InterruptPlanner(3.0, seed=1).p == 1.0
    assert InterruptPlanner(-1.0, seed=1).p == 0.0
