"""Assignment-local cancellation: interrupt execution once, protect finalization.

The pool owns the finite outer deadline. This scope only defers repeated user
signals; it does not catch errors or grant any cleanup/ownership authority.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import signal
import threading

PIER_CLEANUP_SECONDS = 90.0
WORKER_STOP_SECONDS = PIER_CLEANUP_SECONDS + 30.0

@dataclass
class Cancellation:
    requested: bool = False
    finalizing: bool = False

    def receive(self, signum, frame):
        self.requested = True
        if not self.finalizing:
            self.finalizing = True
            raise KeyboardInterrupt

_current = ContextVar("dradar_cancellation", default=None)

@contextmanager
def scope():
    existing = _current.get()
    if existing is not None:
        yield existing
        return
    state = Cancellation()
    token = _current.set(state)
    handlers = {}
    try:
        if threading.current_thread() is threading.main_thread():
            for name in ("SIGINT", "SIGBREAK"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, state.receive)
        yield state
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        _current.reset(token)

def protect_finalization(*, cancelled=False):
    state = _current.get()
    if state is not None:
        state.finalizing = True
        state.requested |= cancelled

def begin_execution():
    state = _current.get()
    if state is not None:
        if state.requested:
            raise KeyboardInterrupt
        state.finalizing = False

def requested():
    state = _current.get()
    return state is not None and state.requested

def scoped(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with scope():
            return function(*args, **kwargs)
    return wrapped
