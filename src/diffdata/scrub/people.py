"""The people list (SCR-2): everyone the scrub knows, and the placeholder each one gets.

The list is private/people.toml in the data folder. It links each placeholder to a real
person, so it never goes in a scrubbed copy. Every scrub learns who's who from the saved
Slack data: each author in the message store, and each mention in messages and canvas
versions, with its label when it has one. A new Slack ID gets the next [Person N], and a
number never changes once given. A Slack ID known only from a mention gets its own number.

The list is meant to be edited by hand:
  nicknames  more names a person goes by, matched whole, as written
  not_names  words that are names here but ordinary words too, like "Will". They're left
             as written everywhere.
A person can be added by hand too, with a placeholder no one else has.

Python reads TOML but can't write it, so `render()` writes this one simple shape itself.
"""

import json
import re
import tomllib
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from diffdata.collect import slack_canvas
from diffdata.common.documents import documents_dir
from diffdata.discover.slack_export import read_store
from diffdata.discover.slack_inbox import slack_dir

PEOPLE_FILE = "private/people.toml"  # under the data folder, as messages name it
PLACEHOLDER = re.compile(r"\[Person ([1-9][0-9]*)\]")
# A Slack user ID: a whole word of U or W, then 8 or more capital letters and digits, at
# least one a digit. Channel, file and team IDs (C, F, T) don't match, so links still work.
SLACK_ID = re.compile(r"(?<![A-Za-z0-9])[UW](?=[A-Z]*[0-9])[A-Z0-9]{8,}(?![A-Za-z0-9])")
ID_VALUE = re.compile(r"[A-Z][A-Z0-9]+")
PERSON_FIELDS = ("placeholder", "slack_ids", "names", "nicknames")
HEADER = """\
# The people the scrub knows, and the placeholder each one gets (SCR-2).
# It links placeholders to real people, so it stays in private/ and is never shared.
#
# Each scrub adds new people and names from Slack. You can add, for any person:
#   nicknames = ["..."]  other names they go by
# and at the top:
#   not_names = ["..."]  words that are names here but ordinary words too, like "Will".
#                        They're left as written everywhere.
# Never change or reuse a placeholder. Comments you add are lost when a scrub adds someone.
# To start a new list, delete the whole private/ folder: everyone gets a new number."""


class PeopleError(Exception):
    """The people list can't be trusted. The message names the problem, never a person."""


@dataclass
class Person:
    placeholder: str
    slack_ids: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # from Slack, kept up to date by each scrub
    nicknames: list[str] = field(default_factory=list)  # added by hand


@dataclass
class People:
    people: list[Person] = field(default_factory=list)
    not_names: list[str] = field(default_factory=list)


def nfc(text: str) -> str:
    """One Unicode form, so the same name typed two ways is the same text."""
    return unicodedata.normalize("NFC", text)


def number(person: Person) -> int:
    return int(PLACEHOLDER.fullmatch(person.placeholder)[1])


def read_people(path: Path) -> People:
    """The people list in `path`, checked. Anything it can't trust stops the scrub."""
    try:
        raw = tomllib.loads(path.read_bytes().decode("utf-8"))
    except UnicodeDecodeError as e:
        raise PeopleError(f"{PEOPLE_FILE} isn't UTF-8 text. Save it as UTF-8.") from e
    except tomllib.TOMLDecodeError as e:
        # Its message can quote the file, so only the place is kept.
        where = re.search(r"at line \d+, column \d+|at end of document", str(e))
        raise PeopleError(
            f"{PEOPLE_FILE} can't be read as TOML ({where[0] if where else 'somewhere in it'})."
        ) from e
    if raw.keys() - {"not_names", "person"}:
        raise PeopleError(
            f"{PEOPLE_FILE} has a setting diffdata doesn't know at the top. It can hold "
            "not_names and [[person]] entries only."
        )
    not_names = strings(raw.get("not_names", []), "not_names")
    entries = raw.get("person", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise PeopleError(f"Each person in {PEOPLE_FILE} must start with [[person]].")
    people: list[Person] = []
    placeholders: dict[str, int] = {}
    owners: dict[str, int] = {}
    for at, entry in enumerate(entries, start=1):
        where = f"person {at} in {PEOPLE_FILE}"
        if entry.keys() - set(PERSON_FIELDS):
            raise PeopleError(
                f"The {where} has a field diffdata doesn't know. Its fields can be "
                f"{', '.join(PERSON_FIELDS)}."
            )
        placeholder = entry.get("placeholder")
        if not isinstance(placeholder, str) or not PLACEHOLDER.fullmatch(placeholder):
            raise PeopleError(f'The {where} needs a placeholder like "[Person 1]".')
        if placeholder in placeholders:
            raise PeopleError(
                f"People {placeholders[placeholder]} and {at} in {PEOPLE_FILE} have the same "
                "placeholder. Each person needs their own."
            )
        placeholders[placeholder] = at
        person = Person(
            placeholder,
            *(
                strings(entry.get(name, []), f"{name} for the {where}")
                for name in PERSON_FIELDS[1:]
            ),
        )
        for slack_id in person.slack_ids:
            if not ID_VALUE.fullmatch(slack_id):
                raise PeopleError(
                    f"The {where} has a Slack ID that isn't one. They look like U0123ABCD."
                )
            if owners.setdefault(slack_id, at) != at:
                raise PeopleError(
                    f"People {owners[slack_id]} and {at} in {PEOPLE_FILE} have the same Slack "
                    "ID. One Slack ID is one person: merge them, or remove it from one."
                )
        people.append(person)
    return People(people, not_names)


def strings(value, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PeopleError(f"{what} must be a list of words in quotes, none of them empty.")
    return [nfc(v.strip()) for v in value]


def seen_in_slack(data: Path) -> dict[str, set[str]]:
    """Each Slack user ID in the saved data, with every name it's shown with: as an author in
    the message store, and as a mention's label in a message or a canvas version. Bots
    aren't people, so their names are left out."""
    seen: dict[str, set[str]] = defaultdict(set)
    texts = []
    for store in sorted((slack_dir(data) / "store").glob("*/messages.jsonl")):
        for record in read_store(store):
            texts.append(record["text"] or "")
            if record["user_id"] and not record["bot"]:
                seen[record["user_id"]].add(record["user_name"])
    versions = sorted((documents_dir(data) / slack_canvas.SOURCE).glob("*/versions/*.md"))
    texts += [path.read_bytes().decode("utf-8") for path in versions]
    for text in texts:
        for sigil, user_id, label in slack_canvas.MENTION.findall(text):
            if sigil == "@" and SLACK_ID.fullmatch(user_id):
                seen[user_id].add(label)
    return {
        user_id: {nfc(n.strip()) for n in names if n.strip()} for user_id, names in seen.items()
    }


def add_seen(people: People, seen: dict[str, set[str]]) -> int:
    """Add what the saved data shows: a person with the next number for each new Slack ID,
    and new names for people already listed. Returns how many people are new."""
    by_id = {slack_id: person for person in people.people for slack_id in person.slack_ids}
    next_number = max(map(number, people.people), default=0) + 1
    new = 0
    for user_id in sorted(seen):  # so a run gives new people the same numbers every time
        person = by_id.get(user_id)
        if person is None:
            person = Person(f"[Person {next_number}]", [user_id])
            people.people.append(person)
            next_number, new = next_number + 1, new + 1
        person.names = sorted(set(person.names) | seen[user_id])
    return new


def render(people: People) -> str:
    """people.toml's text: the header, not_names, then each person in number order."""
    lines = [HEADER, "", f"not_names = {toml_list(people.not_names)}"]
    for person in sorted(people.people, key=number):
        lines += ["", "[[person]]", f"placeholder = {toml_string(person.placeholder)}"]
        lines += [f"{name} = {toml_list(getattr(person, name))}" for name in PERSON_FIELDS[1:]]
    return "\n".join(lines) + "\n"


def toml_string(value: str) -> str:
    # A JSON string is a TOML string too, but for DEL, which TOML needs escaped.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_list(values: list[str]) -> str:
    return "[" + ", ".join(map(toml_string, values)) + "]"
