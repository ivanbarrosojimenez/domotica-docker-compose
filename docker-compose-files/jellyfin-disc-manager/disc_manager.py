#!/usr/bin/env python3
"""Jellyfin .disc manager — coordinated with Media Cleaner.

Default behavior is READ-ONLY, and does not run Media Cleaner.
Real mode is explicitly gated by DRY_RUN=false and RUN_MEDIA_CLEANER=true.
Requires a verified NAS marker on each root. Only considers files that existed
in the pre-cleanup snapshot and disappeared during the controlled task.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def envbool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ('true','1','yes','on')


def log(msg):
    print(f"[{dt.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}", flush=True)


JF_URL = os.environ.get('JELLYFIN_URL', 'http://192.168.1.151:8096/jellyfin').rstrip('/')
JF_USER = os.environ.get('JELLYFIN_USER_ID', '').strip()
JF_KEY = os.environ.get('JELLYFIN_API_KEY', '').strip()
JF_KEY_FILE = os.environ.get('JELLYFIN_API_KEY_FILE', '').strip()
if JF_KEY_FILE:
    try:
        JF_KEY = Path(JF_KEY_FILE).read_text().strip()
    except OSError as err:
        log(f"Cannot read key file: {type(err).__name__}")
DRY_RUN = envbool('DRY_RUN', True)
RUN_CLEANER = envbool('RUN_MEDIA_CLEANER', False)
RESTORE_WATCHED = envbool('RESTORE_WATCHED', False)
ALLOW_MISSING_PARENT = envbool('ALLOW_MISSING_PARENT', False)
MAX_MISSING = int(os.environ.get('MAX_MISSING', '10'))
MIN_FILES = int(os.environ.get('MIN_EXISTING_FILES', '5'))
CLEANER_TIMEOUT = int(os.environ.get('CLEANER_TIMEOUT_SECONDS', '1800'))
SCHEDULE = os.environ.get('DAILY_AT', '04:00')
MARKER = os.environ.get('GUARD_FILENAME', '.jellyfin-disc-manager-ready')
DATA = Path('/data')
VIDEO_SUFFIXES = {'.mkv','.mp4','.avi','.mov','.m4v','.ts','.m2ts','.wmv','.webm','.mpg','.mpeg','.flv','.vob','.iso'}
ROOTS = [('/media/movies/', Path('/movies')), ('/media/tv/', Path('/tv'))]


def api(path, method='GET', params=None):
    if not JF_KEY:
        raise RuntimeError('JELLYFIN_API_KEY was not configured (or secret missing)')
    url = JF_URL + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, headers={
        'Authorization': f'MediaBrowser Token="{JF_KEY}"',
        'Accept': 'application/json',
        'User-Agent': 'jellyfin-disc-manager/0.1'
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as err:
        raise RuntimeError(f'Jellyfin API {method} {path}: HTTP {err.code}') from None
    except json.JSONDecodeError:
        raise RuntimeError(f'Jellyfin API {path} returned non-JSON: check Jellyfin base URL') from None


def check_roots():
    for _, root in ROOTS:
        if not root.is_dir() or root.is_symlink():
            raise RuntimeError(f'NAS root absent or symlink: {root}')
        if not (root / MARKER).is_file():
            raise RuntimeError(f'NAS guard marker missing: {root / MARKER}')


def map_path(jellyfin_path):
    if not jellyfin_path:
        return None
    for prefix, root in ROOTS:
        if jellyfin_path.startswith(prefix):
            rel = Path(jellyfin_path[len(prefix):])
            if not rel.parts or '..' in rel.parts or rel.is_absolute():
                return None
            path = root.joinpath(*rel.parts)
            if not path.is_relative_to(root):
                return None
            return path
    return None


def get_items(played_only=True):
    if not JF_USER:
        raise RuntimeError('Define JELLYFIN_USER_ID in Portainer')
    results = []
    start = 0
    while True:
        params = {
            'UserId': JF_USER,
            'Recursive': 'true',
            'IncludeItemTypes': 'Movie,Episode',
            'EnableTotalRecordCount': 'true',
            'Fields': 'Path,UserData',
            'StartIndex': start,
            'Limit': '500',
        }
        if played_only:
            params['IsPlayed'] = 'true'
        body = api('/Items', params=params)
        page = body.get('Items', [])
        if not isinstance(page, list):
            raise RuntimeError('Invalid Jellyfin Items response')
        results.extend(page)
        start += len(page)
        total = body.get('TotalRecordCount')
        if not page or (isinstance(total,int) and start >= total):
            break
        if start > 100000:
            raise RuntimeError('Pagination safety limit exceeded')
    return results


def snapshot():
    check_roots()
    info = api('/System/Info/Public')
    log(f"Jellyfin version {info.get('Version')}; querying watched items")
    watched = get_items(True)
    found = []
    skipped = {'missing_path': 0, 'unsupported': 0, 'not_on_disk': 0}
    for item in watched:
        p = map_path(item.get('Path'))
        if not p:
            skipped['missing_path'] += 1
            continue
        if p.suffix.lower() not in VIDEO_SUFFIXES:
            skipped['unsupported'] += 1
            continue
        if not p.is_file() or p.is_symlink():
            skipped['not_on_disk'] += 1
            continue
        if any(par.is_symlink() for par in p.parents if par not in (Path('/'),)):
            skipped['unsupported'] += 1
            continue
        data = item.get('UserData') or {}
        found.append({'source': str(p), 'jellyfin_path': item.get('Path'),
                      'name': item.get('Name'), 'type': item.get('Type'),
                      'last_played': data.get('LastPlayedDate'), 'id': item.get('Id'),
                      'index': item.get('IndexNumber'), 'season': item.get('ParentIndexNumber')})
    if len(found) < MIN_FILES:
        raise RuntimeError(f'Only {len(found)} watched media files visible; refusing operation (minimum {MIN_FILES})')
    log(f"Watched items from API: {len(watched)}; existing video files: {len(found)}; skipped: {skipped}")
    return found


def candidate_target(item):
    source = Path(item['source'])
    if source.suffix.lower() not in VIDEO_SUFFIXES:
        return None, 'unsupported file extension'
    if item['type'] == 'Episode':
        if not re.search(r'(?i)s\d{1,2}e\d{1,3}', source.stem):
            return None, 'episode filename does not include SxxEyy'
        if re.search(r'(?i)s\d{1,2}e\d{1,3}(?!\d)(?:e\d{1,3}|[- ]e?\d{1,3})(?!\d)', source.stem):
            return None, 'possibly multi-episode source'
        if item.get('index') is None or item.get('season') is None:
            return None, 'episode missing season/episode index'
    return source.with_suffix('.disc'), None


def still_has_video(item):
    """Skip if an alternate version exists for this movie/episode."""
    source = Path(item['source'])
    if not source.parent.is_dir():
        return False
    if item['type'] == 'Movie':
        return any(p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES for p in source.parent.iterdir())
    episode = re.search(r'(?i)s(\d{1,2})e(\d{1,3})', source.stem)
    if not episode:
        return True
    season, number = map(int, episode.groups())
    for p in source.parent.iterdir():
        if not p.is_file() or p.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        match = re.search(r'(?i)s(\d{1,2})e(\d{1,3})', p.stem)
        if match and tuple(map(int, match.groups())) == (season, number):
            return True
    return False


def reconcile(items, preview=False):
    check_roots()
    missing = [i for i in items if not Path(i['source']).exists()]
    if len(missing) > MAX_MISSING:
        raise RuntimeError(f'{len(missing)} vanished files > MAX_MISSING={MAX_MISSING}; no placeholders created')
    if missing and len(missing) / len(items) > 0.30:
        raise RuntimeError('More than 30% of snapshot disappeared; possible NAS incident; refusing')
    log(f'Files missing after controlled cleanup: {len(missing)}; MAX_MISSING={MAX_MISSING}')
    created = []
    for i in missing:
        target, reason = candidate_target(i)
        if reason:
            log(f"SKIP {i['name']}: {reason}")
            continue
        if target.exists() or still_has_video(i):
            log(f"SKIP {i['name']}: placeholder or another video already exists")
            continue
        if not target.parent.is_dir() and not ALLOW_MISSING_PARENT:
            log(f"SKIP {i['name']}: parent directory deleted; ALLOW_MISSING_PARENT=false")
            continue
        if DRY_RUN or preview:
            log(f"DRY_RUN would create: {target}")
            continue
        check_roots()
        if not target.parent.is_dir():
            # Only known path from pre-cleanup snapshot; path checked against mounts.
            target.parent.mkdir(parents=True, exist_ok=True)
        if any(parent.is_symlink() for parent in [target.parent, *list(target.parents)[:3]]):
            log(f"SKIP {i['name']}: symlink in path")
            continue
        try:
            with target.open('x'):
                pass
            created.append((i, target))
            log(f'CREATED {target}')
        except FileExistsError:
            log(f'SKIP {target}: appeared concurrently')
    return created


def tasks():
    res = api('/ScheduledTasks')
    return res if isinstance(res,list) else []


def trigger_cleaner():
    matches = [t for t in tasks() if 'media cleaner' in (t.get('Name') or '').casefold()]
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one Media Cleaner scheduled task, found {len(matches)}')
    task = matches[0]
    if task.get('State') != 'Idle':
        raise RuntimeError('Media Cleaner task is not idle')
    task_id = task.get('Id')
    if not task_id:
        raise RuntimeError('Media Cleaner task has no ID')
    log(f"Starting Jellyfin task: {task.get('Name')}")
    started = dt.datetime.now(dt.timezone.utc)
    api('/ScheduledTasks/Running/' + task_id, method='POST')
    until = time.monotonic() + CLEANER_TIMEOUT
    time.sleep(2)
    while time.monotonic() < until:
        current = next((t for t in tasks() if t.get('Id') == task_id), None)
        if current and current.get('State') == 'Idle':
            result = current.get('LastExecutionResult') or {}
            stamp = result.get('StartTimeUtc') or result.get('EndTimeUtc') or ''
            try:
                run_time = dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))
            except (ValueError, TypeError):
                run_time = None
            status = result.get('Status')
            if run_time is not None and run_time >= started - dt.timedelta(seconds=10):
                if status not in ('Completed', 'Success'):
                    raise RuntimeError(f'Media Cleaner did not complete successfully: {status}')
                log('Media Cleaner task completed successfully')
                return
        time.sleep(3)
    raise RuntimeError('Timed out waiting for Media Cleaner; no placeholders created')


def scan_and_mark(created):
    if not created:
        return
    try:
        api('/Library/Refresh', method='POST')
        log('Requested Jellyfin library scan')
    except Exception as err:
        log(f'Library scan request failed: {err}')
        return
    if not RESTORE_WATCHED:
        log('RESTORE_WATCHED=false: placeholders must be verified/marked manually')
        return
    # Placeholder IDs may change during scans. Poll for exact new file paths.
    pending = {i['jellyfin_path'].rsplit('.',1)[0] + '.disc': i for i, p in created}
    for attempt in range(18):
        if not pending:
            break
        time.sleep(10)
        try:
            items = get_items(False)
        except Exception as err:
            log(f'Waiting for Jellyfin library scan: {err}')
            continue
        by_path = {i.get('Path'): i for i in items if i.get('Path')}
        for path, previous in list(pending.items()):
            fresh = by_path.get(path)
            if not fresh:
                continue
            new_id = fresh.get('Id')
            if not new_id:
                continue
            params = {'DatePlayed': previous['last_played']} if previous.get('last_played') else None
            try:
                api(f'/Users/{JF_USER}/PlayedItems/{new_id}', method='POST', params=params)
                log(f"Restored watched status: {previous['name']}")
                del pending[path]
            except Exception as err:
                log(f"Failed restoring watched status for {previous['name']}: {err}")
    for previous in pending.values():
        log(f"NEEDS REVIEW: placeholder not found in Jellyfin scan: {previous['name']}")


def once():
    log(f'DRY_RUN={DRY_RUN}; RUN_MEDIA_CLEANER={RUN_CLEANER}; RESTORE_WATCHED={RESTORE_WATCHED}')
    items = snapshot()
    DATA.mkdir(parents=True, exist_ok=True)
    file = DATA / 'latest-snapshot.json'
    temp = DATA / 'latest-snapshot.tmp'
    temp.write_text(json.dumps({'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'items': items}, ensure_ascii=False, indent=2))
    temp.replace(file)
    log(f'Snapshot saved to {file}; number of items: {len(items)}')
    if DRY_RUN:
        log('SAFE MODE: no Media Cleaner task launched, no .disc created, no Jellyfin changes')
        return
    if not RUN_CLEANER:
        log('RUN_MEDIA_CLEANER=false: no cleanup started and no placeholders generated')
        return
    trigger_cleaner()
    created = reconcile(items)
    scan_and_mark(created)
    log(f'Run done; placeholders created: {len(created)}')


def report():
    path = DATA / 'latest-snapshot.json'
    if not path.is_file():
        raise RuntimeError('No snapshot; run --once first, before Media Cleaner cleanup')
    payload = json.loads(path.read_text())
    log('Read-only comparison with the most recent snapshot')
    check_roots()
    reconcile(payload.get('items', []), preview=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--once':
        once()
        return
    if len(sys.argv) > 1 and sys.argv[1] == '--report':
        report()
        return
    last_day = None
    hh, mm = (int(x) for x in SCHEDULE.split(':'))
    if hh not in range(24) or mm not in range(60):
        raise RuntimeError('DAILY_AT must be HH:MM')
    log(f'Daily scheduler active at {SCHEDULE} local container time')
    while True:
        now = dt.datetime.now()
        if now.hour == hh and now.minute == mm and last_day != now.date():
            last_day = now.date()
            try:
                once()
            except Exception as err:
                log(f'RUN FAILED: {type(err).__name__}: {err}')
        time.sleep(20)

if __name__ == '__main__':
    try:
        main()
    except Exception as err:
        log(f'FATAL: {type(err).__name__}: {err}')
        sys.exit(1)
