# Plans for testing.
# These will get populated into the run manager persmissions when the environemnts is opened.
# Named 16-plans because qserver ships with a 00-ophyd, 15-plans, 99-custom.

import bluesky.preprocessors as bpp
from bluesky import plan_stubs as bps


@bpp.run_decorator()
def agent_driven_nap(delay: float, *, delay_kwarg: float = 0, md=None):
    """Ensuring we can auto add 'agent_' plans and use args/kwargs"""
    if delay_kwarg:
        yield from bps.sleep(delay_kwarg)
    else:
        yield from bps.sleep(delay)
