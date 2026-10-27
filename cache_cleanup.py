"""Remove app-owned temporary files, refusing links and Windows reparse points."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

TASK_ID = re.compile(r'[0-9a-f]{24}')
SUBSET_RESUME_SUFFIXES = ('.part', '.progress.json', '.progress.json.tmp', '.progress.sqlite',
                          '.progress.sqlite-wal', '.progress.sqlite-shm', '.progress.sqlite-journal')


def checked_path(path):
    path = Path(os.path.abspath(path))
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
            raise OSError('Cache cleanup refuses links and reparse points.')
    return path


class Cleanup:
    def __init__(self):
        self.bytes = self.files = self.skipped = 0

    @staticmethod
    def inspect(path, root):
        """Read the exact same bounded, link-free tree used by deletion."""
        root, path = checked_path(root), checked_path(path)
        if path == root or not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
            raise OSError('Cache path is outside its intended directory.')
        if not path.exists():
            return []
        entries, pending = [], [path]
        while pending:
            if len(entries) > 100000:
                raise OSError('Too many cache entries.')
            entry = checked_path(pending.pop())
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode):
                pending.extend(entry.iterdir())
            elif not stat.S_ISREG(info.st_mode):
                raise OSError('Unexpected cache file type.')
            entries.append((entry, info))
        return entries

    def remove(self, path, root):
        """Preflight a strictly contained tree before removing any of its entries."""
        try:
            entries = self.inspect(path, root)
            # Parents precede their descendants in the preflight traversal.
            for entry, info in reversed(entries):
                checked_path(entry)
                if stat.S_ISDIR(info.st_mode):
                    entry.rmdir()
                else:
                    entry.unlink()
                    self.bytes += info.st_size
                    self.files += 1
        except OSError:
            self.skipped += 1

    def tasks(self, folder, protected=(), only=None):
        """Only hexadecimal task directories in an app-owned parts directory."""
        try:
            folder = checked_path(folder)
            if not folder.exists():
                return
            for child in folder.iterdir():
                if TASK_ID.fullmatch(child.name) and child.name not in protected and (only is None or child.name in only):
                    self.remove(child, folder)
            self.empty(folder)
        except OSError:
            self.skipped += 1

    def tiles(self, folder):
        try:
            folder = checked_path(folder)
            if not folder.exists():
                return
            for child in folder.iterdir():
                self.remove(child, folder)
            self.empty(folder)
        except OSError:
            self.skipped += 1

    def completed_parts(self, folder):
        try:
            folder = checked_path(folder)
            if not folder.exists():
                return
            for child in folder.iterdir():
                checked_path(child)
                if TASK_ID.fullmatch(child.name) and child.is_dir() and not (child / 'payload.part').exists():
                    self.remove(child, folder)
            self.empty(folder)
        except OSError:
            self.skipped += 1

    def empty(self, folder):
        try:
            checked_path(folder).rmdir()
        except OSError:
            pass  # It may still hold another task's files.

    def report(self):
        return {'bytesFreed': self.bytes, 'filesRemoved': self.files, 'skipped': self.skipped}
