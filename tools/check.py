"""Run every check in the repository.

    python3 tools/check.py

Four things, in increasing order of how long they take:

  1. Every .py file ends with exactly one newline.
  2. Every .py file byte-compiles.
  3. ROADMAP.md is structurally sound (tools/check_roadmap.py).
  4. Every suite in tests/ passes.

Exits non-zero if anything fails, so it works as a pre-commit hook or a CI
step. Test output is captured and shown only for failures, since a passing
run prints several hundred lines nobody reads.
"""
import os
import py_compile
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {'.git', '__pycache__', '.mypy_cache'}

failures = []
notes = []


def report(ok, label, detail=''):
    print('%-5s %s' % ('ok' if ok else 'FAIL', label))
    if not ok:
        failures.append(label)
        if detail:
            notes.append((label, detail))


def python_files():
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(names):
            if name.endswith('.py'):
                yield os.path.join(base, name)


def relative(path):
    return os.path.relpath(path, ROOT)


# --- 1. Trailing newline ---------------------------------------------------
# One newline, no more. Keeps diffs from tagging the last line every time it
# moves, and it is the kind of rule that only holds if something enforces it.
bad = []
for path in python_files():
    data = open(path, 'rb').read()
    if not data:
        bad.append(relative(path) + ' (empty)')
    elif not data.endswith(b'\n'):
        bad.append(relative(path) + ' (no trailing newline)')
    elif data.endswith(b'\n\n'):
        bad.append(relative(path) + ' (more than one)')
report(not bad, 'trailing newlines', '\n'.join(bad))

# --- 2. Byte-compiles ------------------------------------------------------
# Catches a syntax error before it reaches a board, where the only symptom is
# a device that will not start.
bad = []
with tempfile.TemporaryDirectory() as cache:
    for path in python_files():
        target = os.path.join(cache, relative(path).replace(os.sep, '_') + 'c')
        try:
            py_compile.compile(path, cfile=target, doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(str(e).strip())
report(not bad, 'byte-compiles', '\n'.join(bad))

# --- 3. Roadmap structure --------------------------------------------------
checker = os.path.join(ROOT, 'tools', 'check_roadmap.py')
if os.path.exists(checker):
    r = subprocess.run([sys.executable, checker], capture_output=True, text=True)
    report(r.returncode == 0, 'roadmap structure',
           (r.stdout + r.stderr).strip())
else:
    report(False, 'roadmap structure', 'tools/check_roadmap.py is missing')

# --- 4. Test suites --------------------------------------------------------
# Each runs as its own process: the suites install stub modules into
# sys.modules and would contaminate each other in one interpreter.
tests_dir = os.path.join(ROOT, 'tests')
total = 0
suites = sorted(n for n in os.listdir(tests_dir)
                if n.startswith('test_') and n.endswith('.py'))
for name in suites:
    path = os.path.join(tests_dir, name)
    r = subprocess.run([sys.executable, path], capture_output=True, text=True,
                       timeout=300)
    tail = [l for l in r.stdout.strip().split('\n') if 'passed' in l]
    summary = tail[-1] if tail else '(no summary)'
    if r.returncode == 0 and tail:
        total += int(summary.split('/')[0])
    report(r.returncode == 0, 'tests/' + name + '  ' + summary,
           '\n'.join(l for l in r.stdout.split('\n') if l.startswith('FAIL'))
           or r.stderr.strip())

print()
for label, detail in notes:
    print('--- ' + label + ' ---')
    print(detail)
    print()

if failures:
    print('%d check(s) failed' % len(failures))
    sys.exit(1)

print('all checks passed  (%d assertions across %d suites)' % (total, len(suites)))
