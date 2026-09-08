"""Check ROADMAP.md's structure.

Three misfilings reached the document before anyone noticed by reading it
(C4 under B, J before I, A9 under B), and a numeric scramble across four
sections survived a check that only compared section letters. Reading
does not catch this; a script does.

Run from anywhere: python3 tools/check_roadmap.py
"""
import os
import re
import sys

DOC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   '..', 'ROADMAP.md')

SECTION = re.compile(r'^## ([A-Z])\. ', re.M)
TOP = re.compile(r'^## ')
ITEM = re.compile(r'^### (?:✅ |🔬 |🧪 )?([A-Z])(\d+): ')
ANY_ITEM = re.compile(r'^### ')

problems = []


def fail(message):
    problems.append(message)


text = open(DOC).read()
lines = text.split('\n')

# 1. Sections run A, B, C, ... in order.
letters = SECTION.findall(text)
if letters != sorted(letters):
    fail('sections out of order: ' + ' '.join(letters))
if len(letters) != len(set(letters)):
    fail('duplicate section letters: ' + ' '.join(letters))

# 2. Every item sits under its own letter, numbered in order, once each.
current = None
seen = {}
for line in lines:
    m = SECTION.match(line)
    if m:
        current = m.group(1)
        seen[current] = []
        continue
    if TOP.match(line):
        current = None
        continue
    if current is None or not ANY_ITEM.match(line):
        continue
    m = ITEM.match(line)
    if not m:
        fail('heading in section %s is not an item: %s' % (current, line.strip()))
        continue
    letter, number = m.group(1), int(m.group(2))
    if letter != current:
        fail('%s%d is filed under section %s' % (letter, number, current))
    else:
        seen[current].append(number)

for letter, numbers in seen.items():
    if numbers != sorted(numbers):
        fail('section %s is out of numeric order: %s' % (letter, numbers))
    duplicates = set(n for n in numbers if numbers.count(n) > 1)
    if duplicates:
        fail('section %s has duplicate items: %s' % (letter, sorted(duplicates)))

# 3. A status mark must not be restated in prose. Seven headings carried
#    'awaiting hardware test' long after the bench had tested them, because
#    the suffix had to be maintained separately from the mark.
STALE = ('awaiting hardware test', 'hardware-verified', 'not yet tested',
         'awaiting test')
for line in lines:
    if ANY_ITEM.match(line):
        for phrase in STALE:
            if phrase in line.lower():
                fail('heading restates its status in prose (%r): %s'
                     % (phrase, line.strip()))

# 4. An item marked as under test must say what it is waiting on, or the
#    mark decays into "someone flashed this once" and nobody can tell what
#    would finish it.
for i, line in enumerate(lines):
    if line.startswith('### \U0001f9ea '):
        window = '\n'.join(lines[i + 1:i + 6])
        if 'Still unverified:' not in window:
            fail('item under test does not say what it awaits: ' + line.strip())

# 5. No doubled horizontal rules, which reordering tends to leave behind.
if re.search(r'\n---\s*\n\s*---\s*\n', text):
    fail('doubled horizontal rule')

# 6. Every item referenced elsewhere in the document actually exists.
defined = set('%s%d' % (l, n) for l, ns in seen.items() for n in ns)
referenced = set(re.findall(r'`([A-Z]\d+)`', text))
missing = sorted(referenced - defined)
if missing:
    fail('referenced but not defined: ' + ', '.join(missing))

if problems:
    for p in problems:
        print('FAIL ' + p)
    sys.exit(1)

print('ok   %d sections, %d items, all in order' % (len(letters), len(defined)))
