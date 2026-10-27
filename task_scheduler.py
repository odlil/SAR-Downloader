"""A shared concurrency budget for file, DEM and RSLC workers."""
from contextlib import contextmanager
from collections import deque
import threading


class Cancelled(Exception):
    pass


START_ACTIONS = frozenset(('start', 'resume', 'retry'))
MAX_TASK_IDS = 2000  # Up to 1000 file/DEM tasks plus 1000 subset tasks.


def validate_action(action, ids=None, *, starting_only=False):
    """Keep queue operations and their preflight requests on the same contract."""
    from earthdata_auth import TransferError
    allowed = START_ACTIONS if starting_only else START_ACTIONS | {'pause', 'remove'}
    if not isinstance(action, str) or action not in allowed:
        raise TransferError('未知下载操作。')
    if ids is not None and (not isinstance(ids, list) or len(ids) > MAX_TASK_IDS
                            or any(not isinstance(value, str) for value in ids)):
        raise TransferError('任务编号无效。')


class TaskBudget:
    def __init__(self, limit=2):
        self.condition = threading.Condition()
        self.active = 0
        self.waiting = deque()
        self.set_limit(limit)

    def set_limit(self, limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4:
            raise ValueError('同时运行任务数需要为 1–4。')
        with self.condition:
            self.limit = limit
            self.condition.notify_all()

    @contextmanager
    def slot(self, stop):
        with self.condition:
            ticket = object()
            self.waiting.append(ticket)
            try:
                while (self.active >= self.limit or self.waiting[0] is not ticket) and not stop.is_set():
                    self.condition.wait(.1)
                if stop.is_set():
                    raise Cancelled()
                self.active += 1
            finally:
                self.waiting.remove(ticket)
                self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()
