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
  - Our randomness check (`looks_random`): a run of 20 or more letters, digits and
    + / _ - that looks random becomes [secret?], for the producer to check: hex above
    HEX_ENTROPY; a mix of cases and digits above MIXED_ENTROPY unless it reads as joined
    words; and mixed-case letters alone, or one case with digits, at 24 or more characters
    on the same terms (see ONE_CASE_MIN). It skips the IDs in Google links, so those links
    keep working, and meeting links. Inside other links it checks each part between
    slashes on its own, so a site's name stays readable.
  - Three small rules of our own, for what gitleaks lets through: a password in a link
    (user:password@host), a credential-named value in a link's query or after curl's -u,
    and a password someone chose, after its name (PASSWORD_VALUE: password=..., "the db
    password: ...", "my password is ..."), however plain it is. These become [secret].

What we leave out of gitleaks, on purpose:
  - Its allowlists, global and per rule. They only stop a match being reported, so
    without them we replace more, never less.
  - Its path checks. A copy has no path that means anything, so a rule written for one
    kind of file (a NuGet config, a Kubernetes secret...) runs on every copy, since text
    from one can be pasted into a message. The one rule on a file's name only is skipped.
  - Decoding base64 or URL-encoded text before matching.

Rules are matched against the text with invisible characters (a zero-width space, a soft
hyphen...) taken out and wide or styled letters read as plain ones, so a key with a
zero-width space inside is still found. The replacement then covers the whole span in the
real text, invisible characters and all. The last check reads the text the same way.

Go and Python write a few regex features differently. `go_to_python` translates those:
a flag like (?i) partway through applies to the rest of its group in Go, so it becomes a
scoped group, (?i:...); \\z becomes \\Z; and POSIX classes like [[:alnum:]] become plain
ranges. Patterns compile with re.ASCII, so \\w, \\s and \\d mean what they mean in Go.
Python's engine is slower than Go's on some patterns, so two changes keep every rule quick
on a long line. Both drop only the start of a pattern, which says where a match begins,
never where its secret is: a rule's leading lazy word run (see LEAD), and the two curl
rules' "curl, then anything" start (see CURL_START), which lets them find their -H or -u
option anywhere in a copy that holds "curl".

Meeting links: any link to the services in MEETING_HOSTS becomes [meeting link], with any
passcode in it, read on past brackets in its query to a space or its markup.

Phone numbers: Google's `phonenumbers` library finds them, read as UK and as US numbers,
and only numbers it finds valid. Digits inside other links aren't read as phone numbers,
so message and doc links keep working, and neither are digits inside an email, which the
email step replaces. A tel: link is replaced whole, extension and all.
"""

import bisect
import hashlib
import math
import re
import sys
import tomllib
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

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
# A run in one case (small letters and digits, or capitals and digits, like a base32 seed)
# counts only at 24 or more characters with no _ - / or +: in one case, names are written
# with those (snake_case, kebab-case, a path). Its word-like runs must also have a vowel in
# at least 3 of 10 letters, as words do, since random letters in one case form long runs.
# A UUID has hyphens, so it's left alone: it's usually an ID, and a key shaped like one
# with a keyword nearby is still found by gitleaks' rules.
ONE_CASE_MIN, ONE_CASE_WORDS, VOWELS = 24, re.compile(r"[a-z]{3,}|[A-Z]{3,}"), 0.3
SEPARATORS = re.compile(r"[_/+-]")
# A password in a link, as in https://user:password@host. gitleaks has no rule for it.
LINK_PASSWORD = re.compile(
    r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,30}://[^\s/:@<>|]+:(?P<secret>[^\s/@<>|]+)@"
)
# The end of a link's user:[secret] part, so the @ after it isn't read as a mention.
USERINFO_END = re.compile(r"(?i)[a-z][a-z0-9+.-]{0,30}://(?:[^\s/:@<>|]+:)?\[secret\]\Z")

# Links: with a scheme, wherever it starts (after _ or * in Markdown italics, say), or a bare
# domain followed by a path, which can't start inside a word. A bare domain's last part starts
# with a letter, so a phone number written with dots, like 201.555.0123/4, isn't a link. A
# link ends at a space or at what wraps it in Slack's markup (<url|label>) or Markdown
# ([label](url)).
LINK = re.compile(
    r"(?i)(?:(?:https?|ftp)://|(?<![\w.@/-])(?:www\.|[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z][a-z0-9-]*\.?/))"
    r"[^\s<>|\"'`()\[\]]*"
)
# Google links keep their document, file, folder and form IDs, so they still work: a policy
# on links is for before the first release. Slack's IDs are too short, or digits only, for
# the randomness check to take, so a Slack link needs no exception.
GOOGLE_HOSTS = ("google.com", "googleusercontent.com")
GOOGLE_QUERY_ID = re.compile(r"(?:^|&)id=(?P<id>[A-Za-z0-9_-]{20,})(?=&|$)")
# In the path: /d/ and /e/ (Docs), /folders/ (Drive) and /drive/ (a Colab notebook), up to
# the next part of the path, the query or the fragment.
GOOGLE_ID = re.compile(r"/(?:d|e|folders|drive)/(?P<id>[A-Za-z0-9_-]{20,})(?=[/?#]|$)")
# A meeting link goes on past [ ] ( and ) in its query, up to a space or the markup around
# it; inside Markdown's (link), up to the first ).
# Phone numbers aren't read inside an email: the email step takes it whole.
EMAILISH = re.compile(  # starts where a run starts, so it reads a long run once
    r"(?<![\w.+%-])[\w.+%-]+@(?:[\w-]+(?:\.[\w-]+)+|\[(?:IPv6:)?[0-9A-Fa-f.:]+\])"
)
# A rule's secret that's only a placeholder, perhaps in quotes: already scrubbed.
PLACEHOLDER_ONLY = re.compile(r"[\"'`]*\[(?:secret\??|phone|email|meeting link|name\??)\][\"'`]*")
# A value one of our own rules finds is replaced everywhere in the copy only above this.
OURS_SPREAD_ENTROPY = 3.0
# Two small rules of our own, on top of gitleaks', for credentials it lets through when a
# shell or a link carries on right after them: a credential-named value in a link's query
# (?password=...&next=..., ?db_password=...), and curl's -u user:password followed by ; & or |.
QUERY_SECRET = re.compile(
    r"(?i)[?&](?:(?:[a-z0-9]{0,30}[_.-]){0,4}(?:password|passwd|pwd|passcode)|pass|token|"
    r"access_token|refresh_token|id_token|"
    r"api_?key|key|secret|client_secret|auth|session_?id|sid|sig|signature)="
    r"(?P<secret>[^&#\s<>|\"'`()\[\]]+)"
)
CURL_USER = re.compile(
    r"(?<!\S)(?:-u|--user)(?:=|[ \t]{1,5})['\"]?"
    r"(?P<secret>[^\s;&|:'\"]+:[^\s;&|'\"]+)"
)
# A third rule of our own: a password someone chose, after its name. gitleaks' generic rule
# skips a value that doesn't look random, so password=hunter2hunter2 got through it, and so
# did some random ones. This takes the value however plain it is: after password, passwd, pwd
# or passcode (a prefix joined by _ . or -, like DB_PASSWORD, is part of the name), then =, :
# or :=; or in "my/our/your/the [word] password is ...". Markdown's * _ ~ around the name or
# "is" are read past (_Password:_ ..., my password _is_ ...), but not the _ in is_valid.
# Only the value goes. A name inside a longer word (passwords, password_field, password
# issue), after a word and _ or - (the "password" in db_password on its own) or in a
# link's query (QUERY_SECRET's, ?_password= too) isn't read. A command-line flag is:
# --password=..., -pwd ..., --db-password=...
# A value in quotes goes up to its closing quote, read past \" inside; one without, up to a
# space or Slack's markup. With nothing after the name on its line, a next line holding one
# word only is the value (password:\n  hunter2).
PASSWORD_WORD = r"(?:password|passwd|pwd|passcode)"
PASSWORD_VALUE = re.compile(
    r"(?i)(?:"
    r"(?:(?<![A-Za-z0-9?&./_-])|(?<![A-Za-z0-9?&./-]_)(?<=_)|(?<![^\s-]-)(?<=-))"
    r"(?:[a-z0-9]{1,30}[_.-]){0,4}"
    + PASSWORD_WORD
    + r"(?![A-Za-z0-9-])[\"'`*_~]{0,3}[ \t]*(?P<sep>:=|=|:)[*_~]{0,3}"
    r"|(?<![A-Za-z0-9])(?:my|our|your|the)[ \t]+(?:[\w-]{1,20}[ \t]+)?"
    + PASSWORD_WORD
    + r"[*_~]{0,3}[ \t]+[*_~]{0,3}is(?![A-Za-z0-9-])(?![*_~]+[A-Za-z0-9])"
    r"[*_~]{0,3}(?:[ \t]*:)?[*_~]{0,3}"
    r")(?:[ \t]*(?:\"(?P<dq>(?:[^\"\\\n]|\\.)+)\"|'(?P<sq>(?:[^'\\\n]|\\.)+)'"
    r"|`(?P<bq>[^`\n]+)`|(?P<bare>[^\s<>|]+))"
    r"|[ \t]*\r?\n[ \t]*(?P<line>[^\s<>|]+)[ \t]*(?=\r?\n|\Z))"
)
# Left off the end of a value without quotes: the end of a sentence or a bracket around it.
PASSWORD_TRAIL = ".,;:)]}\"'`"
# A value that starts with a placeholder (a person's too: a mention replaced later is still
# one in the last check), a link or a Markdown link isn't a password, so a scrubbed copy
# passes: "password: [Person 3] knows it", "password: \"[meeting link] (weekly call)\"". A
# placeholder counts if a space or the value's end follows it; read past the value's end, as
# a value without quotes stops at the space in [Person 3].
PLACEHOLDER_AT = re.compile(
    r"\[(?:secret\??|phone|email|meeting link|name\??|Person [1-9][0-9]*)\]"
)
LINK_AT = re.compile(r"(?i)https?://|www\.|\[[^\]\n]*\]\(")
# A value that's a word of plain prose, not a password: "password: see vault", "the password
# is in 1Password", "password: same as before". Only for a value without quotes after : or
# "is": after = it's code, so password=wrong goes. A real password that's one of these stays.
PROSE = frozenset(
    "see ask check in on at by for from to via the a an is are was will be same as stored "
    "saved sent shared kept use using required optional none null nil n/a na tbd tba unknown "
    "empty blank not no yes wrong incorrect correct invalid expired changed reset too still "
    "case also below above here there dm please it its it's this that now set hidden redacted "
    "removed needed missing ok okay fine weak strong long short protected enabled disabled "
    "encrypted hashed".split()
)
MEETING_HOSTS = (
    "zoom.us", "zoomgov.com", "meet.google.com", "teams.live.com", "webex.com",
    "whereby.com", "meet.jit.si", "calendly.com", "gotomeet.me", "gotomeeting.com",
)  # fmt: skip
# Teams shares a host with the rest of Microsoft, so only its meeting paths count.
TEAMS_PATH = re.compile(r"(?i)/(?:l/meetup-join|meet)/")
TEL = re.compile(r"(?i)(?<![\w-])tel:\+?[0-9 ().\-/]*[0-9]\)*(?:;[^\s<>|\"'\])]*)?")
REGIONS = ("GB", "US")
# What a masked link becomes before phone numbers are read: no digit, and no letter that
# could read as an extension, like x.
MASK = "|"

POSIX = {
    "alnum": "a-zA-Z0-9", "alpha": "a-zA-Z", "digit": "0-9", "lower": "a-z", "upper": "A-Z",
    "space": r"\t\n\v\f\r ", "xdigit": "0-9A-Fa-f", "word": r"\w", "punct": r"!-/:-@\[-`{-~",
}  # fmt: skip
INLINE_FLAGS = re.compile(r"\(\?([a-zA-Z]+)\)")
# The two curl rules start with "curl", then any text, over up to 5 more lines, before the
# -H or -u option they look for. Python's engine reads that text again from every "curl",
# so it's slow, and capping it would miss a long command. That start only says where a
# match begins, never where its secret is, so it's dropped: the rules then find the option
# wherever it is in a copy that holds "curl" (their keyword). That replaces more, never less.
CURL_START = re.compile(r"^\\bcurl\\b\(\?:\.\*\??\|\.\*\??\(\?:\[\\r\\n\]\{1,2\}\.\*\??\)\{1,5\}\)")
# The Kubernetes rule has two runs of spaces in a row, \s+ then [ \t]*, so on a long run of
# spaces Python tries every split between them, for time in the square of the run. Reading
# the first run possessively (\s++, Python 3.11+) can't change what matches, since the second
# can be empty. Only that rule has this pair.
K8S_SPACE = (r"\s+)?[ \t]*", r"\s++)?[ \t]*")
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


def rules() -> tuple[Rule, ...]:
    """gitleaks' rules, read and checked against the pinned release on every call, so a
    file changed or removed while diffdata runs is still refused. Raises RulesError."""
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
    return compiled(raw)


@cache
def compiled(raw: bytes) -> tuple[Rule, ...]:
    """The rules in a checked rule file, compiled once."""
    try:
        config = tomllib.loads(raw.decode("utf-8"))
        found = []
        for rule in config["rules"]:
            if "regex" not in rule:
                continue  # a rule on a file's name only: a copy has none
            # A rule for one kind of file (its "path") runs on every copy: it can only
            # replace more.
            regex = CURL_START.sub("", rule["regex"])
            pattern = drop_lead(go_to_python(regex)).replace(*K8S_SPACE)
            found.append(
                Rule(
                    id=rule["id"],
                    pattern=re.compile(pattern, re.ASCII),
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


def vowel_share(word: str) -> float:
    return sum(c in "aeiouyAEIOUY" for c in word) / len(word)


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


def split_link(link: str):
    """A link's parts, read by urllib, with or without its scheme. None if it can't be read."""
    try:
        return urlsplit(link if "://" in link[:40] else "//" + link)
    except ValueError:
        return None


def host_of(link: str) -> str:
    """A link's host name: small letters, without a user, password, port or trailing dot."""
    parts = split_link(link)
    try:
        return (parts.hostname or "").rstrip(".") if parts else ""
    except ValueError:
        return ""


def on_host(link: str, hosts: tuple[str, ...]) -> bool:
    """Whether a link goes to one of these hosts or a subdomain of one."""
    name = host_of(link)
    return any(name == h or name.endswith("." + h) for h in hosts)


def is_meeting(link: str) -> bool:
    if host_of(link) == "teams.microsoft.com":
        parts = split_link(link)
        return bool(parts and TEAMS_PATH.match(parts.path))
    return on_host(link, MEETING_HOSTS)


def query_start(link: str) -> int:
    """Where a link's query or fragment starts, or its length if it has neither."""
    found = re.search(r"[?#]", link)
    return found.start() if found else len(link)


def kept_links(text: str) -> list[tuple[int, int]]:
    """What the randomness check leaves alone, sorted: the IDs in a Google link's path (or
    its id= value), so they still work, and meeting links, which become [meeting link]
    whole."""
    spans = []
    for link in LINK.finditer(text):
        if is_meeting(link[0]):
            spans.append(link.span())
        elif on_host(link[0], GOOGLE_HOSTS):
            cut = query_start(link[0])
            for found in GOOGLE_ID.finditer(link[0]):
                spans.append((link.start() + found.start("id"), link.start() + found.end("id")))
            for found in GOOGLE_QUERY_ID.finditer(link[0][cut + 1 :]):
                at = link.start() + cut + 1
                spans.append((at + found.start("id"), at + found.end("id")))
    return sorted(spans)


def looks_random(value: str) -> bool:
    """Whether a run of 20 or more characters looks random enough to be a secret."""
    digits = any(c.isdigit() for c in value)
    upper, lower = any(c.isupper() for c in value), any(c.islower() for c in value)
    if digits and HEX.fullmatch(value):
        return any(c.isalpha() for c in value) and entropy(value) > HEX_ENTROPY
    if upper and lower and (digits or len(value) >= ONE_CASE_MIN):
        # Letters alone only at 24 or more: a shorter mixed-case word run is likely a name.
        wordy = sum(len(w) for w in WORDS.findall(value)) / len(value)
        return entropy(value) > MIXED_ENTROPY and wordy < WORDY
    if digits and (upper or lower) and len(value) >= ONE_CASE_MIN:
        if SEPARATORS.search(value):
            return False
        words = [w for w in ONE_CASE_WORDS.findall(value) if vowel_share(w) >= VOWELS]
        return entropy(value) > MIXED_ENTROPY and sum(map(len, words)) / len(value) < WORDY
    return False


def covering(spans: list[tuple[int, ...]], starts: list[int], start: int, end: int) -> int:
    """The index of the span in a sorted, non-overlapping list that touches start..end, or
    -1. A binary search, so many links or emails don't make a long copy slow."""
    at = bisect.bisect_right(starts, end - 1) - 1
    return at if at >= 0 and spans[at][1] > start else -1


def random_runs(text: str, left: list[tuple[int, int]] | None = None) -> list[tuple[int, int]]:
    """Runs that look random enough to be a secret, outside Google IDs and meeting links.
    `left` is what later steps replace whole (emails and phone numbers), sorted: that part
    of a run is left to the step, and the parts before and after it are checked on their
    own. In
    a link's path, each part between slashes is checked on its own, so the site's name and
    plain path words stay readable; a value in its query is checked whole."""
    kept = kept_links(text)
    kept_starts = [s for s, _ in kept]
    links = [(m.start(), m.end(), m.start() + query_start(m[0])) for m in LINK.finditer(text)]
    link_starts = [s for s, _, _ in links]
    emails = left if left is not None else [m.span() for m in EMAILISH.finditer(text)]
    email_starts = [s for s, _ in emails]
    found = []
    for run in RUN.finditer(text):
        pieces = [run.span()]
        link = covering(links, link_starts, *run.span())
        if link != -1:
            if run.start() < links[link][2]:
                pieces = [
                    (m.start() + run.start(), m.end() + run.start())
                    for m in re.finditer(r"[^/]+", run[0])
                ]
        for piece in pieces:
            # What a later step takes (an email, a phone number) is left to it, and the parts
            # of the run before and after it are checked on their own. But if one sits wholly
            # inside the run, like digits inside a key, the whole run is checked first.
            parts = outside(piece, emails, email_starts)
            if parts != [piece] and parts and parts[0][0] == piece[0] and parts[-1][1] == piece[1]:
                parts = [piece] + parts
            for start, end in parts:
                # Strip the symbols at its edges: "--" or "==" around a word isn't part of it.
                while start < end and text[start] in "+/_=-":
                    start += 1
                while end > start and text[end - 1] in "+/_=-":
                    end -= 1
                if end - start < MIN_RUN:
                    continue
                k = covering(kept, kept_starts, start, end)
                if k != -1 and kept[k][0] <= start and end <= kept[k][1]:
                    continue
                if looks_random(text[start:end]):
                    found.append((start, end))
                    if (start, end) == piece or (start <= piece[0] and piece[1] <= end):
                        break  # the whole run goes, so its parts needn't be checked
    return found


def outside(
    piece: tuple[int, int], spans: list[tuple[int, int]], starts: list[int]
) -> list[tuple[int, int]]:
    """The parts of `piece` that no span in a sorted, non-overlapping list covers."""
    start, end = piece
    parts = []
    at = max(bisect.bisect_right(starts, start) - 1, 0)
    while at < len(spans) and spans[at][0] < end:
        s, e = spans[at]
        if e > start:
            if s > start:
                parts.append((start, s))
            start = max(start, e)
        at += 1
    if start < end:
        parts.append((start, end))
    return parts


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
            if PLACEHOLDER_ONLY.fullmatch(value):
                continue  # already scrubbed: the last check passes it
            spans.append((start, end, True))
            values.add(value)
    ours = [LINK_PASSWORD, QUERY_SECRET] + ([CURL_USER] if "curl" in low else [])
    for pattern in ours:
        for match in pattern.finditer(seen):
            if PLACEHOLDER_ONLY.fullmatch(match["secret"]):
                continue
            spans.append((*match.span("secret"), True))
            # Our rules have no entropy setting, so a plain word (?key=features) is replaced
            # where it's found, but only a value that looks random is replaced everywhere.
            if entropy(match["secret"]) > OURS_SPREAD_ENTROPY:
                values.add(match["secret"])
    for start, end in chosen_passwords(seen, low):
        spans.append((start, end, True))
        if entropy(seen[start:end]) > OURS_SPREAD_ENTROPY:
            values.add(seen[start:end])
    # A secret found once is replaced everywhere it appears in the copy.
    for value in values:
        if len(value) < 8:
            continue
        at = seen.find(value)
        while at != -1:
            spans.append((at, at + len(value), True))
            at = seen.find(value, at + 1)
    spans += [(start, end, False) for start, end in random_runs(seen, later_steps(text, where))]
    # A meeting link is replaced whole by the next step, so nothing inside it is taken now:
    # a [secret] in its user or query would split it, and it would no longer read as one.
    meetings = [(start, end) for start, end, _ in meeting_links(seen)]
    starts = [start for start, _ in meetings]
    spans = [
        (s, e, sure)
        for s, e, sure in spans
        if (k := covering(meetings, starts, s, e)) == -1
        or not (meetings[k][0] <= s and e <= meetings[k][1])
    ]
    # Back to the real text, covering any invisible characters inside.
    real = [(where[s], where[e - 1] + 1, sure) for s, e, sure in spans]
    return [(s, e, SECRET if sure else MAYBE_SECRET) for s, e, sure in merge(real)]


def chosen_passwords(seen: str, low: str) -> list[tuple[int, int]]:
    """Where PASSWORD_VALUE's values are in the text as rules read it (`low` is it in small
    letters), leaving out a value with no letter or digit (password: ********), one that
    starts with a placeholder or a link, and a prose word after : or "is"."""
    if "pass" not in low and "pwd" not in low:
        return []
    found, at = [], 0
    # A value that's left alone is read again from its start, so a password inside it
    # ("[secret] for staging; demo password=owl7") is still found.
    while match := PASSWORD_VALUE.search(seen, at):
        group = next(g for g in ("dq", "sq", "bq", "bare", "line") if match[g] is not None)
        start, end = match.span(group)
        at = max(start, match.start() + 1)
        if group in ("bare", "line"):
            while end > start and seen[end - 1] in PASSWORD_TRAIL:
                end -= 1
        value = seen[start:end]
        if not any(c.isalnum() for c in value) or PLACEHOLDER_ONLY.fullmatch(value):
            continue
        held = PLACEHOLDER_AT.match(seen, start)
        if held and (held.end() >= end or seen[held.end()].isspace()):
            continue
        if LINK_AT.match(value):
            continue
        plain = group in ("bare", "line") and match["sep"] in (":", None)
        if plain and value.lower() in PROSE:
            continue
        found.append((start, end))
        at = match.end()
    return found


def later_steps(text: str, where: list[int]) -> list[tuple[int, int]]:
    """What the email and phone steps will replace whole, found in the real text as those
    steps find it, then placed in view()'s positions (`where` maps view to real), sorted.
    Reading both the same way means a wide ＠ or an invisible character can't make the
    randomness check leave a run to an email step that won't take it."""
    from diffdata.scrub.text import EMAIL  # here, as text.py imports this module

    real = [m.span() for m in EMAIL.finditer(text)] + [s[:2] for s in phone_numbers(text)]
    spans = []
    for start, end in real:
        s, e = bisect.bisect_left(where, start), bisect.bisect_left(where, end)
        if e > s:
            spans.append((s, e))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


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


def meeting_end(text: str, end: int) -> int:
    """Where a meeting link ends: on past brackets in its query, but not past a space, its
    markup, or a ) it didn't open (Markdown's [label](link))."""
    depth = 0
    while end < len(text):
        c = text[end]
        if c.isspace() or c in "<>|\"'`":
            break
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
        end += 1
    return end


def meeting_links(text: str) -> list[tuple[int, int, str]]:
    """Where the meeting links are: the whole link, passcode and all. A link inside one
    already found is part of it."""
    # Read as the secret step reads text, so a host with an invisible character or wide
    # letters in it (zoom\u200b.us) is still a meeting link, here and in the last check.
    seen, where = view(text)
    found: list[tuple[int, int]] = []
    for link in LINK.finditer(seen):
        if found and link.start() < found[-1][1]:
            continue
        if is_meeting(link[0]):
            found.append((link.start(), meeting_end(seen, link.end())))
    return [(where[s], where[e - 1] + 1, MEETING) for s, e in found if e > s]


def phone_numbers(text: str) -> list[tuple[int, int, str]]:
    """Where the phone numbers are, and tel: links, but not digits inside other links."""
    spans = [(*m.span(), True) for m in TEL.finditer(text)]
    masked = list(text)
    masks = [m.span() for m in LINK.finditer(text)] + [m.span() for m in EMAILISH.finditer(text)]
    for start, end in masks + [s[:2] for s in spans]:
        masked[start:end] = MASK * (end - start)
    masked = "".join(masked)
    for region in REGIONS:
        # No cap on tries: by default the matcher stops after 65,535 numbers it can't use,
        # without a word, so a big table of numbers could hide a phone number after it.
        for number in phonenumbers.PhoneNumberMatcher(masked, region, max_tries=sys.maxsize):
            spans.append((number.start, number.end, True))
    return [(s, e, PHONE) for s, e, _ in merge(spans)]


def replace(text: str, spans: list[tuple[int, int, str]]) -> str:
    """The text with each span put back as its placeholder."""
    pieces, done = [], 0
    for start, end, placeholder in spans:
        pieces += [text[done:start], placeholder]
        done = end
    return "".join(pieces) + text[done:]
