"""Find secrets, meeting links and phone numbers in a copy's text (SCR-1, SCR-2).

Each finder returns where in the text to replace, never the value, so nothing here can
quote one. `text.py` puts the placeholders in and runs the last check.

Secrets, in two parts:
  - gitleaks' default rules (`rules/gitleaks.toml`, pinned to RULES_RELEASE), read by our
    code and applied as gitleaks applies them: a rule runs only if the text holds one of its
    keywords, the secret is its `secretGroup` or else its first group with text, and a rule
    with an `entropy` setting skips a secret whose Shannon entropy isn't above it. Only the
    secret is replaced, not the words around it, and the same value anywhere else in the
    copy is replaced too. These become [secret].
  - Our randomness check: a run of 20 or more letters, digits and + / _ = - that looks
    random becomes [secret?], for the producer to check. It skips the ID part of Slack and
    Google links, so those links keep working.

What we leave out of gitleaks, on purpose:
  - Its allowlists, global and per rule. They only stop a match being reported, so
    without them we replace more, never less.
  - Rules that match a file's path. A copy has no path that means anything.
  - Decoding base64 or URL-encoded text before matching.

Rules are matched against the text with invisible characters (a zero-width space, a soft
hyphen...) taken out and wide or styled letters read as plain ones, so a key with a
zero-width space inside is still found. The replacement then covers the whole span in the
real text, invisible characters and all. The last check reads the text the same way.

Go and Python write a few regex features differently. `go_to_python` translates those:
a flag like (?i) partway through applies to the rest of its group in Go, so it becomes a
scoped group, (?i:...); \\z becomes \\Z; and POSIX classes like [[:alnum:]] become plain
ranges. Patterns compile with re.ASCII, so \\w, \\s and \\d mean what they mean in Go.

Meeting links: any link to the services in MEETING_HOSTS becomes [meeting link], with any
passcode in it.

Phone numbers: Google's `phonenumbers` library finds them, read as UK and as US numbers,
and only numbers it finds valid. Digits inside other links aren't read as phone numbers,
so message and doc links keep working. A tel: link is replaced whole.
"""

import hashlib
import math
import re
import tomllib
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import phonenumbers

from diffdata.scrub.chars import HIDDEN, PLAIN_FORMS

RULES_FILE = Path(__file__).parent / "rules" / "gitleaks.toml"
RULES_RELEASE = "gitleaks v8.30.1"
RULES_SHA256 = "e163e53b9e7e8a8511e77271e2b323ed057759542a6d988258afe3a1fa329caf"

SECRET, MAYBE_SECRET, PHONE, MEETING = "[secret]", "[secret?]", "[phone]", "[meeting link]"

# Our randomness check. A run starts and ends where none of its characters touch it.
RUN = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{20,}(?![A-Za-z0-9+/_-])")
HEX = re.compile(r"[0-9A-Fa-f]+")
MIN_RUN = 20
# Shannon entropy, in bits per character. Most random hex strings of 20 or more characters
# are above 3.0, and most random mixes of capitals, small letters and digits above 3.8.
HEX_ENTROPY, MIXED_ENTROPY = 3.0, 3.8
# Joined words, like a file name (MyProjectNotes2024Final), can be as mixed as a key. Most of
# their letters are in runs like "notes" or "Notes"; a random key's mostly aren't. A run with
# at least WORDY of its length in such runs is read as words. So about 1 in 10 random keys of
# 20 characters is missed, and almost none of 24 or more. gitleaks' rules still run on them.
WORDS = re.compile(r"[a-z]{3,}|[A-Z][a-z]{2,}")
WORDY = 0.7
# A password in a link, as in https://user:password@host. gitleaks has no rule for it.
LINK_PASSWORD = re.compile(
    r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,30}://[^\s/:@<>|]+:(?P<secret>[^\s/@<>|]+)@"
)

# Links: with a scheme, or a bare domain followed by a path. A link ends at a space or at
# what wraps it in Slack's markup (<url|label>) or Markdown ([label](url)).
LINK = re.compile(
    r"(?i)(?<![\w.@/-])(?:(?:https?|ftp)://|www\.|[a-z0-9-]+(?:\.[a-z0-9-]+)+/)[^\s<>|\"'`()\[\]]*"
)
HOST = re.compile(r"(?i)^(?:(?:https?|ftp)://)?(?P<host>[^/?#:\s]+)")
# Slack and Google links keep their IDs: a policy on links is for before the first release.
KEEP_ID_HOSTS = ("slack.com", "google.com", "googleusercontent.com", "goo.gl")
MEETING_HOSTS = (
    "zoom.us", "zoomgov.com", "meet.google.com", "teams.live.com", "webex.com",
    "whereby.com", "meet.jit.si", "calendly.com", "gotomeet.me", "gotomeeting.com",
)  # fmt: skip
# Teams shares a host with the rest of Microsoft, so only its meeting paths count.
TEAMS = re.compile(r"(?i)^(?:https?://)?teams\.microsoft\.com/(?:l/meetup-join|meet)/")
TEL = re.compile(r"(?i)(?<![\w-])tel:\+?[0-9 ().\-/]*[0-9]")
REGIONS = ("GB", "US")
# What a masked link becomes before phone numbers are read: no digit, and no letter that
# could read as an extension, like x.
MASK = "|"

POSIX = {
    "alnum": "a-zA-Z0-9", "alpha": "a-zA-Z", "digit": "0-9", "lower": "a-z", "upper": "A-Z",
    "space": r"\t\n\v\f\r ", "xdigit": "0-9A-Fa-f", "word": r"\w", "punct": r"!-/:-@\[-`{-~",
}  # fmt: skip
INLINE_FLAGS = re.compile(r"\(\?([a-zA-Z]+)\)")
# Many rules start with any 0 to 50 word characters, lazily, before their keyword. That only
# moves where a match starts, never where its secret is, but in Python two of them in a row
# are slow on a long word. So a pattern's leading ones are dropped. A test checks the
# secrets found are the same.
LEAD = re.compile(r"\(\?[a-z]+[:)]|\[\\w\.-\]\{0,50\}\?")


class RulesError(Exception):
    """The rule file is missing, changed or broken. The scrub stops before writing."""


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern
    keywords: tuple[str, ...]
    entropy: float  # 0 means no entropy setting
    group: int  # 0 means the first group with text, as gitleaks does


def go_to_python(regex: str) -> str:
    """A gitleaks (Go) pattern in Python's syntax. See the module notes."""
    out: list[str] = []
    scoped: list[list[str]] = [[]]  # for each open group, the flags set partway through it
    i, in_class = 0, False
    while i < len(regex):
        c = regex[i]
        if c == "\\":
            pair = regex[i : i + 2]
            out.append(r"\Z" if pair == r"\z" else pair)
            i += 2
            continue
        if in_class:
            posix = re.match(r"\[:(\w+):\]", regex[i:])
            if posix and posix[1] in POSIX:
                out.append(POSIX[posix[1]])
                i += len(posix[0])
                continue
            if c == "]":
                in_class = False
            out.append(c)
            i += 1
            continue
        if c == "[":
            in_class = True
            out.append(c)
            i += 1
            # A ] or ^] right after the [ is part of the class.
            for lead in ("^]", "]", "^"):
                if regex.startswith(lead, i):
                    out.append(lead)
                    i += len(lead)
                    break
            continue
        flags = INLINE_FLAGS.match(regex, i)
        if flags and out:
            # Go: the flags hold to the end of the group they're in, across any | in it.
            out.append(f"(?{flags[1]}:")
            scoped[-1].append(flags[1])
            i = flags.end()
            continue
        if c == "(":
            scoped.append([])
        elif c == ")":
            out.append(")" * len(scoped.pop()))
        elif c == "|":
            # Close the scoped groups for this branch, and open them again for the next.
            out.append(")" * len(scoped[-1]) + "|" + "".join(f"(?{f}:" for f in scoped[-1]))
            i += 1
            continue
        out.append(c)
        i += 1
    out.append(")" * len(scoped.pop()))
    return "".join(out)


def drop_lead(pattern: str) -> str:
    """The pattern without the lazy word run it starts with, before or inside its opening
    flag groups. See LEAD."""
    kept, at = [], 0
    while lead := LEAD.match(pattern, at):
        if lead[0].startswith("("):
            kept.append(lead[0])
        at = lead.end()
    return "".join(kept) + pattern[at:]


@cache
def rules() -> tuple[Rule, ...]:
    """gitleaks' rules, checked against the pinned release. Raises RulesError."""
    try:
        raw = RULES_FILE.read_bytes()
    except OSError:
        raise RulesError(
            "The secret rules file (diffdata/scrub/rules/gitleaks.toml) is missing, so "
            "nothing was scrubbed. Reinstall diffdata (uv sync) and run again."
        ) from None
    if hashlib.sha256(raw).hexdigest() != RULES_SHA256:
        raise RulesError(
            f"The secret rules file (diffdata/scrub/rules/gitleaks.toml) isn't the pinned "
            f"{RULES_RELEASE} copy, so nothing was scrubbed. Restore it from the repository "
            "and run again."
        )
    try:
        config = tomllib.loads(raw.decode("utf-8"))
        found = []
        for rule in config["rules"]:
            if "path" in rule or "regex" not in rule:
                continue  # a rule on file paths: a copy has none
            found.append(
                Rule(
                    id=rule["id"],
                    pattern=re.compile(drop_lead(go_to_python(rule["regex"])), re.ASCII),
                    keywords=tuple(k.lower() for k in rule.get("keywords", ())),
                    entropy=float(rule.get("entropy", 0)),
                    group=int(rule.get("secretGroup", 0)),
                )
            )
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError):
        raise RulesError(
            "The secret rules file (diffdata/scrub/rules/gitleaks.toml) can't be read, so "
            "nothing was scrubbed. Restore it from the repository and run again."
        ) from None
    except re.error:
        raise RulesError(
            f"A rule in the secret rules file won't compile in Python, so nothing was "
            f"scrubbed. Rule: {rule['id']}."
        ) from None
    return tuple(found)


def entropy(text: str) -> float:
    """Shannon entropy in bits per character, as gitleaks measures it."""
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def view(text: str) -> tuple[str, list[int]]:
    """The text as rules read it: nothing invisible, and wide or styled letters as plain
    ones. And for each of its characters, where it came from in `text`."""
    pieces, where = [], []
    for at, c in enumerate(text):
        if HIDDEN.fullmatch(c):
            continue
        if unicodedata.decomposition(c).startswith(PLAIN_FORMS):
            c = unicodedata.normalize("NFKC", c)
        pieces.append(c)
        where += [at] * len(c)
    return "".join(pieces), where


def secret_span(match: re.Match, group: int) -> tuple[int, int]:
    """Where the secret is in a rule's match: its secretGroup, or else its first group
    with text, or else the whole match, as gitleaks picks it."""
    if group and group <= match.re.groups:
        return match.span(group)
    for g in range(1, match.re.groups + 1):
        if match[g]:
            return match.span(g)
    return match.span()


def on_host(link: str, hosts: tuple[str, ...]) -> bool:
    """Whether a link goes to one of these hosts or a subdomain of one."""
    host = HOST.match(link)
    name = host["host"].lower() if host else ""
    return any(name == h or name.endswith("." + h) for h in hosts)


def is_meeting(link: str) -> bool:
    return bool(TEAMS.match(link)) or on_host(link, MEETING_HOSTS)


def kept_links(text: str) -> list[tuple[int, int]]:
    """Links the randomness check leaves alone: Slack and Google links, whose IDs stay, and
    meeting links, which become [meeting link] whole."""
    return [
        link.span()
        for link in LINK.finditer(text)
        if on_host(link[0], KEEP_ID_HOSTS) or is_meeting(link[0])
    ]


def random_runs(text: str) -> list[tuple[int, int]]:
    """Runs that look random enough to be a secret, outside Slack and Google links."""
    kept = kept_links(text)
    found = []
    for run in RUN.finditer(text):
        start, end = run.span()
        # Strip the symbols at its edges: "--" or "==" around a word isn't part of it.
        while start < end and text[start] in "+/_=-":
            start += 1
        while end > start and text[end - 1] in "+/_=-":
            end -= 1
        if end - start < MIN_RUN or any(s <= start and end <= e for s, e in kept):
            continue
        value = text[start:end]
        if not any(c.isdigit() for c in value):
            continue
        if HEX.fullmatch(value):
            if any(c.isalpha() for c in value) and entropy(value) > HEX_ENTROPY:
                found.append((start, end))
        elif any(c.isupper() for c in value) and any(c.islower() for c in value):
            wordy = sum(len(w) for w in WORDS.findall(value)) / len(value)
            if entropy(value) > MIXED_ENTROPY and wordy < WORDY:
                found.append((start, end))
    return found


def secrets(text: str) -> list[tuple[int, int, str]]:
    """Where the secrets are in `text`: (start, end, placeholder), sorted, not overlapping.
    A span gitleaks' rules find is [secret]; one only the randomness check finds is
    [secret?]."""
    seen, where = view(text)
    low = seen.lower()
    spans: list[tuple[int, int, bool]] = []
    values = set()
    for rule in rules():
        if rule.keywords and not any(k in low for k in rule.keywords):
            continue
        for match in rule.pattern.finditer(seen):
            start, end = secret_span(match, rule.group)
            value = seen[start:end]
            if end <= start or (rule.entropy and entropy(value) <= rule.entropy):
                continue
            spans.append((start, end, True))
            values.add(value)
    for match in LINK_PASSWORD.finditer(seen):
        spans.append((*match.span("secret"), True))
        values.add(match["secret"])
    # A secret found once is replaced everywhere it appears in the copy.
    for value in values:
        if len(value) < 8:
            continue
        at = seen.find(value)
        while at != -1:
            spans.append((at, at + len(value), True))
            at = seen.find(value, at + 1)
    spans += [(start, end, False) for start, end in random_runs(seen)]
    # Back to the real text, covering any invisible characters inside.
    real = [(where[s], where[e - 1] + 1, sure) for s, e, sure in spans]
    return [(s, e, SECRET if sure else MAYBE_SECRET) for s, e, sure in merge(real)]


def merge(spans: list[tuple[int, int, bool]]) -> list[tuple[int, int, bool]]:
    """Overlapping or touching spans as one. It's sure if any part of it is."""
    merged: list[list] = []
    for start, end, sure in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = merged[-1][2] or sure
        else:
            merged.append([start, end, sure])
    return [tuple(span) for span in merged]


def meeting_links(text: str) -> list[tuple[int, int, str]]:
    """Where the meeting links are: the whole link, passcode and all."""
    return [(*link.span(), MEETING) for link in LINK.finditer(text) if is_meeting(link[0])]


def phone_numbers(text: str) -> list[tuple[int, int, str]]:
    """Where the phone numbers are, and tel: links, but not digits inside other links."""
    spans = [(*m.span(), True) for m in TEL.finditer(text)]
    masked = list(text)
    for start, end in [m.span() for m in LINK.finditer(text)] + [s[:2] for s in spans]:
        masked[start:end] = MASK * (end - start)
    masked = "".join(masked)
    for region in REGIONS:
        for number in phonenumbers.PhoneNumberMatcher(masked, region):
            spans.append((number.start, number.end, True))
    return [(s, e, PHONE) for s, e, _ in merge(spans)]


def replace(text: str, spans: list[tuple[int, int, str]]) -> str:
    """The text with each span put back as its placeholder."""
    pieces, done = [], 0
    for start, end, placeholder in spans:
        pieces += [text[done:start], placeholder]
        done = end
    return "".join(pieces) + text[done:]
