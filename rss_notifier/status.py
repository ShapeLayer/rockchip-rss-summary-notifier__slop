"""Read-only operational status; never expose Telegram chat IDs or tokens."""
from contextlib import closing
import json
import sqlite3
import time
from pathlib import Path


def snapshot(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {}
        for name in ('inbox', 'articles', 'match_scores', 'retrieval_titles', 'tg_jobs', 'tg_completed'):
            counts[name] = db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0] if name in tables else 0
        if 'tg_subscriptions' in tables:
            counts['subscribers'] = db.execute('SELECT COUNT(DISTINCT chat) FROM tg_subscriptions').fetchone()[0]
        if 'clusters' in tables:
            counts['summaries'] = db.execute('SELECT COUNT(*) FROM clusters WHERE summary IS NOT NULL').fetchone()[0]
        now = time.time()
        stages = []
        if 'runtime_status' in tables:
            for preset, stage, at, detail in db.execute('SELECT preset,stage,at,detail FROM runtime_status ORDER BY preset,stage'):
                stages.append({'preset': preset, 'stage': stage, 'seconds_ago': round(now - at, 1), 'detail': json.loads(detail)})
        return {'counts': counts, 'stages': stages}
