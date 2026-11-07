"""Deterministic local workers for UI behavior tests; network tasks stay mocked."""


def run_local(fn, *args, local=False, on_done=None, on_error=None, on_progress=None, **kwargs):
    if not local:
        return
    result = fn(*args, **kwargs)
    if on_done:
        on_done(result)
