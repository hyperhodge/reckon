#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The names check -- item 6 of the approved plan (spec-core.md Item 6).

Nothing leaves this Mac until this passes. It reads a list of clients,
companies and brands that must never appear in shared data, expands every
entry into the forms it turns up in, and looks for every form in everything
headed for the package: every line of every file, every string in every data
file, and every file and folder name.

## The list is not here, and never will be

    ~/Library/Application Support/claude-ledger/never-publish/names.txt

The list is more sensitive than anything it protects, so it lives outside the
repository, locked to one account. The check refuses to run if the file is
missing, readable by another account, or inside the repository. **A missing
list is a refusal, never a pass**: a check with nothing to look for finds
nothing, and "found nothing" is exactly what a pass says.

One name per line, aliases on the same line after `|`. `#` lines are ignored,
and so are `#?` lines, which are candidates nobody has decided on yet.

## What it matches

Every name and alias becomes: as written, lower case, no spaces, hyphenated,
underscored, possessive, web address and email domain -- and `&` and `and` are
each tried for the other. A match is always a whole word: a name inside a
longer word is never one, so a two-letter name never matches inside a word.

**Short names and ordinary words are matched with more care**, because a
whole-word match is not enough for them. A name of four letters or fewer, or
a single ordinary English word, matches only as written -- `MFX`, not the `mfx`
in `mfx = model` or in `rate_mfx`, where an underscore joins it to the word
beside it. It matches in any case only where a lower-case name really lives:
in a file or folder name, beside a `/` in a path, and in a web address or
email domain. Not beside a `-` or a `.`: that is every stylesheet and script.

**Names of six letters or more also catch anything one edit away** -- except
where that near miss is itself an ordinary English word (`glimmertop` is not a
misspelling of a brand called Glimmerton), and never by gluing together words a
space separates (`spend a` is not a misspelling of anything). The same letters
split by punctuation are always caught (`quill-brook`). The dictionary is the
Mac's own; without it, fewer near misses are excused, never more.

## The positive control, and why it runs every time

Before scanning anything, every form of every entry -- and a one-letter
misspelling of every long one -- is planted in synthetic text and scanned with
the same matcher. **If one is missed, the check refuses to run.** A check that
has silently stopped matching looks exactly like a check that found nothing,
and the control is the only thing that tells them apart.

## What the report may say

Counts, the control result, every match with file and line, and the second
layer's capitalised words. **A match prints only the text that matched and the
list line it came from** -- never the other names on that line, because an
alias found in a file must not become a report naming the client behind it.

## A false alarm

Is cleared only by a person, by name, recorded in `decisions/`. The record
carries a keyed fingerprint rather than the matched text, because `decisions/`
is in git and the list must never be. The key lives beside the list.

    python3 store/names_check.py --export           # the redacted export
    python3 store/names_check.py some/folder file   # files headed for the package
    python3 store/names_check.py --clear FINGERPRINT --by NAME --reason TEXT
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import store  # noqa: E402

LIST_PATH = (Path.home() / "Library" / "Application Support" / "claude-ledger"
             / "never-publish" / "names.txt")
WORDS_PATH = Path("/usr/share/dict/words")
KEY_NAME = "names.key"
CLEARANCE_KIND = "NamesCheckFalseAlarmCleared"

KINDS = ("as written", "lower case", "no spaces", "hyphenated", "underscored",
         "possessive", "web address", "email domain")
ANY_CASE_KINDS = ("web address", "email domain")
SHORT = 4                  # letters and digits; at or under this, matched as written
NEAR_MISS_MIN = 6          # letters and digits; shorter names never catch near misses
NEAR_MISS = "one edit away"
SAME_LETTERS = "same letters, other punctuation"
IN_A_PATH = "lower case, in a path or name"
PATH_CHARS = "/"

EXIT_PASS, EXIT_MATCH, EXIT_REFUSED = 0, 1, 2


class Refused(Exception):
    """The check cannot vouch for anything, so it does not run."""


class ControlFailed(Refused):
    """A planted name was not caught."""


# --------------------------------------------------------------------------
# The list, and the dictionary
# --------------------------------------------------------------------------

def load_list(path=LIST_PATH, root=store.ROOT):
    """[(list line number, [name, alias, ...])]. Refuses rather than guesses."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise Refused("no names list at the configured path. A check with "
                      "nothing to look for would pass everything, so it does not run.")
    try:
        path.resolve().relative_to(Path(root).resolve())
    except ValueError:
        pass
    else:
        raise Refused("the names list is inside the repository. It must live "
                      "outside it, locked to one account.")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise Refused("the names list can be read by other accounts on this Mac. "
                      "Lock it to its owner before the check will use it.")
    entries = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names = [part.strip() for part in line.split("|") if part.strip()]
        if names:
            entries.append((number, names))
    if not entries:
        raise Refused("the names list has no active entries, so there is nothing to check.")
    return entries


def load_words(path=WORDS_PATH):
    """Ordinary lower-case English words. Capitalised dictionary entries are
    proper nouns and are left out: a brand that is also a place is not ordinary."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as handle:
            return frozenset(word.strip() for word in handle
                             if word.strip() and word.strip().islower())
    except OSError:
        return frozenset()


SUFFIXES = (("s", ""), ("es", ""), ("d", ""), ("ed", ""), ("ing", ""), ("ing", "e"),
            ("r", ""), ("er", ""), ("rs", ""), ("ers", ""), ("ly", ""))


def is_ordinary(word, words):
    """An English word, allowing the endings and two-word compounds an old
    dictionary leaves out: observed, discovers, reusers, baseline."""
    if not words:
        return False
    if word in words:
        return True
    for suffix, restore in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if word[:-len(suffix)] + restore in words:
                return True
    return any(word[:i] in words and word[i:] in words for i in range(4, len(word) - 3))


def _tokens(text):
    return [token.lower() for token in re.findall(r"[0-9A-Za-z]+", text)]


def _spaces(text):
    return " ".join(text.lower().split())


def is_strict(name, words):
    """Matched only as written: short, or a single ordinary word."""
    tokens = _tokens(name)
    joined = "".join(tokens)
    return len(joined) <= SHORT or (len(tokens) == 1 and joined in words)


def expand(name, strict=False):
    """{form: kind} for one name or alias.

    A strict name keeps its own capitals in the forms matched as written, and
    has no lower-case, joined or hyphenated forms: those are what turn `MFX`
    into every `mfx` in a program."""
    variants = {name.strip()}
    if "&" in name:
        variants.add(re.sub(r"\s*&\s*", " and ", name))
    if re.search(r"\band\b", name, re.IGNORECASE):
        variants.add(re.sub(r"\s+and\s+", " & ", name, flags=re.IGNORECASE))
    forms = {}

    def add(kind, form):
        if form:
            forms.setdefault(form, kind)

    for variant in sorted(variants):
        tokens = _tokens(variant)
        if not tokens:
            continue
        joined = "".join(tokens)
        if strict:
            written = " ".join(variant.split())
            add("as written", written)
            add("possessive", written + "'s")
        else:
            written = _spaces(variant)
            add("as written", written)
            add("lower case", " ".join(tokens))
            add("no spaces", written.replace(" ", ""))
            add("no spaces", joined)
            add("hyphenated", "-".join(tokens))
            add("underscored", "_".join(tokens))
            add("possessive", written + "'s")
            add("possessive", " ".join(tokens) + "'s")
        add("web address", f"www.{joined}.com")
        add("web address", f"{joined}.co.uk")
        add("web address", "-".join(tokens) + ".com")
        add("email domain", f"@{joined}.com")
    return forms


def matcher_forms(entries, words=frozenset()):
    """{form: {(list line, name, kind)}} -- what the matcher is built from.

    Separate from expand() on purpose: the control plants what expand() says a
    name looks like and scans with what this returns, so a matcher that has
    lost a form is caught by the control rather than trusted.
    """
    forms = {}
    for number, names in entries:
        for name in names:
            for form, kind in expand(name, is_strict(name, words)).items():
                forms.setdefault(form, set()).add((number, name, kind))
    return forms


def _trie_pattern(words):
    """One regex over many literals, as a trie: a flat alternation of ~2,000
    forms is tried in full at every word start and is far too slow."""
    trie = {}
    for word in words:
        node = trie
        for char in word:
            node = node.setdefault(char, {})
        node[""] = {}

    def build(node):
        branches = [re.escape(char) + build(child)
                    for char, child in sorted(node.items()) if char != ""]
        if not branches:
            return ""
        body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
        return f"(?:{body})?" if "" in node else body

    return build(trie)


def _compile(forms, outside):
    if not forms:
        return None
    return re.compile(f"(?<!{outside})(?:" + _trie_pattern(forms) + f")(?!{outside})")


def _deletions(word):
    return {word} | {word[:i] + word[i + 1:] for i in range(len(word))}


def within_one_edit(a, b):
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    if len(a) == len(b):
        return a[i + 1:] == b[i + 1:]
    return a[i:] == b[i + 1:]


class Matcher:
    def __init__(self, entries, words=frozenset()):
        self.entries = entries
        self.words = words
        self.forms = matcher_forms(entries, words)
        strict = {name for _, names in entries for name in names if is_strict(name, words)}
        any_case, as_written = [], []
        self.loose = {}                      # lower-cased strict form -> sources
        for form, sources in self.forms.items():
            if any(name in strict and kind not in ANY_CASE_KINDS for _, name, kind in sources):
                as_written.append(form)
                self.loose.setdefault(form.lower(), set()).update(sources)
            else:
                any_case.append(form)
        self.any_case = _compile(any_case, "[0-9a-z]")
        self.as_written = _compile(as_written, "[0-9A-Za-z_]")
        self.in_a_path = _compile(self.loose, "[0-9a-z_]")

        self.long = {}                       # joined letters -> {(list line, name)}
        self.long_tokens = {}                # joined letters -> most words in its name
        for number, names in entries:
            for name in names:
                tokens = _tokens(name)
                joined = "".join(tokens)
                if len(joined) >= NEAR_MISS_MIN:
                    self.long.setdefault(joined, set()).add((number, name))
                    self.long_tokens[joined] = max(len(tokens), self.long_tokens.get(joined, 0))
        self.index = {}
        for joined in self.long:
            for deletion in _deletions(joined):
                self.index.setdefault(deletion, set()).add(joined)
        self.window = 1 + max(self.long_tokens.values(), default=0)
        self.longest = 1 + max((len(joined) for joined in self.long), default=0)
        self._ordinary = {}

    def ordinary(self, word):
        if word not in self._ordinary:
            self._ordinary[word] = is_ordinary(word, self.words)
        return self._ordinary[word]

    def scan_line(self, line, name=False):
        """[(list line, name, kind, matched text)], one per entry per line.
        `name` is True for a file or folder name, where lower case is normal."""
        original = " ".join(line.split())
        text = original.lower()
        hits, seen = [], set()

        def take(sources, found, kind=None):
            for number, entry, own_kind in sorted(sources):
                if number not in seen:
                    seen.add(number)
                    hits.append((number, entry, kind or own_kind, found))

        if self.any_case:
            for found in self.any_case.finditer(text):
                take(self.forms[found.group(0)], found.group(0))
        if self.as_written:
            for found in self.as_written.finditer(original):
                take(self.forms[found.group(0)], found.group(0))
        if self.in_a_path:
            for found in self.in_a_path.finditer(text):
                start, end = found.span()
                if (name or (start and text[start - 1] in PATH_CHARS)
                        or (end < len(text) and text[end] in PATH_CHARS)):
                    take(self.loose[found.group(0)], found.group(0), IN_A_PATH)
        if self.long:
            self._near_misses(text, take)
        return hits

    def _near_misses(self, text, take):
        spans = list(re.finditer(r"[0-9a-z]+", text))
        for start in range(len(spans)):
            joined, gaps = "", 0
            for end in range(start, min(start + self.window, len(spans))):
                if end > start and " " in text[spans[end - 1].end():spans[end].start()]:
                    gaps += 1
                joined += spans[end].group(0)
                if len(joined) > self.longest:
                    break
                if len(joined) < NEAR_MISS_MIN - 1:
                    continue
                candidates = set()
                for deletion in _deletions(joined):
                    candidates |= self.index.get(deletion, set())
                for target in sorted(candidates):
                    if not within_one_edit(joined, target):
                        continue
                    if joined == target and end == start:
                        continue             # one plain word: the patterns own that
                    if joined != target:
                        if gaps + 1 > self.long_tokens[target]:
                            continue         # words a space separates, glued together
                        window = [span.group(0) for span in spans[start:end + 1]]
                        if all(self.ordinary(word) for word in window):
                            continue         # ordinary English, not a misspelling
                    kind = SAME_LETTERS if joined == target else NEAR_MISS
                    take({(number, entry, kind) for number, entry in self.long[target]},
                         text[spans[start].start():spans[end].end()])


# --------------------------------------------------------------------------
# The positive control
# --------------------------------------------------------------------------

def _misspell(name):
    """A one-letter substitution in the middle of a long name."""
    letters = [i for i, char in enumerate(name) if char.isalnum()]
    i = letters[len(letters) // 2]
    swap = "q" if name[i].lower() != "q" else "x"
    return name[:i] + swap + name[i + 1:]


def positive_control(entries, matcher):
    """Plant every form of every entry, a misspelling of every long one, and
    every strict name in lower case in a folder name.

    Returns counts. Raises ControlFailed naming the list line and the kind of
    form missed -- never the name, because the refusal is printed.
    """
    planted = caught = near = 0
    missed = []
    for number, names in entries:
        for name in names:
            strict = is_strict(name, matcher.words)
            plants = [(kind, form, False)
                      for form, kind in sorted(expand(name, strict).items())]
            if strict:
                plants.append((IN_A_PATH, f"clients/{_spaces(name)}-notes/", False))
                plants.append((IN_A_PATH, _spaces(name), True))
            if len("".join(_tokens(name))) >= NEAR_MISS_MIN:
                plants.append((NEAR_MISS, _misspell(name), False))
            for kind, form, as_name in plants:
                planted += 1
                text = form if as_name else (
                    f"Synthetic text planted by the control: {form} and nothing else.")
                if any(hit[0] == number for hit in matcher.scan_line(text, name=as_name)):
                    caught += 1
                    near += kind == NEAR_MISS
                else:
                    missed.append((number, kind))
    if missed:
        detail = "; ".join(f"list line {n}, {kind}" for n, kind in missed[:10])
        raise ControlFailed(f"the positive control missed {len(missed)} of {planted} "
                            f"planted names ({detail}). The check does not run.")
    return {"planted": planted, "caught": caught, "near_misses": near}


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------

def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class Scan:
    CAPITALISED = re.compile(r"\b[A-Z][a-z]+(?:[ ][A-Z][a-z]+)*\b")

    def __init__(self, matcher):
        self.matcher = matcher
        self.matches = []            # (where, list line, name, kind, text)
        self.files = self.lines = self.strings = self.names = 0
        self.unreadable = []
        self.capitalised = {}        # phrase -> first place seen
        self.lower_words = set()

    def _record(self, where, line, name=False):
        for number, entry, kind, text in self.matcher.scan_line(line, name=name):
            self.matches.append((where, number, entry, kind, text))
        for word in re.findall(r"\b[a-z]+\b", line):
            self.lower_words.add(word)
        for phrase in self.CAPITALISED.findall(line):
            self.capitalised.setdefault(phrase, where)

    def text(self, label, text, data=None):
        """One file's worth of text. `data` is parsed JSON when there is some."""
        self.files += 1
        for number, line in enumerate(text.splitlines(), 1):
            self.lines += 1
            self._record(f"{label}:{number}", line)
        values = []
        if data is not None:
            values = [(label, data)]
        elif label.endswith(".jsonl"):
            for number, line in enumerate(text.splitlines(), 1):
                try:
                    values.append((f"{label}:{number}", json.loads(line)))
                except ValueError:
                    continue
        elif label.endswith(".json"):
            try:
                values = [(label, json.loads(text))]
            except ValueError:
                values = []
        known = {(m[0].rsplit(":", 1)[0], m[1]) for m in self.matches}
        for where, value in values:
            for string in _strings(value):
                self.strings += 1
                for number, entry, kind, found in self.matcher.scan_line(string):
                    if (where.rsplit(":", 1)[0], number) not in known:
                        self.matches.append((f"{where} (a string value)",
                                             number, entry, kind, found))

    def name(self, label, name):
        self.names += 1
        self._record(f"{label} (file or folder name)", name, name=True)

    def path(self, target):
        target = Path(target)
        base = target.parent
        items = [target] if target.is_file() else [target] + sorted(target.rglob("*"))
        for item in items:
            label = str(item.relative_to(base))
            self.name(label, item.name)
            if not item.is_file() or item.is_symlink():
                continue
            try:
                self.text(label, item.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, OSError):
                self.unreadable.append(label)

    def candidates(self, stoplist=frozenset({"I", "A", "The"})):
        """The capital-letter layer: phrases with a word never seen in lower case."""
        out = []
        for phrase, where in sorted(self.capitalised.items()):
            words = phrase.split()
            if all(word in stoplist or word.lower() in self.lower_words for word in words):
                continue
            out.append((phrase, where))
        return out


# --------------------------------------------------------------------------
# Fingerprints and clearances
# --------------------------------------------------------------------------

def _key(list_path):
    path = Path(list_path).expanduser().parent / KEY_NAME
    if not path.exists():
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(os.urandom(32).hex())
    return bytes.fromhex(path.read_text().strip())


def fingerprint(key, where, name, text):
    """Keyed, so a fingerprint in git cannot be checked against a dictionary."""
    place = where.split(" (")[0].rsplit(":", 1)[0]
    message = "\0".join((place, name.lower(), text)).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:16]


def cleared_fingerprints(root):
    directory = Path(root) / "decisions"
    if not directory.is_dir():
        return {}
    return {record.get("fingerprint"): record
            for record in store.read_decisions(directory)
            if record.get("kind") == CLEARANCE_KIND}


def clear(list_path, root, fp, by, reason, entries=None):
    """Record a false alarm cleared by a named person. Refuses a reason that
    itself names something on the list, because decisions/ is in git."""
    if not re.fullmatch(r"[0-9a-f]{16}", fp or ""):
        raise Refused("a fingerprint is the sixteen characters printed beside a match.")
    if not (by or "").strip() or not (reason or "").strip():
        raise Refused("a false alarm is cleared by a named person, with a reason.")
    entries = entries or load_list(list_path, root)
    if Matcher(entries).scan_line(reason, name=True):
        raise Refused("the reason names something on the list, and decisions/ is in "
                      "git. Say why it is a false alarm without naming it.")
    record = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "kind": CLEARANCE_KIND, "fingerprint": fp,
              "by": by.strip(), "reason": reason.strip()}
    store.append_decisions([record], Path(root) / "decisions")
    return record


# --------------------------------------------------------------------------
# The check, and its report
# --------------------------------------------------------------------------

def run_check(targets=(), export_text=None, list_path=LIST_PATH, root=store.ROOT,
              words=None):
    """The whole check. Raises Refused; otherwise returns a result whose
    `passed` is the only thing a package builder should read."""
    entries = load_list(list_path, root)
    matcher = Matcher(entries, load_words() if words is None else words)
    control = positive_control(entries, matcher)
    scan = Scan(matcher)
    if export_text is not None:
        scan.text("redacted-export.json", export_text)
    for target in targets:
        if not Path(target).exists():
            raise Refused(f"nothing to scan at {target}.")
        scan.path(target)
    key = _key(list_path)
    cleared = cleared_fingerprints(root)
    matches, cleared_here = [], []
    for where, number, name, kind, text in scan.matches:
        fp = fingerprint(key, where, name, text)
        (cleared_here if fp in cleared else matches).append(
            {"where": where, "list_line": number, "kind": kind, "text": text,
             "fingerprint": fp})
    return {
        "entries": len(entries),
        "names": sum(len(names) for _, names in entries),
        "forms": len(matcher.forms),
        "dictionary": len(matcher.words),
        "control": control,
        "files": scan.files, "lines": scan.lines, "strings": scan.strings,
        "names_scanned": scan.names,
        "unreadable": scan.unreadable,
        "matches": matches,
        "cleared": cleared_here,
        "candidates": scan.candidates(),
        "passed": not matches,
    }


def format_report(result, scanned):
    at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    control = result["control"]
    lines = [
        f"NAMES CHECK  {at}",
        f"scanned      {scanned}",
        "",
        f"list         {result['entries']} entries, {result['names']} names and aliases, "
        f"{result['forms']} forms. The list itself is not printed.",
        f"control      PASSED. {control['caught']} of {control['planted']} names planted in "
        f"synthetic text were caught, {control['near_misses']} of them one-letter misspellings.",
        f"read         {result['files']} file(s), {result['lines']} line(s), "
        f"{result['strings']} string value(s), {result['names_scanned']} file and folder name(s)",
    ]
    if not result["dictionary"]:
        lines.append("dictionary   NOT FOUND. No near miss is excused as an ordinary word, "
                     "so expect more false alarms, never fewer catches.")
    if result["unreadable"]:
        lines.append(f"NOT READ     {len(result['unreadable'])} file(s) are not text, and a "
                     f"person must look at them:")
        lines += [f"             {label}" for label in result["unreadable"]]
    lines.append("")
    if result["matches"]:
        lines.append(f"MATCHES      {len(result['matches'])}. A match stops the package.")
        for match in result["matches"]:
            lines.append(f"  {match['where']}  matched '{match['text']}'  "
                         f"({match['kind']}, list line {match['list_line']})  "
                         f"fingerprint {match['fingerprint']}")
    else:
        lines.append("matches      none.")
    if result["cleared"]:
        lines.append(f"cleared      {len(result['cleared'])} false alarm(s), each cleared by "
                     f"name in decisions/.")
    lines.append("")
    lines.append(f"second layer {len(result['candidates'])} capitalised word(s) never seen "
                 f"in lower case. Not failures -- for a person to judge:")
    lines += [f"  {phrase}  first at {where}" for phrase, where in result["candidates"]]
    lines += ["", "RESULT       PASS" if result["passed"] else
              "RESULT       STOPPED. Nothing is shared until every match is removed "
              "or cleared by name."]
    return lines


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="The names check (spec-core.md Item 6).")
    parser.add_argument("targets", nargs="*", help="files or folders headed for the package")
    parser.add_argument("--export", action="store_true",
                        help="scan the redacted export (governance.py --present), in memory")
    parser.add_argument("--list", default=str(LIST_PATH))
    parser.add_argument("--root", default=str(store.ROOT))
    parser.add_argument("--report", help="also write the report to this file")
    parser.add_argument("--clear", metavar="FINGERPRINT")
    parser.add_argument("--by")
    parser.add_argument("--reason", help="why it is a false alarm, without naming it")
    return parser.parse_args(argv)


def main(argv=None, out=None):
    args = parse_args(argv)
    out = out or sys.stdout

    def say(line=""):
        print(line, file=out)

    try:
        if args.clear:
            record = clear(args.list, args.root, args.clear, args.by, args.reason)
            say(f"cleared  {record['fingerprint']}  by {record['by']}  "
                f"-> decisions/{record['at'][:7]}.jsonl")
            return EXIT_PASS
        if not args.export and not args.targets:
            raise Refused("nothing to scan: name files or folders, or --export.")
        export_text, scanned = None, []
        if args.export:
            import governance
            root = Path(args.root)
            loaded = store.load_store(root)
            register = store.RefRegister.load(root / "store" / "refs.json")
            payload = store.export(loaded, register)
            presented = governance.present(payload)
            audit = governance.audit_presentation(payload, presented)
            if not audit["clean"]:
                raise Refused(f"the redacted export fails its own audit: "
                              f"{len(audit['leaked'])} value(s) survive redaction. "
                              f"Fix the redaction before checking it for names.")
            export_text = json.dumps(presented, indent=2, sort_keys=True)
            scanned.append("the redacted export")
        scanned += [str(target) for target in args.targets]
        result = run_check(args.targets, export_text, args.list, args.root)
    except Refused as refused:
        say(f"REFUSED  {refused}")
        say("RESULT   NOT RUN. A check that did not run is not a pass.")
        return EXIT_REFUSED
    lines = format_report(result, ", ".join(scanned))
    for line in lines:
        say(line)
    if args.report:
        descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return EXIT_PASS if result["passed"] else EXIT_MATCH


if __name__ == "__main__":
    sys.exit(main())
