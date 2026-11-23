"""`diffdata push` (STO-1, STO-2, STO-3, STO-9, STO-11): send the scrubbed copies the last
scrub passed to the team's folder in the store, and nothing else.

What to send comes only from `scrubbed/manifest.json`'s list of passed copies: an allow-list.
Nothing else in the data folder is ever read for sending, so the raw data, `private/` and
`slack/left_out.json` stay on this computer, and so would a private file added later.

Every check runs before the store is opened, so a refusal sends nothing:
  - an incomplete or wrong [store] section in config.toml
  - a setting in the environment that would send the copies somewhere other than Google
  - a scrub or another push running, or a scrub cut short
  - no scrubbed/ folder or manifest, or a manifest not exactly in the shape a scrub writes
  - scrubbed/ being a link, holding a link or a hard-linked file, or not being a folder
  - a listed copy that isn't a name a scrub makes, that's missing, a link or hard-linked, that
    the manifest also lists as held back, whose content changed since the scrub, or in which
    the scrub's last check, run again here, finds a name, a Slack ID or an email

The push holds the scrub's lock throughout, so no scrub can replace the copies while they're
checked and sent.

In the store, each copy goes to
    <folder>/<producer>/<project>/<source type>/<date>/<ID>.<first 16 of its SHA-256>.md
(STO-2). Identical content gets the same name and is skipped; changed content gets a new name,
beside the old one. Every add is create-only, so nothing is ever overwritten (STO-3, STO-9).
Each push then adds a record of what it sent: <folder>/<producer>/<project>/pushes/<time>.json.

Reports give counts. An error names a copy only by its place in the manifest.
"""

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from diffdata import __version__
from diffdata.common.config import Config
from diffdata.scrub.people import PEOPLE_FILE, PeopleError, read_people
from diffdata.scrub.run import IN_PROGRESS, LOCK, PRIVATE, SCRUBBED, is_link
from diffdata.scrub.text import CHECKS, Scrubber
from diffdata.store.backend import SignInError, Store, StoreError

MANIFEST = "manifest.json"
BACKENDS = ("gcs",)
# A folder, producer or project is one plain path segment.
SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Google Cloud Storage's bucket names: lowercase letters, digits, dots, - and _, starting and
# ending with a letter or digit. bucket_name() checks the lengths.
BUCKET = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
# The copies a scrub makes (scrub/run.py), and nothing else.
SLACK_COPY = re.compile(r"slack/export/(?P<id>[A-Z0-9]+)/(?P<date>\d{4}-\d{2}-\d{2})\.md")
CANVAS_COPY = re.compile(r"documents/current/slack-canvas_(?P<id>[A-Z0-9]+)\.md")
SHA256 = re.compile(r"[0-9a-f]{64}")
# A manifest entry has exactly the fields a scrub writes.
COPY_FIELDS = {"source", "id", "path", "sha256", "checks", "flags"}
HELD_FIELDS = {"source", "id", "why"}
SOURCES = ("slack", "slack-canvas")
# A canvas copy's date is the day it last changed, from its header.
LAST_CHANGED = re.compile(r"^last_changed: (\d{4}-\d{2}-\d{2})", re.MULTILINE)
MARKDOWN = "text/markdown; charset=utf-8"
NOTHING_SENT = "Nothing was sent."
# Settings Google's library reads that would send the copies somewhere other than Google.
REDIRECTS = ("STORAGE_EMULATOR_HOST", "GOOGLE_CLOUD_UNIVERSE_DOMAIN")


class PushError(Exception):
    """The push can't go ahead, or stopped. The message names no person and quotes no text."""


@dataclass(frozen=True)
class Settings:
    backend: str
    bucket: str
    folder: str
    producer: str
    project: str

    @property
    def prefix(self) -> str:
        return f"{self.folder}/{self.producer}/{self.project}"


@dataclass(frozen=True)
class Upload:
    copy: dict  # the manifest's entry
    name: str  # where it goes in the store
    data: bytes


def bucket_name(name: str) -> bool:
    """Whether `name` follows Google Cloud Storage's naming rules for a bucket."""
    if not BUCKET.fullmatch(name) or name.startswith("goog") or "google" in name:
        return False
    if "." in name:
        return len(name) <= 222 and all(1 <= len(part) <= 63 for part in name.split("."))
    return 3 <= len(name) <= 63


def settings(config: Config) -> Settings:
    """[store] from config.toml, checked. Anything missing or wrong stops the push before it
    signs in, naming the setting but never quoting its value."""
    found = Settings(
        config.store_backend,
        config.store_bucket,
        config.store_folder,
        config.store_producer,
        config.store_project,
    )
    where = f"under [store] in {config.path}" if config.path else "under [store] in config.toml"
    missing = [key for key, value in vars(found).items() if not value.strip()]
    if missing:
        raise PushError(
            f"{', '.join(missing)} {where} {'is' if len(missing) == 1 else 'are'} empty. The "
            f"project team gives you these; see config.example.toml. {NOTHING_SENT}"
        )
    if found.backend not in BACKENDS:
        raise PushError(f'backend {where} must be "gcs", the pilot store. {NOTHING_SENT}')
    if not bucket_name(found.bucket):
        raise PushError(
            f"bucket {where} isn't a Google Cloud Storage bucket name: 3 to 63 lowercase "
            f"letters, digits, - and _, or dotted parts of up to 63 each, with no spaces. "
            f"{NOTHING_SENT}"
        )
    for key in ("folder", "producer", "project"):
        if not SEGMENT.fullmatch(getattr(found, key)):
            raise PushError(
                f"{key} {where} must be one plain name: letters, digits, - and _ only, with no "
                f"/ or spaces. {NOTHING_SENT}"
            )
    return found


def check_environment() -> None:
    """Stop if a setting in the environment would send the copies somewhere other than Google.
    Names the setting, which holds no data, and never its value."""
    for name in REDIRECTS:
        if os.environ.get(name):
            raise PushError(
                f"{name} is set in your environment, which would send the copies somewhere "
                f"other than Google Cloud Storage. Unset it, then push again. {NOTHING_SENT}"
            )


@contextmanager
def holding_lock(data: Path) -> Iterator[None]:
    """Hold the scrub's own lock for the whole push, so no scrub can start, and replace the
    copies, while they're being checked and sent."""
    private = data / PRIVATE
    if is_link(private) or not private.is_dir():
        raise PushError(
            f"There are no scrubbed copies yet. Run uv run diffdata scrub first. {NOTHING_SENT}"
        )
    try:
        (private / LOCK).touch(exist_ok=False)
    except FileExistsError:
        raise PushError(
            f"A scrub or another push is running, or a scrub stopped without finishing "
            f"({PRIVATE}/{LOCK} is in your data folder). Let it finish, or run uv run diffdata "
            f"scrub again, then push. {NOTHING_SENT}"
        ) from None
    try:
        yield
    finally:
        (private / LOCK).unlink(missing_ok=True)


def read_plain(root: Path, relative: str) -> bytes | None:
    """The bytes of `relative` under `root`, or None if it's missing or isn't one plain file.
    No folder on the way may be a link, the file is opened without following a link, and a
    file with a second hard link is refused, since that link could be to a private file."""
    path = root
    for part in ["", *relative.split("/")]:
        path = path / part if part else path
        if is_link(path):
            return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        handle = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(handle, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        return file.read()


def unsafe_entry(folder: Path) -> bool:
    """Whether `folder` holds a link, or a file with a second hard link, at any depth."""
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.is_symlink() or entry.is_junction():
                return True
            if entry.is_dir(follow_symlinks=False):
                if unsafe_entry(Path(entry.path)):
                    return True
            # os.stat, not entry.stat: on Windows a DirEntry's st_nlink is always 0.
            elif os.stat(entry.path, follow_symlinks=False).st_nlink != 1:
                return True
    return False


def no_repeats(pairs: list[tuple[str, object]]) -> dict:
    """A JSON object, refusing a key given twice: a second held_back list could hide the first."""
    found = dict(pairs)
    if len(found) != len(pairs):
        raise ValueError("a key is repeated")
    return found


def read_manifest(data: Path) -> dict:
    """The last scrub's manifest, if it's exactly in the shape a scrub writes. Anything else
    stops."""
    if (data / IN_PROGRESS).exists() or is_link(data / IN_PROGRESS):
        raise PushError(
            f"A scrub stopped without finishing ({IN_PROGRESS}/ is in your data folder). Run uv "
            f"run diffdata scrub again, then push. {NOTHING_SENT}"
        )
    scrubbed = data / SCRUBBED
    if is_link(scrubbed) or (scrubbed.exists() and not scrubbed.is_dir()):
        raise PushError(
            f"{SCRUBBED}/ in your data folder is a link or isn't a folder. {NOTHING_SENT}"
        )
    if not scrubbed.is_dir():
        raise PushError(
            f"There are no scrubbed copies yet. Run uv run diffdata scrub first. {NOTHING_SENT}"
        )
    if unsafe_entry(scrubbed):
        raise PushError(
            f"{SCRUBBED}/ in your data folder holds a link, which could send a file from somewhere "
            f"else. Run uv run diffdata scrub again to rebuild it. {NOTHING_SENT}"
        )
    broken = (
        f"{SCRUBBED}/{MANIFEST} is missing or isn't in the shape a scrub writes. Run uv run "
        f"diffdata scrub again. {NOTHING_SENT}"
    )
    raw = read_plain(scrubbed, MANIFEST)
    if raw is None:
        raise PushError(broken)
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=no_repeats)
    except (UnicodeDecodeError, ValueError):  # JSONDecodeError is a ValueError
        raise PushError(broken) from None
    if not (
        isinstance(manifest, dict)
        and isinstance(manifest.get("copies"), list)
        and isinstance(manifest.get("held_back"), list)
        # Every scrub so far marks its copies not approved: until SCR-1, nothing else is right.
        and manifest.get("approved_for_release") is False
        and all(isinstance(c, dict) for c in manifest["copies"] + manifest["held_back"])
    ):
        raise PushError(broken)
    for held in manifest["held_back"]:
        if not (
            held.keys() == HELD_FIELDS
            and all(isinstance(held[k], str) for k in HELD_FIELDS)
            and held["source"] in SOURCES
        ):
            raise PushError(broken)
    for copy in manifest["copies"]:
        if not (
            copy.keys() == COPY_FIELDS
            and all(isinstance(copy[k], str) for k in ("source", "id", "path", "sha256"))
            # The checks go into the push record, and a scrub runs them all.
            and copy["checks"] == list(CHECKS)
            and isinstance(copy["flags"], int)
            and not isinstance(copy["flags"], bool)
            and SHA256.fullmatch(copy["sha256"])
        ):
            raise PushError(broken)
    return manifest


def object_name(prefix: str, copy: dict, text: str) -> str | None:
    """Where a copy goes in the store, or None if a canvas copy's header has no date."""
    path, digest = copy["path"], copy["sha256"][:16]
    if slack := SLACK_COPY.fullmatch(path):
        return f"{prefix}/slack/{slack['date']}/{slack['id']}.{digest}.md"
    canvas = CANVAS_COPY.fullmatch(path)
    changed = LAST_CHANGED.search(text.partition("\n---\n")[0])
    if changed is None:
        return None
    return f"{prefix}/slack-canvas/{changed[1]}/{canvas['id']}.{digest}.md"


def last_check(data: Path) -> Scrubber:
    """The scrub's last check, with the people list on this computer. Push runs it again on
    every copy, so a file put in scrubbed/ by hand, with a manifest entry to match, still can't
    send a name, a Slack ID or an email."""
    try:
        return Scrubber(read_people(data / PEOPLE_FILE))
    except (OSError, PeopleError):
        raise PushError(
            f"{PEOPLE_FILE} in your data folder is missing or can't be read, so the copies "
            f"can't be checked again before sending. Run uv run diffdata scrub again. "
            f"{NOTHING_SENT}"
        ) from None


def plan(data: Path, prefix: str) -> tuple[dict, list[Upload], int]:
    """Every check on the copies, before anything is sent. Returns the manifest, what to send,
    and how many canvas copies were left out for having no date. A copy is named by its place
    in the manifest, never by its path: the manifest could have been edited."""
    manifest = read_manifest(data)
    scrubbed = data / SCRUBBED
    held = {(h["source"], h["id"]) for h in manifest["held_back"]}
    scrubber = last_check(data) if manifest["copies"] else None
    uploads, seen, undated = [], set(), 0
    for number, copy in enumerate(manifest["copies"], start=1):
        which = f"Copy {number} in {SCRUBBED}/{MANIFEST}"
        path = copy["path"]
        slack, canvas = SLACK_COPY.fullmatch(path), CANVAS_COPY.fullmatch(path)
        made = (
            slack and copy["source"] == "slack" and copy["id"] == f"{slack['id']}/{slack['date']}"
        ) or (canvas and copy["source"] == "slack-canvas" and copy["id"] == canvas["id"])
        if not made:
            raise PushError(
                f"{which} isn't one a scrub makes. Run uv run diffdata scrub again. {NOTHING_SENT}"
            )
        if path in seen or (copy["source"], copy["id"]) in held:
            raise PushError(
                f"{which} is listed twice, or as both passed and held back. Run uv run diffdata "
                f"scrub again. {NOTHING_SENT}"
            )
        seen.add(path)
        content = read_plain(scrubbed, path)
        if content is None:
            raise PushError(f"{which} is missing or isn't a plain file. {NOTHING_SENT}")
        if hashlib.sha256(content).hexdigest() != copy["sha256"]:
            raise PushError(
                f"{which} changed after the scrub, so it may not be scrubbed any more. Run uv "
                f"run diffdata scrub again. {NOTHING_SENT}"
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise PushError(f"{which} isn't UTF-8 text. {NOTHING_SENT}") from None
        if scrubber.leftovers(text):
            raise PushError(
                f"{which} still holds a name, a Slack ID or an email, so it isn't a scrubbed "
                f"copy. Run uv run diffdata scrub again. {NOTHING_SENT}"
            )
        name = object_name(prefix, copy, text)
        if name is None:
            undated += 1
            continue
        uploads.append(Upload(copy, name, content))
    return manifest, uploads, undated


def open_gcs(bucket: str) -> Store:
    from diffdata.store.gcs import GCSStore  # only here, so tests never load Google's library

    return GCSStore(bucket)


def stopped(e: Exception) -> str:
    """What to say about an error from the store, true however far the push got."""
    if not isinstance(e, StoreError):
        # Its message could quote anything, so only its type is shown.
        return f"The upload stopped on an unexpected {type(e).__name__}."
    said = str(e)
    if isinstance(e, SignInError):  # its "Nothing was sent." isn't true partway
        said = said.removesuffix(" Nothing was sent.")
    return said


def push(config: Config, opener: Callable[[str], Store] | None = None) -> str:
    """Send every passed copy, create-only, then the push record. Returns the report."""
    found = settings(config)
    check_environment()
    with holding_lock(config.data_dir):
        manifest, uploads, undated = plan(config.data_dir, found.prefix)
        store = (opener or open_gcs)(found.bucket)  # signs in: only after every check passed
        results = []
        for upload in uploads:
            try:
                results.append(store.add(upload.name, upload.data, MARKDOWN))
            except Exception as e:
                raise PushError(
                    f"{stopped(e)} {results.count('added')} copy(ies) were added before it "
                    "stopped. Run again to send the rest: those already sent are skipped."
                ) from None
        pushed_at = datetime.now(UTC)
        record = {
            "tool": f"diffdata {__version__}",
            "pushed_at": pushed_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "producer": found.producer,
            "project": found.project,
            "approved_for_release": manifest["approved_for_release"],
            "copies": [
                {
                    "source": u.copy["source"],
                    "id": u.copy["id"],
                    "name": u.name,
                    "sha256": u.copy["sha256"],
                    "checks": u.copy["checks"],
                    "result": result,
                }
                for u, result in zip(uploads, results, strict=True)
            ],
        }
        body = (json.dumps(record, indent=2) + "\n").encode("utf-8")
        # Named by its time and content, so two pushes in the same second get two names, and
        # a name already taken holds this same record.
        stamp = pushed_at.strftime("%Y-%m-%dT%H-%M-%SZ")
        digest = hashlib.sha256(body).hexdigest()[:12]
        try:
            store.add(f"{found.prefix}/pushes/{stamp}-{digest}.json", body, "application/json")
        except Exception as e:
            raise PushError(
                f"{stopped(e)} {results.count('added')} copy(ies) were added, but this push's "
                "record wasn't. Run again: the copies are skipped and the record is added."
            ) from None

    added, skipped = results.count("added"), results.count("exists")
    report = [
        f"{added} scrubbed copy(ies) added to the store, and {skipped} skipped because the same "
        "content was already there."
    ]
    if held := len(manifest["held_back"]):
        report.append(f"{held} copy(ies) the scrub held back were not sent.")
    if undated:
        report.append(f"{undated} canvas copy(ies) had no last_changed date and were not sent.")
    report.append(
        "These copies are marked not approved for release, in the store too: phone numbers, "
        "meeting links and secrets aren't checked yet."
    )
    return "\n".join(report)
