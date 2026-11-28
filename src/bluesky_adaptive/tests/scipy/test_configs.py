import pytest

from bluesky_adaptive import ax
from bluesky_adaptive.scipy.configs import SCP, Objective, RangeDOF, ScipyCFG

from ..conftest import MovableSignal

OBJECTIVE = Objective(name="test_objective", minimize=True)


@pytest.mark.parametrize(
    ("options", "rejected"),
    [
        ({"parameter_type": "int"}, "parameter_type='int'"),
        ({"parameter_type": "float", "step_size": 0.5}, "step_size=0.5"),
        ({"parameter_type": "float", "scaling": "log"}, "scaling='log'"),
    ],
)
def test_range_dof_rejects_unsupported_options(options, rejected):
    """DOF options the SciPy backend would ignore are rejected when the DOF is created."""
    with pytest.raises(ValueError, match=rf"SciPy backend does not support {rejected}"):
        RangeDOF(actuator=MovableSignal(name="x"), bounds=(0, 10), **options)


def test_range_dof_accepts_linear_float():
    dof = RangeDOF(actuator=MovableSignal(name="x"), bounds=(0, 10), parameter_type="float", scaling="linear")
    assert dof.parameter_name == "x"


def test_config_rejects_unsupported_dof_from_other_backend():
    """DOFs from other backends are checked when the SciPy configuration is created."""
    dof = ax.RangeDOF(actuator=MovableSignal(name="x"), bounds=(0, 10), parameter_type="int", step_size=1)
    with pytest.raises(ValueError, match=r"SciPy backend does not support parameter_type='int', step_size=1"):
        ScipyCFG(dofs=[dof], objective=OBJECTIVE)


def test_config_rejects_eps_with_trust_constr():
    """trust-constr does not accept eps, so the combination is rejected before SciPy is started."""
    dof = RangeDOF(actuator=MovableSignal(name="x"), bounds=(0, 10), parameter_type="float")
    with pytest.raises(ValueError, match="eps is not supported by 'trust-constr'"):
        ScipyCFG(dofs=[dof], objective=OBJECTIVE, optimizer=SCP.TRUST_CONSTR, eps=0.1)
