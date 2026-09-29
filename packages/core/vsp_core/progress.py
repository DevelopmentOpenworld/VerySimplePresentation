"""What a run is doing, told as it happens (owner decision 55, 29.09.2026: «интерактивные элементы в реальном времени
отображающие процесс который идёт (без симуляции, всё честно)»).

An event is written by the code that does the work, at the moment it begins or ends a step: the pipeline around its steps
(with the seconds the step took and what it gave), the model client around every request it sends. Nothing is estimated,
scheduled or played back; a step the run skips is not in its plan. The page that reads the events shows the run itself.

The events of a run are kept in memory for the page that started it (a long poll, Progress.after) and are gone with the
server process: run.json stays the record of a run.
"""
from datetime import datetime, timezone
import threading
import time

SCHEMA = 'vsp.progress/1'


class Progress:
    def __init__(self, work):
        self.work = work                      # 'prepare' | 'generate' | 'fix'
        self.events = []
        self.closed = False
        self.created = time.time()
        self._changed = threading.Condition()
        self._began = None                    # perf_counter of the first event: the clock of the run on the server
        self._steps = {}

    def tell(self, event, **fields):
        now = time.perf_counter()
        with self._changed:
            if self.closed:
                return None
            if self._began is None:
                self._began = now
            record = {'n': len(self.events) + 1, 'at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                      't': round(now - self._began, 3), 'event': event,
                      **{k: v for k, v in fields.items() if v is not None}}
            self.events.append(record)
            if event in ('done', 'failed'):
                self.closed = True
            self._changed.notify_all()
            return record

    def plan(self, steps):
        """The steps this run goes through, in their order; told before the first of them begins."""
        self.tell('plan', work=self.work, steps=list(steps))

    def begin(self, step, **fields):
        self._steps[step] = time.perf_counter()
        self.tell('step', step=step, state='began', **fields)

    def end(self, step, **fields):
        began = self._steps.pop(step, None)
        seconds = fields.pop('seconds', None)
        if seconds is None and began is not None:
            seconds = time.perf_counter() - began
        self.tell('step', step=step, state='ended', seconds=None if seconds is None else round(seconds, 2), **fields)

    def request(self, record):
        """The listener of the model client (llm.listen): a request went out, an answer came back."""
        self.tell('request', **record)

    def done(self, **fields):
        self.tell('done', **fields)

    def failed(self, error):
        self.tell('failed', error=str(error)[:500], running=sorted(self._steps))

    def after(self, number, timeout=20.0):
        """The events after event `number`; waits for the next one up to `timeout` seconds when there is none yet."""
        with self._changed:
            if len(self.events) <= number and not self.closed:
                self._changed.wait(timeout)
            return self.events[number:], self.closed


class Quiet:
    """A run nobody watches: the same calls, nothing kept."""
    work = None

    def tell(self, event, **fields):
        return None

    def plan(self, steps):
        return None

    def begin(self, step, **fields):
        return None

    def end(self, step, **fields):
        return None

    def request(self, record):
        return None

    def done(self, **fields):
        return None

    def failed(self, error):
        return None
