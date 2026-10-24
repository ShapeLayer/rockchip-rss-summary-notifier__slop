import argparse
import logging
import time
import tomllib
from pathlib import Path

from .core import DISPATCHERS, Engine, Model
from .telegram import Worker


def validate(config):
    Model(config['model'])
    if config['model'].get('backend', 'ollama') not in ('ollama', 'openai'):
        raise ValueError('model.backend must be ollama or openai')
    names = set()
    for p in config['presets']:
        if p['name'] in names:
            raise ValueError('Duplicate preset name')
        names.add(p['name'])
        ids = [f['id'] for f in p['feeds']]
        if len(ids) != len(set(ids)) or not ids:
            raise ValueError('Feed IDs must be unique within each preset')
        if not 2 <= p.get('min_sources', 3) <= len(ids):
            raise ValueError('min_sources must be between 2 and feed count')
        if not 0 <= p.get('similarity_threshold', .8) <= 1 or p.get('window_hours', 24) <= 0:
            raise ValueError('Invalid threshold or time window')
        dispatchers = p['dispatchers']
        if not dispatchers or len({d['id'] for d in dispatchers}) != len(dispatchers):
            raise ValueError('Dispatcher IDs must be nonempty and unique')
        for d in dispatchers:
            if d['type'] not in DISPATCHERS:
                raise ValueError('Unknown dispatcher type')
        for f in p['feeds']:
            if not f['url'].startswith(('http://', 'https://')):
                raise ValueError('Feed URL must be HTTP(S)')
    if config.get('poll_seconds', 300) < 1:
        raise ValueError('poll_seconds must be positive')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/news.toml')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--check', action='store_true', help='Validate configuration without network calls')
    parser.add_argument('--preset', action='append', help='Run selected preset; may repeat')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with open(args.config, 'rb') as file:
        config = tomllib.load(file)
    validate(config)
    if args.check:
        print('Configuration valid')
        return
    selected = [p for p in config['presets'] if p.get('enabled', True) and (not args.preset or p['name'] in args.preset)]
    if not selected or (args.preset and set(args.preset) - {p['name'] for p in selected}):
        parser.error('No enabled preset matches selection')
    database = Path(config.get('database', 'state/notifier.sqlite3'))
    database.parent.mkdir(parents=True, exist_ok=True)
    engine = Engine(database, Model(config['model']))
    worker = Worker(database, selected)
    worker.thread.start()
    try:
        while True:
            for preset in selected:
                try:
                    engine.process(preset)
                except Exception as exc:
                    # Never log request URLs/errors which could expose a bot token.
                    logging.error('Preset %s failed (%s); will retry next cycle', preset['name'], type(exc).__name__)
                    if args.once:
                        raise SystemExit(1) from None
            if args.once:
                break
            time.sleep(config.get('poll_seconds', 300))
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop.set()
        worker.thread.join(timeout=40)
        engine.db.close()


if __name__ == '__main__':
    main()
