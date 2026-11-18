"""Trusted response destination for this invocation, separate from data visibility."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from cockp.core.contracts import Output, Visibility


@dataclass(frozen=True)
class ReplyOrigin:
    private: bool
    user_id: str
    group_id: str


reply_origin = ContextVar("cockp_reply_origin", default=None)


@contextmanager
def response_scope(private, user_id, group_id):
    token = reply_origin.set(ReplyOrigin(bool(private), user_id, group_id))
    try:
        yield
    finally:
        reply_origin.reset(token)


def route_outputs(outputs, state, *, hidden=False):
    origin = reply_origin.get()
    objects = [Output.model_validate(output) for output in outputs]
    if origin is None or hidden:
        return objects
    result = []
    for output in objects:
        if origin.private:
            if output.visibility.scope == "public" or (
                output.visibility.scope == "private" and origin.user_id in output.visibility.recipients
            ):
                output = output.model_copy(update={"visibility": Visibility(scope="private", recipients=[origin.user_id])})
            else:
                output = Output(text="操作已记录。", visibility=Visibility(scope="private", recipients=[origin.user_id]))
        elif output.visibility.scope != "public":
            # A destination preference is not permission to publish the payload.
            # Producers that can answer in a group must build a public projection
            # themselves. Literal secret matching cannot authorize publication.
            output = Output(text="操作已记录。" if output.visibility.scope == "kp" else "操作已处理。")
        result.append(output)
    return result
