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
with any spacing, with a curly apostrophe the same as a straight one, and with wide or
styled letters (Ａｌｅｘ, 𝐀𝐥𝐞𝐱) the same as plain ones. Characters that show as nothing,
like a zero-width space, a soft hyphen or a variation selector, are read past inside a
name and anywhere in the spacing between its words, so "Alex" with a zero-width space
inside is replaced whole, and so is "Big Al" with one between two spaces. The text is
written in one Unicode form (NFC), so a name typed two ways is the same text.

A name ends where no letter touches it, but a Latin name ends at any letter that isn't
Latin, so "Alex" is found in 我和Alex明天开会 or Alex는. A name in a script written without
spaces isn't found inside other words: 李 in 李明天 is left as written, since matching
there would hide parts of other words. PR 4's spaCy step reads names in free text.

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
SYMBOL = r"[.!#$%'*+^`{|}~-]"
# A link can spell the @ as %40. Two emails with only symbols between them, as in a table
# row typed without spaces (a|b) or with bold ones (*a*|*b*), are two: the second can also
# start after the first symbol that follows the first one's domain, and run up to 64
# characters, the most an address holds before its @. The 64 keeps a long run with many
# such starts, like x+%40x+%40..., quick to read.
EMAIL = re.compile(
    rf'(?:(?<!{LOCAL})(?:{LOCAL}+|"[^"\n]+")|(?<=[\w\]]{SYMBOL}){LOCAL}{{1,64}})(?:@|%40)'
    r"(?:[\w-]+(?:\.[\w-]+)+|\[(?:IPv6:)?[0-9A-Fa-f.:]+\])"
)
# An address can start with symbols, so the ones between two emails are read as part of the
# second. They're kept, so a row keeps its | and *.
JOIN = re.compile(f"{SYMBOL}*")
SHARED = "shared name"
UNKNOWN_ID = "unknown Slack ID"
UNKNOWN_MENTION = "unknown mention"
# The placeholders the steps put in. Nothing is matched inside one.
KEPT = r"\[(?:Person [1-9][0-9]*|name\??|email)\]"
# A name starts and ends at a letter's edge: "Alex's" and "alex_2" hold Alex; "Alexa" doesn't.
BEFORE, AFTER = r"(?<![^\W\d_])", r"(?![^\W\d_])"
# The Latin letters folded text can hold. A Latin name's edge is any character but these.
# Their blocks hold a few symbols too, like U+A789, which looks like a colon. Those are left
# out, so a name next to one still ends there. A test checks that each of these is a letter.
LATIN = (
    r"a-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u02af\u1d00-\u1dbf\u1e00-\u1eff\u2c60-\u2c7f"
    r"\ua722-\ua788\ua78b-\ua7ca\ua7d0\ua7d1\ua7d3\ua7d5-\ua7d9\ua7f2-\ua7ff\uab30-\uab5a"
    r"\uab5c-\uab69"
)
LATIN_BEFORE, LATIN_AFTER = rf"(?<![{LATIN}])", rf"(?![{LATIN}])"
# Characters that show as nothing: Unicode's format characters (Cf), like a zero-width
# space, a soft hyphen or a direction mark, and the rest of what it calls default ignorable,
# like a variation selector or a Hangul filler. Text pasted from web pages or Word can hold
# them inside a name. A test checks the list against Python's own Unicode data.
INVISIBLE = (
    r"\u00ad\u034f\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u115f\u1160\u17b4\u17b5"
    r"\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0"
    r"\ufff0-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3"
    r"\U0001d173-\U0001d17a\U000e0000-\U000e0fff"
)
HIDDEN = re.compile(f"[{INVISIBLE}]+")
# Wide letters from Chinese, Japanese and Korean keyboards (Ａ), narrow ones (ｱ) and styled
# ones from fancy-text tools (𝐀) are compared as their plain letter. Other compatibility
# forms keep their shape, so "Alex™" still ends at the ™.
PLAIN_FORMS = ("<wide>", "<narrow>", "<font>")
# "@jordan", "@alex.newperson", "cc:@_jordan" or "@Jordan Smith": an @ that doesn't follow
# a letter, a handle, and up to two more words, since a display name can have spaces. Only
# words in capitals count, in any alphabet, which `Scrubber.mentions` checks. A handle reads
# past invisible characters, inside a word and anywhere in the spacing between two, which
# needs a space or tab. Each word and gap has its own group, so the label is never split
# again: a split that tries every place in a long run of Hangul fillers is slow. Emails are
# gone by then.
WORD = rf"(?:[\w.'’{INVISIBLE}-]*\w)?"
GAP = rf"[{INVISIBLE}]*[ \t][ \t{INVISIBLE}]*"
MENTION = re.compile(
    rf"(?<![\w@])@(?P<label>(?P<w0>[^\W\d]{WORD})"
    rf"(?:(?P<g1>{GAP})(?P<w1>[^\W\d_]{WORD})(?:(?P<g2>{GAP})(?P<w2>[^\W\d_]{WORD}))?)?)"
)
MENTION_PARTS = ("w0", "g1", "w1", "g2", "w2")
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
    """One character as names are compared: no accent, folded case, one apostrophe, and a
    wide or styled letter as its plain one. It can become none (a lone accent) or more than
    one (ß becomes ss)."""
    if letter.isascii():
        return letter.lower()
    if unicodedata.decomposition(letter).startswith(PLAIN_FORMS):
        letter = unicodedata.normalize("NFKC", letter)
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
    """How a name is compared: folded, with single spaces, and nothing invisible."""
    return " ".join(HIDDEN.sub("", folded(nfc(text))[0]).split())


def initial(part: str) -> bool:
    """One letter of an alphabet with capitals, like "J." or "J"."""
    letters = [c for c in HIDDEN.sub("", part) if c.isalpha()]
    return len(letters) == 1 and letters[0].lower() != letters[0].upper()


def variants(person: Person) -> set[str]:
    """Every way the scrub looks for a person."""
    parts = {
        part.strip("()[]{}\"'’,;:!?")
        for name in person.names
        for part in re.split(r"[\s._-]+", name)
    }
    found = {*person.names, *person.nicknames} | {part for part in parts if not initial(part)}
    # A Hangul filler is a letter that shows as nothing, so a "blank" Slack name made of
    # them has no letter once compared, and would otherwise match everywhere.
    return {variant for variant in found if any(c.isalpha() for c in key(variant))}


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
        # The names that start with a Latin letter share one check of the edge before them,
        # and the rest share another, so a place inside a word is passed over at once rather
        # than tried against every name. A name fits only the group its first letter is in.
        names = []
        for before, latin in ((LATIN_BEFORE, True), (BEFORE, False)):
            group = [
                f"(?P<n{i}>{spelled(k)}{after(k)})"
                for i, k in enumerate(keys)
                if bool(re.match(f"[{LATIN}]", k)) == latin
            ]
            if group:
                names.append(f"{before}(?:{'|'.join(group)})")
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
        return EMAIL.sub(lambda match: JOIN.match(match[0])[0] + "[email]", text)

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
            words = [match[part] for part in MENTION_PARTS if match[part] is not None]
            end = 1
            # A Hangul filler is a letter that shows as nothing, so a word can be all hidden.
            while end < len(words) and HIDDEN.sub("", words[end + 1])[:1].isupper():
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
    """A folded name as a pattern: its words, with any spacing between them, and anything
    invisible between their letters or anywhere in that spacing, which needs a real space."""
    gap = f"[{INVISIBLE}]*"
    space = rf"[{INVISIBLE}]*\s[\s{INVISIBLE}]*"
    return space.join(gap.join(map(re.escape, word)) for word in name.split())


def after(name: str) -> str:
    """The edge after a folded name: any character but a Latin letter, if it ends with one,
    and otherwise any character but a letter."""
    return LATIN_AFTER if re.search(f"[{LATIN}]$", name) else AFTER
