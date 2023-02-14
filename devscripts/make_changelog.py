from __future__ import annotations

import enum
import itertools
import json
import logging
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path

BASE_URL = 'https://github.com'
LOCATION_PATH = Path(__file__).parent

logger = logging.getLogger(__name__)


class CommitGroup(enum.Enum):
    PRIORITY = 'Important'
    CORE = 'Core'
    EXTRACTOR = 'Extractor'
    DOWNLOADER = 'Downloader'
    POSTPROCESSOR = 'Postprocessor'
    MISC = 'Misc.'

    @classmethod
    @cache
    def commit_lookup(cls):
        return {
            name: group
            for group, names in {
                cls.PRIORITY: {''},
                cls.CORE: {
                    'aes',
                    'cache',
                    'compat_utils',
                    'compat',
                    'cookies',
                    'core',
                    'dependencies',
                    'jsinterp',
                    'outtmpl',
                    'plugins',
                    'update',
                    'utils',
                },
                cls.MISC: {
                    'build',
                    'cleanup',
                    'devscripts',
                    'docs',
                    'misc',
                    'test',
                },
                cls.EXTRACTOR: {'extractor', 'extractors'},
                cls.DOWNLOADER: {'downloader'},
                cls.POSTPROCESSOR: {'postprocessor'},
            }.items()
            for name in names
        }

    @classmethod
    def get(cls, value):
        result = cls.commit_lookup().get(value)
        if result:
            logger.debug(f'Mapped {value!r} => {result.name}')
        return result


@dataclass
class Commit:
    hash: str | None
    short: str
    authors: list[str]

    def __str__(self):
        result = f'{self.short!r}'

        if self.hash:
            result += f' ({self.hash[:7]})'

        if self.authors:
            authors = ', '.join(self.authors)
            result += f' by {authors}'

        return result


@dataclass
class CommitInfo:
    details: str | None
    sub_details: tuple[str, ...]
    message: str
    issues: list[str]
    commit: Commit

    def key(self):
        return ((self.details or '').lower(), self.sub_details, self.message)


class Changelog:
    MISC_RE = re.compile(r'(?:^|\b)(?:lint(?:ing)?|misc|format(?:ting)?|fixes)(?:\b|$)', re.IGNORECASE)

    def __init__(self, groups, repo):
        self._groups = groups
        self._repo = repo

    def __str__(self):
        return '\n'.join(self._format_groups(self._groups)).replace('\t', '    ')

    def _format_groups(self, groups):
        yield '## Changelog'

        for item in CommitGroup:
            group = groups[item]
            if group:
                yield self.format_module(item.value, group)

    def format_module(self, name, group):
        return f'### {name} changes\n' + '\n'.join(self._format_group(group))

    def _format_group(self, group):
        sorted_group = sorted(group, key=CommitInfo.key)
        detail_groups = itertools.groupby(sorted_group, lambda item: (item.details or '').lower())
        for details, items in detail_groups:
            if not details:
                indent = ''
            else:
                yield f'- {details}'
                indent = '\t'

            if details == 'cleanup':
                items, cleanup_misc_items = self._filter_cleanup_misc_items(items)

            sub_detail_groups = itertools.groupby(items, lambda item: item.sub_details)
            for sub_details, entries in sub_detail_groups:
                if not sub_details:
                    for entry in entries:
                        yield f'{indent}- {self.format_single_change(entry)}'
                    continue

                prefix = f'{indent}- {", ".join(sub_details)}'
                entries = list(entries)
                if len(entries) == 1:
                    yield f'{prefix}: {self.format_single_change(entries[0])}'
                    continue

                yield prefix
                for entry in entries:
                    yield f'{indent}\t- {self.format_single_change(entry)}'

            if details == 'cleanup' and cleanup_misc_items:
                yield from self._format_cleanup_misc_sub_group(cleanup_misc_items)

    def _filter_cleanup_misc_items(self, items):
        cleanup_misc_items = defaultdict(list)
        new_items = []
        for item in items:
            if self.MISC_RE.search(item.message):
                cleanup_misc_items[tuple(item.commit.authors)].append(item)
            else:
                new_items.append(item)

        return items, cleanup_misc_items

    def _format_cleanup_misc_sub_group(self, group):
        prefix = '\t- Miscellaneous'
        if len(group) == 1:
            yield f'{prefix}: {next(self._format_cleanup_misc_items(group))}'
            return

        yield prefix
        for message in self._format_cleanup_misc_items(group):
            yield f'\t\t- {message}'

    def _format_cleanup_misc_items(self, group):
        for authors, infos in group.items():
            message = ', '.join(
                f'[{info.commit.hash or "unknown":.7}]({self.repo_url}/commit/{info.commit.hash})'
                for info in sorted(infos, key=lambda item: item.commit.hash or ''))
            yield f'{message} by {self._format_authors(authors)}'

    def format_single_change(self, info):
        message = (
            info.message if info.commit.hash is None else
            f'[{info.message}]({self.repo_url}/commit/{info.commit.hash})')
        if info.issues:
            message = f'{message} ({self._format_issues(info.issues)})'

        if not info.commit.authors:
            return message

        return f'{message} by {self._format_authors(info.commit.authors)}'

    def _format_issues(self, issues):
        return ', '.join(f'[#{issue}]({self.repo_url}/issues/{issue})' for issue in issues)

    @staticmethod
    def _format_authors(authors):
        return ', '.join(f'[{author}]({BASE_URL}/{author})' for author in authors)

    @property
    def repo_url(self):
        return f'{BASE_URL}/{self._repo}'


class CommitRange:
    COMMAND = 'git'
    COMMIT_SEPARATOR = '-----'

    AUTHOR_INDICATOR_RE = re.compile(r'Authored by:? ', re.IGNORECASE)
    MESSAGE_RE = re.compile(r'''
        (?:\[
            (?P<prefix>[^\]\/:,]+)
            (?:/(?P<details>[^\]:,]+))?
            (?:[:,](?P<sub_details>[^\]]+))?
        \]\ )?
        (?:`?(?P<sub_details_alt>[^:`]+)`?: )?
        (?P<message>.+?)
        (?:\ \((?P<issues>\#\d+(?:,\ \#\d+)*)\))?
        ''', re.VERBOSE)
    EXTRACTOR_INDICATOR_RE = re.compile(r'(?:Fix|Add)\s+Extractors?', re.IGNORECASE)

    def __init__(self, start, end, default_author=None) -> None:
        self._start = start
        self._end = end
        self._commits = {commit.hash: commit for commit in self._get_commits_raw(default_author)}
        self._commits_added = []

    @classmethod
    def from_single(cls, commitish='HEAD', default_author=None):
        start_commitish = cls.get_prev_tag(commitish)
        end_commitish = cls.get_next_tag(commitish)
        if start_commitish == end_commitish:
            start_commitish = cls.get_prev_tag(f'{commitish}~')
        logger.info(f'Determined range from {commitish!r}: {start_commitish}..{end_commitish}')
        return cls(start_commitish, end_commitish, default_author)

    @classmethod
    def get_prev_tag(cls, commitish):
        command = [cls.COMMAND, 'describe', '--tags', '--abbrev=0', commitish]
        return subprocess.check_output(command, text=True).strip()

    @classmethod
    def get_next_tag(cls, commitish):
        result = subprocess.run(
            [cls.COMMAND, 'describe', '--contains', '--abbrev=0', commitish],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        if result.returncode:
            return 'HEAD'

        return result.stdout.partition('~')[0].strip()

    def __iter__(self):
        return iter(itertools.chain(self._commits.values(), self._commits_added))

    def __len__(self):
        return len(self._commits) + len(self._commits_added)

    def __contains__(self, commit):
        if isinstance(commit, Commit):
            if not commit.hash:
                return False
            commit = commit.hash

        return commit in self._commits

    def _is_ancestor(self, commitish):
        return bool(subprocess.call(
            [self.COMMAND, 'merge-base', '--is-ancestor', commitish, self._start]))

    def _get_commits_raw(self, default_author):
        result = subprocess.check_output([
            self.COMMAND, 'log', f'--format=%H%n%s%n%b%n{self.COMMIT_SEPARATOR}',
            f'{self._start}..{self._end}'], text=True)
        lines = iter(result.splitlines(False))
        for line in lines:
            commit_hash = line
            short = next(lines)
            skip = short.startswith('Release ') or short == '[version] update'

            authors = [default_author] if default_author else []
            for line in iter(lambda: next(lines), self.COMMIT_SEPARATOR):
                match = self.AUTHOR_INDICATOR_RE.match(line)
                if match:
                    authors = sorted(map(str.strip, line[match.end():].split(',')), key=str.casefold)

            commit = Commit(commit_hash, short, authors)
            if skip:
                logger.debug(f'Skipped commit: {commit}')
            else:
                yield commit

    def apply_overrides(self, overrides):
        for override in overrides:
            when = override.get('when')
            if when and when not in self and when != self._start:
                logger.debug(f'Ignored {when!r}, not in commits {self._start!r}')
                continue

            override_hash = override.get('hash')
            if override['action'] == 'add':
                commit = Commit(override.get('hash'), override['short'], override.get('authors') or [])
                logger.info(f'ADD    {commit}')
                self._commits_added.append(commit)

            elif override['action'] == 'remove':
                if override_hash in self._commits:
                    logger.info(f'REMOVE {self._commits[override_hash]}')
                    del self._commits[override_hash]

            elif override['action'] == 'change':
                if override_hash not in self._commits:
                    continue
                commit = Commit(override_hash, override['short'], override['authors'])
                logger.info(f'CHANGE {self._commits[commit.hash]} -> {commit}')
                self._commits[commit.hash] = commit

        self._commits = {key: value for key, value in reversed(self._commits.items())}

    def groups(self):
        groups = defaultdict(list)
        for commit in self:
            match = self.MESSAGE_RE.fullmatch(commit.short)
            if not match:
                logger.error(f'Error parsing short commit message: {commit.short!r}')
                continue

            prefix, details, sub_details, sub_details_alt, message, issues = match.groups()
            group = None
            if prefix:
                if prefix == 'priority':
                    prefix, _, details = (details or '').partition('/')
                    logger.debug(f'Priority: {message!r}')
                    group = CommitGroup.PRIORITY

                if not details and prefix:
                    if prefix not in ('extractor', 'postprocessor', 'downloader'):
                        logger.debug(f'Replaced details with {prefix!r}')
                        details = prefix or None

                if details == 'common':
                    details = None

                if details:
                    details = details.strip()

            else:
                group = CommitGroup.CORE

            sub_details = f'{sub_details or ""},{sub_details_alt or ""}'.lower().replace(':', ',')
            sub_details = tuple(filter(None, map(str.strip, sub_details.split(','))))

            issues = [issue.strip()[1:] for issue in issues.split(',')] if issues else []

            if not group:
                group = CommitGroup.get(prefix.lower())
                if not group:
                    if self.EXTRACTOR_INDICATOR_RE.search(commit.short):
                        group = CommitGroup.EXTRACTOR
                    else:
                        group = CommitGroup.POSTPROCESSOR
                    logger.warning(f'Failed to map {commit.short!r}, selected {group.name}')

            commit_info = CommitInfo(details, sub_details, message.strip(), issues, commit)
            logger.debug(f'Resolved {commit.short!r} to {commit_info!r}')
            groups[group].append(commit_info)

        return groups


def get_new_contributors(contributors_path, commits):
    contributors = set()
    if contributors_path.exists():
        with contributors_path.open() as file:
            for line in filter(None, map(str.strip, file)):
                author, _, _ = line.partition(' (')
                authors = author.split('/')
                contributors.update(map(str.casefold, authors))

    new_contributors = set()
    for commit in commits:
        for author in commit.authors:
            author_folded = author.casefold()
            if author_folded not in contributors:
                contributors.add(author_folded)
                new_contributors.add(author)

    return sorted(new_contributors, key=str.casefold)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Create a changelog markdown from a git commit range')
    parser.add_argument(
        'commitish', default='HEAD', nargs='?',
        help='The commitish to create the range from (default: HEAD)')
    parser.add_argument(
        '-v', '--verbosity', action='count', default=0,
        help='increase verbosity each time specified')
    parser.add_argument(
        '-c', '--contributors', action='store_true',
        help='update CONTRIBUTORS file (default: false)')
    parser.add_argument(
        '--contributors-path', type=Path, default=LOCATION_PATH.parent / 'CONTRIBUTORS',
        help='path to the override CONTRIBUTORS file')
    parser.add_argument(
        '-o', '--override', action='store_true',
        help='use the override json in commit generation (default: false)')
    parser.add_argument(
        '--override-path', type=Path, default=LOCATION_PATH / 'changelog_override.json',
        help='path to the override json file')
    parser.add_argument(
        '--default-author', default='pukkandan',
        help='the author to use when no author indicator is specified')
    parser.add_argument(
        '--repo', default='yt-dlp/yt-dlp',
        help='the github repository to use for the operations')
    args = parser.parse_args()

    logging.basicConfig(
        datefmt='%Y-%m-%d %H-%M-%S', format='{asctime} | {levelname:<8} | {message}',
        level=logging.WARNING - 10 * args.verbosity, style='{', stream=sys.stderr)

    commits = CommitRange.from_single(args.commitish, args.default_author)

    if args.override:
        if args.override_path.exists():
            with args.override_path.open() as file:
                overrides = json.load(file)
            commits.apply_overrides(overrides)
        else:
            logger.warning(f'File {args.override.as_posix()} does not exist')

    logger.info(f'Loaded {len(commits)} commits')

    new_contributors = get_new_contributors(args.contributors_path, commits)
    if args.contributors:
        with args.contributors_path.open('a') as file:
            for contributor in new_contributors:
                file.write(f'{contributor}\n')
        logger.info(f'Added new contributors: {", ".join(new_contributors)}')
    else:
        logger.debug(f'New contributors: {new_contributors}')

    print(Changelog(commits.groups(), args.repo))
