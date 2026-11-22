"""`diffdata push` (STO-1, STO-2, STO-3, STO-9, STO-11): send the scrubbed copies the last
scrub passed to the team's folder in the store, and nothing else.

What to send comes only from `scrubbed/manifest.json`'s list of passed copies: an allow-list.
Nothing else in the data folder is ever read for sending, so the raw data, `private/` and
`slack/left_out.json` stay on this computer, and so would a private file added later.

Every check runs before the store is opened, so a refusal sends nothing:
  - an incomplete or wrong [store] section in config.toml
  - a scrub running, or one cut short
  - no scrubbed/ folder or manifest, or a manifest not in the shape a scrub writes
  - scrubbed/ being a link, holding one, or not being a folder
  - a listed copy that isn't a name a scrub makes, that resolves outside scrubbed/, that's
    missing, that the manifest also lists as held back, or whose content changed since the scrub

In the store, each copy goes to
    <folder>/<producer>/<project>/<source type>/<date>/<ID>.<first 16 of its SHA-256>.md
(STO-2). Identical content gets the same name and is skipped; changed content gets a new name,
beside the old one. Every add is create-only, so nothing is ever overwritten (STO-3, STO-9).
Each push then adds a record of what it sent: <folder>/<producer>/<project>/pushes/<time>.json.

Reports give counts. An error names a copy only by its path, which holds IDs only.
"""

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from diffdata import __version__
from diffdata.common.config import Config
from diffdata.scrub.run import IN_PROGRESS, LOCK, PRIVATE, SCRUBBED, holds_link, is_link
from diffdata.store.backend import Store, StoreError

MANIFEST = "manifest.json"
BACKENDS = ("gcs",)
# A folder, producer or project is one plain path segment.
SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Google Cloud Storage's bucket names: lowercase letters, digits, dots, - and _.
BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]")
# The copies a scrub makes (scrub/run.py), and nothing else.
SLACK_COPY = re.compile(r"slack/export/(?P<id>[A-Z0-9]+)/(?P<date>\d{4}-\d{2}-\d{2})\.md")
CANVAS_COPY = re.compile(r"documents/current/slack-canvas_(?P<id>[A-Z0-9]+)\.md")
SHA256 = re.compile(r"[0-9a-f]{64}")
# A canvas copy's date is the day it last changed, from its header.
LAST_CHANGED = re.compile(r"^last_changed: (\d{4}-\d{2}-\d{2})", re.MULTILINE)
MARKDOWN = "text/markdown; charset=utf-8"
NOTHING_SENT = "Nothing was sent."


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
    if not BUCKET.fullmatch(found.bucket):
        raise PushError(
            f"bucket {where} isn't a Google Cloud Storage bucket name: lowercase letters, digits, "
            f"dots, - and _. {NOTHING_SENT}"
        )
    for key in ("folder", "producer", "project"):
        if not SEGMENT.fullmatch(getattr(found, key)):
            raise PushError(
                f"{key} {where} must be one plain name: letters, digits, - and _ only, with no "
                f"/ or spaces. {NOTHING_SENT}"
            )
    return found


def read_manifest(data: Path) -> dict:
    """The last scrub's manifest, if it's in the shape a scrub writes. Anything else stops."""
    if (data / PRIVATE / LOCK).exists() or (data / IN_PROGRESS).exists():
        raise PushError(
            f"A scrub is running, or one stopped without finishing ({PRIVATE}/{LOCK} or "
            f"{IN_PROGRESS}/ is in your data folder). Let it finish, or run uv run diffdata scrub "
            f"again, then push. {NOTHING_SENT}"
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
    if holds_link(scrubbed):
        raise PushError(
            f"{SCRUBBED}/ in your data folder holds a link, which could send a file from somewhere "
            f"else. Run uv run diffdata scrub again to rebuild it. {NOTHING_SENT}"
        )
    broken = (
        f"{SCRUBBED}/{MANIFEST} is missing or isn't in the shape a scrub writes. Run uv run "
        f"diffdata scrub again. {NOTHING_SENT}"
    )
    try:
        manifest = json.loads((scrubbed / MANIFEST).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise PushError(broken) from None
    if not (
        isinstance(manifest, dict)
        and isinstance(manifest.get("copies"), list)
        and isinstance(manifest.get("held_back"), list)
        and isinstance(manifest.get("approved_for_release"), bool)
        and all(isinstance(c, dict) for c in manifest["copies"] + manifest["held_back"])
    ):
        raise PushError(broken)
    for copy in manifest["copies"]:
        if not (
            all(isinstance(copy.get(k), str) for k in ("source", "id", "path", "sha256"))
            and isinstance(copy.get("checks"), list)
            and SHA256.fullmatch(copy["sha256"])
        ):
            raise PushError(broken)
    return manifest


def object_name(prefix: str, copy: dict, data: bytes) -> str | None:
    """Where a copy goes in the store, or None if a canvas copy's header has no date."""
    path, digest = copy["path"], copy["sha256"][:16]
    if slack := SLACK_COPY.fullmatch(path):
        return f"{prefix}/slack/{slack['date']}/{slack['id']}.{digest}.md"
    canvas = CANVAS_COPY.fullmatch(path)
    changed = LAST_CHANGED.search(data.decode("utf-8").partition("\n---\n")[0])
    if changed is None:
        return None
    return f"{prefix}/slack-canvas/{changed[1]}/{canvas['id']}.{digest}.md"


def plan(data: Path, prefix: str) -> tuple[dict, list[Upload], int]:
    """Every check on the copies, before anything is sent. Returns the manifest, what to send,
    and how many canvas copies were left out for having no date."""
    manifest = read_manifest(data)
    scrubbed = (data / SCRUBBED).resolve()
    held = {(h.get("source"), h.get("id")) for h in manifest["held_back"]}
    uploads, seen, undated = [], set(), 0
    for copy in manifest["copies"]:
        path = copy["path"]
        slack, canvas = SLACK_COPY.fullmatch(path), CANVAS_COPY.fullmatch(path)
        made = (
            slack and copy["source"] == "slack" and copy["id"] == f"{slack['id']}/{slack['date']}"
        ) or (canvas and copy["source"] == "slack-canvas" and copy["id"] == canvas["id"])
        if not made:
            # Its path isn't quoted: it isn't one a scrub makes, so it could hold anything.
            raise PushError(
                f"{SCRUBBED}/{MANIFEST} lists a copy that isn't one a scrub makes. Run uv run "
                f"diffdata scrub again. {NOTHING_SENT}"
            )
        if path in seen or (copy["source"], copy["id"]) in held:
            raise PushError(
                f"{SCRUBBED}/{MANIFEST} lists {path} twice, or as both passed and held back. Run "
                f"uv run diffdata scrub again. {NOTHING_SENT}"
            )
        seen.add(path)
        file = scrubbed / path
        if not file.resolve().is_relative_to(scrubbed) or not file.is_file() or is_link(file):
            raise PushError(f"{SCRUBBED}/{path} is missing or isn't a plain file. {NOTHING_SENT}")
        content = file.read_bytes()
        if hashlib.sha256(content).hexdigest() != copy["sha256"]:
            raise PushError(
                f"{SCRUBBED}/{path} changed after the scrub, so it may not be scrubbed any more. "
                f"Run uv run diffdata scrub again. {NOTHING_SENT}"
            )
        try:
            name = object_name(prefix, copy, content)
        except UnicodeDecodeError:
            raise PushError(f"{SCRUBBED}/{path} isn't UTF-8 text. {NOTHING_SENT}") from None
        if name is None:
            undated += 1
            continue
        uploads.append(Upload(copy, name, content))
    return manifest, uploads, undated


def open_gcs(bucket: str) -> Store:
    from diffdata.store.gcs import GCSStore  # only here, so tests never load Google's library

    return GCSStore(bucket)


def push(config: Config, opener: Callable[[str], Store] | None = None) -> str:
    """Send every passed copy, create-only, then the push record. Returns the report."""
    found = settings(config)
    manifest, uploads, undated = plan(config.data_dir, found.prefix)
    store = (opener or open_gcs)(found.bucket)  # signs in: only after every check passed

    results = []
    for upload in uploads:
        try:
            results.append(store.add(upload.name, upload.data, MARKDOWN))
        except StoreError as e:
            added = results.count("added")
            raise PushError(
                f"{e} {added} copy(ies) were added before it stopped. Run again to send the rest: "
                "those already sent are skipped."
            ) from None
    pushed_at = datetime.now(UTC)
    record = {
        "tool": f"diffdata {__version__}",
        "pushed_at": pushed_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "producer": found.producer,
        "project": found.project,
        "approved_for_release": manifest["approved_for_release"],
        "note": manifest.get("note", ""),
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
    stamp = pushed_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    try:
        for attempt in range(1, 6):
            suffix = "" if attempt == 1 else f"-{attempt}"
            if (
                store.add(f"{found.prefix}/pushes/{stamp}{suffix}.json", body, "application/json")
                == "added"
            ):
                break
    except StoreError as e:
        raise PushError(f"{e} The copies were sent, but this push's record wasn't.") from None

    added, skipped = results.count("added"), results.count("exists")
    report = [
        f"{added} scrubbed copy(ies) added to the store, and {skipped} skipped because the same "
        "content was already there."
    ]
    if held := len(manifest["held_back"]):
        report.append(f"{held} copy(ies) the scrub held back were not sent.")
    if undated:
        report.append(f"{undated} canvas copy(ies) had no last_changed date and were not sent.")
    if not manifest["approved_for_release"]:
        report.append(
            "These copies are marked not approved for release, in the store too: phone numbers, "
            "meeting links and secrets aren't checked yet."
        )
    return "\n".join(report)
