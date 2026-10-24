"""RSS collection runs independently of slow model inference."""
import logging
import threading
import time

from .core import Engine

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, path, presets, interval):
        self.path, self.presets, self.interval = path, presets, interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='rss-collector', daemon=True)

    def run(self):
        engine = Engine(self.path, None)
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                for preset in self.presets:
                    if self.stop.is_set():
                        break
                    try:
                        engine.collect(preset)
                    except Exception as exc:
                        log.warning('Collection failed preset=%s error=%s', preset['name'], type(exc).__name__)
                self.stop.wait(max(1, self.interval - (time.monotonic() - started)))
        finally:
            engine.db.close()
