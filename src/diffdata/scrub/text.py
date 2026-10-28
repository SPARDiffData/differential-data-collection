"""Replace what identifies people in a copy's text, flag what can't be matched, and check
what's left (SCR-2).

The steps run in this order, each on the text the one before left:
  1. emails become [email]
  2. Slack user IDs become that person's [Person N], or [name?] if the people list
     doesn't have them
  3. known people's names become their [Person N]: whole words in any case, longest first
     ("Alex Example" before "Alex"), and never inside a placeholder already put in. A name
     two people share becomes [name].
  4. @mentions of names nobody in the list has become @[name?]
Text is compared in one Unicode form (NFC), so a name typed two ways still matches, and
the copy is written in that form. Later steps slot into the order: secrets first, then
meeting links and phone numbers before emails.

A person's names are each name and nickname whole, and each word of their Slack names. An
initial on its own, like "J.", isn't one: it would hide every J.

`leftovers()` is the separate last check. It scans a whole scrubbed text again for any
email, any Slack-ID-shaped word, and any name or Slack ID in the people list. A copy with
anything left isn't written.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from diffdata.common.documents import EMAIL as DOCUMENT_EMAIL
from diffdata.scrub.people import PLACEHOLDER, SLACK_ID, People, Person, nfc

# documents.EMAIL, starting only where a word starts: the same matches, but a long run of
# letters with no @, like a pasted image, is read once rather than once per letter.
EMAIL = re.compile(r"(?<![\w.+-])" + DOCUMENT_EMAIL.pattern)
SHARED = "shared name"
UNKNOWN_ID = "unknown Slack ID"
UNKNOWN_MENTION = "unknown mention"
# The placeholders the steps put in. Nothing is matched inside one.
KEPT = r"\[(?:Person [1-9][0-9]*|name\??|email)\]"
# A name starts and ends at a letter's edge: "Alex's" and "alex_2" hold Alex; "Alexa" doesn't.
BEFORE, AFTER = r"(?<![^\W\d_])", r"(?![^\W\d_])"
# "@jordan", or "@Jordan Smith": an @ that starts a word, and up to two more words in
# capitals after it, since a display name can have spaces. Emails are gone by then.
MENTION = re.compile(
    r"(?<![\w@./:+-])@(?P<first>[^\W_](?:[\w'.-]*[^\W_])?)"
    r"(?P<more>(?:[ \t]+[A-ZÀ-ÖØ-Þ](?:[\w'.-]*[^\W_])?){0,2})"
)
# Slack's own: <!here>, <!channel> and <!everyone> show as @here, @channel and @everyone.
NOT_PEOPLE = {"here", "channel", "everyone"}
# The steps `Scrubber.scrub` runs, then the last check, as the manifest lists them.
CHECKS = ("emails", "Slack IDs", "known names", "unknown mentions", "last check")


@dataclass(frozen=True)
class Flag:
    """Text hidden without knowing who it is. `text` is real data, kept only in flags.md."""

    kind: str
    text: str
    shared_by: tuple[str, ...] = ()  # the placeholders that share a name


def key(text: str) -> str:
    """How names are compared: one Unicode form, any case, any spacing."""
    return " ".join(nfc(text).casefold().split())


def variants(person: Person) -> set[str]:
    """Every way the scrub looks for a person."""
    found = {*person.names, *person.nicknames}
    for name in person.names:
        found |= {word.strip("()[]{}\"'.,;:!?") for word in name.split()}
    return {v for v in found if sum(c.isalpha() for c in v) >= 2}


class Scrubber:
    """The replacements for one people list, built once per run."""

    def __init__(self, people: People):
        self.ids = {slack_id: p.placeholder for p in people.people for slack_id in p.slack_ids}
        self.not_names = {key(word) for word in people.not_names}
        known = sorted(self.ids, key=longest_first)
        either = "|".join([*map(re.escape, known), SLACK_ID.pattern])
        self.slack_id = re.compile(rf"(?<![A-Za-z0-9])(?:{either})(?![A-Za-z0-9])")

        owners: dict[str, set[str]] = defaultdict(set)
        spellings: dict[str, set[str]] = defaultdict(set)
        for person in people.people:
            for variant in variants(person):
                if key(variant) not in self.not_names:
                    owners[key(variant)].add(person.placeholder)
                    spellings[key(variant)].add(nfc(variant))
        # Longest first, so the longest name that fits wins. Each name has its own group,
        # so a match says whose it is.
        keys = sorted(owners, key=longest_first)
        self.shared: list[tuple[str, ...]] = []  # for each name, the people who share it
        self.targets: list[str] = []  # for each name, what replaces it
        groups = []
        for i, k in enumerate(keys):
            shared = tuple(sorted(owners[k], key=lambda p: int(PLACEHOLDER.fullmatch(p)[1])))
            self.shared.append(shared if len(shared) > 1 else ())
            self.targets.append("[name]" if len(shared) > 1 else shared[0])
            either = "|".join(map(spelled, sorted(spellings[k], key=longest_first)))
            groups.append(f"(?P<n{i}>{either})")
        names = [rf"{BEFORE}(?:{'|'.join(groups)}){AFTER}"] if groups else []
        self.names = re.compile("|".join([f"(?P<kept>{KEPT})", *names]), re.IGNORECASE)

    def scrub(self, text: str) -> tuple[str, list[Flag]]:
        """The text with every step applied, and what was flagged on the way."""
        flags: list[Flag] = []
        text = nfc(text)
        for step in (self.emails, self.slack_ids, self.known_names, self.unknown_mentions):
            text = step(text, flags)
        return text, flags

    def emails(self, text: str, flags: list[Flag]) -> str:
        return EMAIL.sub("[email]", text)

    def slack_ids(self, text: str, flags: list[Flag]) -> str:
        def replace(match: re.Match) -> str:
            if match[0] in self.ids:
                return self.ids[match[0]]
            flags.append(Flag(UNKNOWN_ID, match[0]))
            return "[name?]"

        return self.slack_id.sub(replace, text)

    def known_names(self, text: str, flags: list[Flag]) -> str:
        def replace(match: re.Match) -> str:
            if match["kept"]:
                return match[0]
            found = int(match.lastgroup[1:])
            if self.shared[found]:
                flags.append(Flag(SHARED, match[0], self.shared[found]))
            return self.targets[found]

        return self.names.sub(replace, text)

    def unknown_mentions(self, text: str, flags: list[Flag]) -> str:
        def replace(match: re.Match) -> str:
            if key(match["first"]) in NOT_PEOPLE | self.not_names:
                return match[0]
            flags.append(Flag(UNKNOWN_MENTION, match["first"] + match["more"]))
            return "@[name?]"

        return MENTION.sub(replace, text)

    def leftovers(self, text: str) -> list[str]:
        """What the last check finds in a scrubbed text, by kind and never by value, so it
        can go in the manifest. Empty means the copy can be kept."""
        text = nfc(text)
        found = []
        if EMAIL.search(text):
            found.append("an email")
        if self.slack_id.search(text):
            found.append("a Slack ID")
        if any(not match["kept"] for match in self.names.finditer(text)):
            found.append("a name")
        return found


def longest_first(text: str) -> tuple[int, str]:
    return -len(text), text


def spelled(name: str) -> str:
    """A name as a pattern: its words, with any spacing between them."""
    return r"\s+".join(map(re.escape, name.split()))
