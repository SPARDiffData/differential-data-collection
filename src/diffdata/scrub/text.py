"""Replace what identifies people in a copy's text, flag what can't be matched, and check
what's left (SCR-2).

The steps run in this order, each on the text the one before left:
  1. emails become [email]
  2. Slack user IDs become that person's [Person N], or [name?] if the people list
     doesn't have them
  3. @mentions are read whole, handle and all: a known name becomes its [Person N], and
     anything else becomes @[name?], so a handle like @alex.newperson, or a display name
     like @Alex Newperson that only starts with a known name, can't half survive
  4. known people's names become their [Person N]: whole words, longest first ("Alex
     Example" before "Alex"), and never inside a placeholder already put in
A name two people share becomes [name]. Later steps slot into the order: secrets first,
then meeting links and phone numbers before emails.

Names are compared folded: in any case (WEISS for Weiß), without accents (Jose for José),
with any spacing, and with a curly apostrophe the same as a straight one. The text is
written in one Unicode form (NFC), so a name typed two ways is the same text.

A person's names are each name and nickname whole, and each part of their Slack names,
split at spaces, hyphens, dots and underscores: "Anne-Marie" gives "Anne", and "riley.fake"
gives "riley". An initial on its own, like "J.", isn't a name: it would hide every J. A
one-letter name in a script without capitals, like 李, is.

`leftovers()` is the separate last check. It scans a whole scrubbed text again for any
email, any Slack-ID-shaped word, and any name or Slack ID in the people list. A copy with
anything left isn't written.
"""

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from diffdata.scrub.people import PLACEHOLDER, SLACK_ID, People, Person, nfc

# An email: before the @, the letters, digits and symbols an address can hold, or a part in
# quotes; after it, a domain or an IP address in brackets, like [192.0.2.1]. Of the symbols,
# / = ? and & are left out: they're rare in an address and build links, so an email in a
# link's query takes only itself. It starts only where such a run starts, so a long run
# with no @, like a pasted image, is read once rather than once per letter.
LOCAL = r"[\w.!#$%'*+^`{|}~-]"
# A link can spell the @ as %40.
EMAIL = re.compile(
    rf'(?<!{LOCAL})(?:{LOCAL}+|"[^"\n]+")(?:@|%40)'
    r"(?:[\w-]+(?:\.[\w-]+)+|\[(?:IPv6:)?[0-9A-Fa-f.:]+\])"
)
SHARED = "shared name"
UNKNOWN_ID = "unknown Slack ID"
UNKNOWN_MENTION = "unknown mention"
# The placeholders the steps put in. Nothing is matched inside one.
KEPT = r"\[(?:Person [1-9][0-9]*|name\??|email)\]"
# A name starts and ends at a letter's edge: "Alex's" and "alex_2" hold Alex; "Alexa" doesn't.
BEFORE, AFTER = r"(?<![^\W\d_])", r"(?![^\W\d_])"
# "@jordan", "@alex.newperson", "cc:@_jordan" or "@Jordan Smith": an @ that doesn't follow
# a letter, a handle, and up to two more words, since a display name can have spaces. Only
# words in capitals count, in any alphabet, which `Scrubber.mentions` checks. Emails are
# gone by then.
MENTION = re.compile(
    r"(?<![\w@])@(?P<label>[^\W\d](?:[\w.'’-]*\w)?(?:[ \t]+[^\W\d_](?:[\w.'’-]*\w)?){0,2})"
)
POSSESSIVE = re.compile(r"(?P<owner>.+)(?P<s>['’]s)")
# Slack's own: <!here>, <!channel> and <!everyone> show as @here, @channel and @everyone.
NOT_PEOPLE = {"here", "channel", "everyone"}
APOSTROPHES = str.maketrans("’‘ʼ", "'''")
# The steps `Scrubber.scrub` runs, then the last check, as the manifest lists them.
CHECKS = ("emails", "Slack IDs", "mentions", "known names", "last check")


@dataclass(frozen=True)
class Flag:
    """Text hidden without knowing who it is. `text` is real data, kept only in flags.md."""

    kind: str
    text: str
    shared_by: tuple[str, ...] = ()  # the placeholders that share a name


def fold_letter(letter: str) -> str:
    """One character as names are compared: no accent, folded case, one apostrophe. It can
    become none (a lone accent) or more than one (ß becomes ss)."""
    if letter.isascii():
        return letter.lower()
    bare = "".join(c for c in unicodedata.normalize("NFD", letter) if not unicodedata.combining(c))
    return bare.casefold().translate(APOSTROPHES)


def folded(text: str) -> tuple[str, list[int]]:
    """The text folded, and for each of its characters, where it came from in `text`."""
    pieces, where = [], []
    for at, letter in enumerate(text):
        piece = fold_letter(letter)
        pieces.append(piece)
        where += [at] * len(piece)
    return "".join(pieces), where


def key(text: str) -> str:
    """How a name is compared: folded, with single spaces."""
    return " ".join(folded(nfc(text))[0].split())


def initial(part: str) -> bool:
    """One letter of an alphabet with capitals, like "J." or "J"."""
    letters = [c for c in part if c.isalpha()]
    return len(letters) == 1 and letters[0].lower() != letters[0].upper()


def variants(person: Person) -> set[str]:
    """Every way the scrub looks for a person."""
    parts = {
        part.strip("()[]{}\"'’,;:!?")
        for name in person.names
        for part in re.split(r"[\s._-]+", name)
    }
    found = {*person.names, *person.nicknames} | {part for part in parts if not initial(part)}
    return {variant for variant in found if any(c.isalpha() for c in variant)}


class Scrubber:
    """The replacements for one people list, built once per run."""

    def __init__(self, people: People):
        self.ids = {slack_id: p.placeholder for p in people.people for slack_id in p.slack_ids}
        self.not_names = {key(word) for word in people.not_names}
        known = sorted(self.ids, key=longest_first)
        either = "|".join([*map(re.escape, known), SLACK_ID.pattern])
        self.slack_id = re.compile(rf"(?<![A-Za-z0-9])(?:{either})(?![A-Za-z0-9])")

        owners: dict[str, set[str]] = defaultdict(set)
        for person in people.people:
            for variant in variants(person):
                if key(variant) not in self.not_names:
                    owners[key(variant)].add(person.placeholder)
        # Longest first, so the longest name that fits wins. Each name has its own group,
        # so a match says whose it is.
        keys = sorted(owners, key=longest_first)
        self.index = {k: i for i, k in enumerate(keys)}
        self.shared: list[tuple[str, ...]] = []  # for each name, the people who share it
        self.targets: list[str] = []  # for each name, what replaces it
        for k in keys:
            shared = tuple(sorted(owners[k], key=lambda p: int(PLACEHOLDER.fullmatch(p)[1])))
            self.shared.append(shared if len(shared) > 1 else ())
            self.targets.append("[name]" if len(shared) > 1 else shared[0])
        groups = "|".join(f"(?P<n{i}>{spelled(k)})" for i, k in enumerate(keys))
        names = [rf"{BEFORE}(?:{groups}){AFTER}"] if keys else []
        # Run on folded text, so it finds a name in any case, with or without accents.
        self.names = re.compile("|".join([f"(?P<kept>{KEPT})", *names]), re.IGNORECASE)

    def scrub(self, text: str) -> tuple[str, list[Flag]]:
        """The text with every step applied, and what was flagged on the way."""
        flags: list[Flag] = []
        text = nfc(text)
        for step in (self.emails, self.slack_ids, self.mentions, self.known_names):
            text = step(text, flags)
        return text, flags

    def target(self, found: int, written: str, flags: list[Flag]) -> str:
        """What replaces known name number `found`, flagging it if two people share it."""
        if self.shared[found]:
            flags.append(Flag(SHARED, written, self.shared[found]))
        return self.targets[found]

    def emails(self, text: str, flags: list[Flag]) -> str:
        return EMAIL.sub("[email]", text)

    def slack_ids(self, text: str, flags: list[Flag]) -> str:
        def replace(match: re.Match) -> str:
            if match[0] in self.ids:
                return self.ids[match[0]]
            flags.append(Flag(UNKNOWN_ID, match[0]))
            return "[name?]"

        return self.slack_id.sub(replace, text)

    def mentions(self, text: str, flags: list[Flag]) -> str:
        def replace(match: re.Match) -> str:
            # The words after the handle that start with a capital; the rest is ordinary text.
            words = re.split(r"([ \t]+)", match["label"])
            end = 1
            while end < len(words) and words[end + 1][0].isupper():
                end += 2
            label, after = "".join(words[:end]), "".join(words[end:])
            # "@Alex's doc" is Alex, and the 's stays.
            owner, s = label, ""
            if key(label) not in self.index and (possessive := POSSESSIVE.fullmatch(label)):
                owner, s = possessive["owner"], possessive["s"]
            found = self.index.get(key(owner))
            if found is not None:
                return "@" + self.target(found, owner, flags) + s + after
            # Slack's own are one word, so any words after one are ordinary text.
            if key(owner.split()[0]) in NOT_PEOPLE or key(owner) in self.not_names:
                return match[0]
            # Anything else, even one that starts with a known name, isn't given to anyone.
            flags.append(Flag(UNKNOWN_MENTION, owner))
            return "@[name?]" + s + after

        return MENTION.sub(replace, text)

    def known_names(self, text: str, flags: list[Flag]) -> str:
        fold, where = folded(text)
        pieces, done = [], 0
        for match in self.names.finditer(fold):
            if match["kept"]:
                continue
            # Back to the text's own characters. One can fold to two (ß to ss), so a match
            # may start inside a character the one before already took.
            start, end = max(where[match.start()], done), where[match.end() - 1] + 1
            found = int(match.lastgroup[1:])
            pieces += [text[done:start], self.target(found, text[start:end], flags)]
            done = end
        return "".join(pieces) + text[done:]

    def leftovers(self, text: str) -> list[str]:
        """What the last check finds in a scrubbed text, by kind and never by value, so it
        can go in the manifest. Empty means the copy can be kept."""
        text = nfc(text)
        found = []
        if EMAIL.search(text):
            found.append("an email")
        if self.slack_id.search(text):
            found.append("a Slack ID")
        if any(not match["kept"] for match in self.names.finditer(folded(text)[0])):
            found.append("a name")
        return found


def longest_first(text: str) -> tuple[int, str]:
    return -len(text), text


def spelled(name: str) -> str:
    """A folded name as a pattern: its words, with any spacing between them."""
    return r"\s+".join(map(re.escape, name.split()))
