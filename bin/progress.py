'''Progress display for pkg_sync using rich.

Provides two progress contexts for the two-phase sync:
  - ScanProgress: single bar for the pkgsinfo scan phase
  - TransferProgress: overall bar + per-file bars for S3 transfers

Falls back to simple print() when stdout is not a TTY (CI, pipes).
'''

import sys
from rich.console import Console
from rich.progress import (
    Progress,
    BarColumn,
    DownloadColumn,
    TextColumn,
    TimeRemainingColumn,
    TaskProgressColumn,
    SpinnerColumn,
    MofNCompleteColumn,
    TransferSpeedColumn,
)


def _is_tty():
    return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()


class ScanProgress:
    '''Progress bar for the scan phase (checking which files need transfer).

    Usage:
        with ScanProgress(total=616) as sp:
            sp.advance()           # one pkgsinfo scanned
            sp.log("message")      # print below the bar
    '''

    def __init__(self, total, label="Scanning pkgsinfos"):
        self._total = total
        self._label = label
        self._tty = _is_tty()
        self._progress = None
        self._task = None

    def __enter__(self):
        if self._tty:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.description}"),
                BarColumn(bar_width=40),
                MofNCompleteColumn(),
                TaskProgressColumn(),
                TextColumn("[dim]{task.fields[current]}"),
                console=Console(stderr=True),
                transient=True,
            )
            self._progress.__enter__()
            self._task = self._progress.add_task(self._label, total=self._total, current="")
        return self

    def __exit__(self, *exc):
        if self._progress:
            self._progress.__exit__(*exc)

    def advance(self, n=1):
        if self._progress and self._task is not None:
            self._progress.advance(self._task, n)

    def set_current(self, label, max_width=40):
        '''Update the "currently scanning" label.'''
        if self._progress and self._task is not None:
            if len(label) > max_width:
                label = label[:max_width - 1] + "..."
            self._progress.update(self._task, current=label)

    def log(self, message):
        if self._progress:
            self._progress.console.print(message)
        else:
            print(message)


class TransferProgress:
    '''Multi-bar progress for the transfer phase (concurrent S3 downloads/uploads).

    Shows an overall progress bar and one sub-bar per active file with
    byte-level progress driven by boto3 callbacks.

    Usage:
        with TransferProgress(items, mode='download') as tp:
            def do_transfer(item):
                callback = tp.file_callback(item)
                download_file(item, bucket, callback=callback)
                tp.file_done(item)
    '''

    def __init__(self, items, mode='download'):
        self._items = items
        self._mode = mode
        self._tty = _is_tty()
        self._progress = None
        self._overall_task = None
        self._file_tasks = {}
        self._active_callbacks = {}
        # Munki stores sizes in KB; convert to bytes for rich's DownloadColumn
        self._total_bytes = sum(i['size'] * 1024 for i in items)

    def __enter__(self):
        if self._tty:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold]{task.description}", justify="left"),
                BarColumn(bar_width=30),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=Console(stderr=True),
                transient=False,
            )
            self._progress.__enter__()
            verb = "Downloading" if self._mode == 'download' else "Uploading"
            self._overall_task = self._progress.add_task(
                f"[blue]{verb} {len(self._items)} files",
                total=self._total_bytes,
            )
        else:
            verb = "Downloading" if self._mode == 'download' else "Uploading"
            print(f"{verb} {len(self._items)} file(s)...")
        return self

    def __exit__(self, *exc):
        if self._progress:
            self._progress.__exit__(*exc)

    def file_start(self, item):
        '''Register a file as actively transferring.'''
        if self._progress:
            existing = self._file_tasks.get(item['name'])
            if existing is not None:
                self._progress.reset(existing, total=item['size'] * 1024,
                                     description=f"  {item['name']}")
            else:
                task_id = self._progress.add_task(
                    f"  {item['name']}",
                    total=item['size'] * 1024,
                )
                self._file_tasks[item['name']] = task_id

    def file_callback(self, item):
        '''Returns a boto3-compatible callback that advances the per-file and overall bars.'''
        if not self._progress:
            return None

        # Roll back overall progress from any previous attempt
        prev_cb = self._active_callbacks.get(item['name'])
        if prev_cb and prev_cb.actual_bytes[0] > 0:
            self._progress.advance(self._overall_task, -prev_cb.actual_bytes[0])

        self.file_start(item)
        file_task = self._file_tasks.get(item['name'])
        overall_task = self._overall_task
        estimated = item['size'] * 1024

        progress = self._progress
        actual_bytes = [0]

        def _callback(bytes_transferred):
            actual_bytes[0] += bytes_transferred
            if file_task is not None:
                progress.advance(file_task, bytes_transferred)
            if overall_task is not None:
                progress.advance(overall_task, bytes_transferred)

        _callback.actual_bytes = actual_bytes
        _callback.estimated = estimated
        self._active_callbacks[item['name']] = _callback
        return _callback

    def file_done(self, item):
        '''Mark a file as complete. Adjusts totals if actual size differs from estimate.'''
        if self._progress:
            task_id = self._file_tasks.get(item['name'])
            if task_id is not None:
                self._progress.update(task_id, description=f"  [green]done {item['name']}")
            cb = self._active_callbacks.pop(item['name'], None)
            if cb and self._overall_task is not None:
                diff = cb.actual_bytes[0] - cb.estimated
                if diff != 0:
                    new_total = (self._total_bytes + diff)
                    self._total_bytes = new_total
                    self._progress.update(self._overall_task, total=new_total)
        else:
            verb = "downloaded" if self._mode == 'download' else "uploaded"
            size_mb = item['size'] / 1024
            print(f"  {item['name']} {verb} ({size_mb:.1f}MB)")

    def file_retry(self, item, attempt, max_retries):
        '''Mark a file as retrying.'''
        if self._progress:
            task_id = self._file_tasks.get(item['name'])
            if task_id is not None:
                self._progress.update(task_id,
                                      description=f"  [yellow]retry {item['name']} ({attempt}/{max_retries})")
        else:
            print(f"  {item['name']}: retry {attempt}/{max_retries}")

    def file_error(self, item, error):
        '''Mark a file as failed.'''
        short_error = str(error)
        if len(short_error) > 80:
            short_error = short_error[:77] + "..."
        if self._progress:
            task_id = self._file_tasks.get(item['name'])
            if task_id is not None:
                self._progress.update(task_id, description=f"  [red]FAIL {item['name']}: {short_error}")
        else:
            print(f"  FAIL {item['name']}: {short_error}")
