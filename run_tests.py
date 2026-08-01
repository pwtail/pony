#!/usr/bin/env python

"""Run the Pony ORM and utility test suites on Python 3.10 through 3.14."""

from __future__ import print_function

import os
import shutil
import subprocess
import sys


ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_VERSIONS = ('3.10', '3.11', '3.12', '3.13', '3.14')
TEST_SUITES = (
    (
        'ORM',
        ('-m', 'unittest', 'discover', '-s', 'pony/orm/tests',
         '-p', 'test_*.py'),
    ),
    (
        'Utils',
        ('-m', 'unittest', 'discover', '-s', 'pony/utils/tests',
         '-p', 'test_*.py'),
    ),
)


def run_command(command, capture_output=False):
    kwargs = {'cwd': ROOT_DIR}
    if capture_output:
        kwargs.update(
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    return subprocess.run(command, **kwargs)


def get_python_version(executable):
    command = [
        executable,
        '-c',
        'import sys; print("%d.%d" % sys.version_info[:2])',
    ]
    try:
        result = run_command(command, capture_output=True)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else None


def normalize_executable(executable):
    executable = executable.strip()
    if os.path.isfile(executable):
        return os.path.abspath(executable)
    return shutil.which(executable) or executable


def find_python(version):
    candidates = []

    current_version = '%d.%d' % sys.version_info[:2]
    if current_version == version:
        candidates.append(sys.executable)

    uv = shutil.which('uv')
    if uv:
        result = run_command(
            [uv, 'python', 'find', version],
            capture_output=True,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            if lines:
                candidates.append(lines[-1])

    if os.name == 'nt':
        launcher = shutil.which('py')
        if launcher:
            result = run_command(
                [
                    launcher,
                    '-' + version,
                    '-c',
                    'import sys; print(sys.executable)',
                ],
                capture_output=True,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().splitlines()
                if lines:
                    candidates.append(lines[-1])

    candidates.extend(('python' + version, 'python' + version + '.exe'))

    checked = set()
    for candidate in candidates:
        executable = normalize_executable(candidate)
        normalized = os.path.normcase(executable)
        if normalized in checked:
            continue
        checked.add(normalized)
        if get_python_version(executable) == version:
            return executable
    return None


def run_suite(executable, arguments):
    command = [executable] + list(arguments)
    print('> %s' % subprocess.list2cmdline(command), flush=True)
    try:
        return run_command(command).returncode
    except OSError as exc:
        print('Cannot run interpreter: %s' % exc, file=sys.stderr)
        return 1


def format_status(exit_code):
    if exit_code is None:
        return 'NOT FOUND'
    if exit_code == 0:
        return 'PASS'
    return 'FAIL (%d)' % exit_code


def main():
    results = []

    for version in PYTHON_VERSIONS:
        print('\n=== Python %s ===' % version, flush=True)
        executable = find_python(version)
        if executable is None:
            print(
                'Python %s was not found. Install it or make uv/py available.'
                % version,
                file=sys.stderr,
            )
            results.append((version, None, None))
            continue

        print('Interpreter: %s' % executable, flush=True)
        suite_results = []
        for suite_name, arguments in TEST_SUITES:
            print('\n--- %s tests ---' % suite_name, flush=True)
            suite_results.append(run_suite(executable, arguments))
        results.append((version,) + tuple(suite_results))

    print('\n=== Summary ===')
    print('{:<8} {:<12} {:<12}'.format('Python', 'ORM', 'Utils'))
    for version, orm_exit, utils_exit in results:
        print(
            '{:<8} {:<12} {:<12}'.format(
                version,
                format_status(orm_exit),
                format_status(utils_exit),
            )
        )

    return int(any(
        exit_code is None or exit_code != 0
        for _, orm_exit, utils_exit in results
        for exit_code in (orm_exit, utils_exit)
    ))


if __name__ == '__main__':
    sys.exit(main())
