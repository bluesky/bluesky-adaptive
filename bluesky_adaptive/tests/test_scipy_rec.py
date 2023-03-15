import bluesky.plan_stubs as bps
import pytest

from bluesky_adaptive.on_stop import recommender_factory
from bluesky_adaptive.per_start import adaptive_plan


def test_scipy_minimize_recommender(RE, hw):
    pytest.importorskip("scipy")
    from bluesky_adaptive.scipy_reccomendations import MinimizerReccomender

    results_list = []

    def do_the_thing(det, det_key):
        recommender = MinimizerReccomender(scale=-1)
        cb, queue = recommender_factory(
            adaptive_obj=recommender,
            independent_keys=["np.mean(motor)"],
            dependent_keys=[det_key],
            target_keys=["motor"],
            max_count=100,
        )
        yield from adaptive_plan([det], {hw.motor: 1}, to_recommender=cb, from_recommender=queue)
        yield from bps.mv(hw.motor, recommender.result.x)
        print(recommender.result)
        results_list.append(recommender.result)

    RE(do_the_thing(hw.det, "np.asarray(det)"))
    RE(do_the_thing(hw.img, "np.median(img)"))
    assert len(results_list) == 2
    assert all(_ is not None for _ in results_list)
