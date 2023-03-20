from typing import AnyStr, Dict, List

from bluesky_queueserver_api.api_threads import API_Threads_Mixin
from pydantic import BaseModel

DEFAULT_NAME = "agent_suggestions"


class Suggestion(BaseModel):
    ask_uid: str  # UID from the agent ask message
    plan_name: str
    plan_args: list = []
    plan_kwargs: dict = {}


class AdjudicatorMsg(BaseModel):
    agent_name: str
    suggestions_uid: str
    suggestions: Dict[AnyStr, List[Suggestion]]  # TLA: list


class Judgment(BaseModel):
    """Allow for positional arguments from user derived make judgements"""

    re_manager: API_Threads_Mixin
    agent_name: str
    suggestion: Suggestion

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, re_manager: API_Threads_Mixin, agent_name: str, suggestion: Suggestion, **kwargs) -> None:
        super().__init__(re_manager=re_manager, agent_name=agent_name, suggestion=suggestion, **kwargs)


if __name__ == "__main__":
    """Example main to show serializing capabilities"""
    import msgpack

    suggestion = Suggestion(ask_uid="123", plan_name="test_plan", plan_args=[1, 3], plan_kwargs={"md": {}})
    msg = AdjudicatorMsg(
        agent_name="aardvark",
        suggestions_uid="456",
        suggestions={
            "pdf": [
                suggestion,
                suggestion,
            ],
            "bmm": [
                suggestion,
            ],
        },
    )
    print(msg)
    s = msgpack.dumps(msg.dict())
    new_msg = AdjudicatorMsg(**msgpack.loads(s))
    print(new_msg)
