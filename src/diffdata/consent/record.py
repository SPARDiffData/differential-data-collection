"""The consent record: what each producer agreed to share, with whom, and for what (CON-2, CON-3).

Under the GDPR our lawful basis is consent (Art. 6(1)(a)). Consent has to be specific and
informed, we have to be able to show it was given (Art. 7(1)), and withdrawing it has to be
as easy as giving it (Art. 7(3)). This module keeps that proof.

Layout under the data folder:
  consent/ledger.jsonl    every grant and revoke, one JSON object per line, in the order
                          they happened. Lines are only ever added, never changed or
                          removed, so the ledger shows who agreed to what, and when.

Rules:
- A grant names one producer, the sources they share, the consumers allowed to download
  them (CON-6), the uses those consumers may make of them, and the version of the consent
  wording the producer saw. A new grant from the same producer replaces their last one.
- A revoke stops a source on the next run (CON-3). It doesn't undo the past: data
  collected before it was collected lawfully (Art. 7(3)). Deleting what's already stored
  is a separate step (STO-7).
- A source is only collected if the producer's current grant lists it and no later revoke
  names it. Anything the ledger doesn't cover counts as no consent.
- The ledger holds the producer's name, so it's personal data. It lives in the data folder,
  outside any repo, like everything else we collect.
- Times are UTC, written like 2026-10-13T14:00:00Z.

Every line has `type` ("grant" or "revoke"), `record_id`, `producer` and `at`. By type:
  grant    Adds `sources`, `consumers`, `uses` and `policy_version`.
  revoke   Adds `sources`: the sources this producer withdraws.
"""

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

LEDGER = Path("consent") / "ledger.jsonl"
# The consent wording a producer agrees to. Bump it when the wording changes, so each
# grant shows which wording it agreed to. The wording itself is still a draft.
POLICY_VERSION = "draft-2026-10"
# Purpose limitation (Art. 5(1)(b)): consumers may only use data for the uses named here,
# and only for the ones the producer picked. Consent must name training plainly, because it
# can't be undone once a model is trained (Oct 8 call).
USES = {
    "research": "Read and analyse the data for AI safety research.",
    "evaluation": "Build or run evaluations of AI models from the data.",
    "training": "Train or fine-tune AI models on the data. This can't be undone.",
}
# A source is <kind>:<id>, like slack:example-channel or gdoc:1AbC... Kinds are letters,
# digits and -, so a source reads the same in every grant, revoke and check.
SOURCE = re.compile(r"[a-z][a-z0-9-]*:[^\s]+")
TYPES = ("grant", "revoke")


class ConsentError(Exception):
    """A grant or revoke is incomplete, or the ledger can't be read."""


@dataclass(frozen=True)
class Grant:
    producer: str
    sources: tuple[str, ...]
    consumers: tuple[str, ...]
    uses: tuple[str, ...]
    at: str
    policy_version: str
    record_id: str


@dataclass(frozen=True)
class Consent:
    """A producer's consent as it stands now: their last grant, less later revokes."""

    grant: Grant
    revoked: tuple[str, ...]

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(s for s in self.grant.sources if s not in self.revoked)


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def source_name(value: str) -> str:
    """A source as the ledger writes it. Kinds are lowercase; Slack channels are too."""
    kind, sep, rest = value.strip().partition(":")
    kind = kind.lower()
    if kind == "slack":
        rest = rest.lstrip("#").lower()
    name = f"{kind}{sep}{rest}"
    if not SOURCE.fullmatch(name):
        raise ConsentError(
            f"{value!r} isn't a source. Write it as <kind>:<id>, like slack:example-channel."
        )
    return name


def names(values, what: str) -> tuple[str, ...]:
    """Clean, de-duplicated, in the order given. At least one is needed."""
    cleaned = tuple(dict.fromkeys(v.strip() for v in values if v.strip()))
    if not cleaned:
        raise ConsentError(f"A grant needs at least one {what}.")
    return cleaned


def ledger_path(data: Path) -> Path:
    return data / LEDGER


def append(data: Path, line: dict) -> None:
    """Add one line to the ledger. Lines already there are never touched."""
    path = ledger_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def grant(data: Path, producer: str, sources, consumers, uses, at: str | None = None) -> Grant:
    """Record a producer's consent (CON-2). It replaces any grant they gave before."""
    producer = producer.strip()
    if not producer:
        raise ConsentError("A grant needs the producer's name.")
    uses = names(uses, "use")
    unknown = [u for u in uses if u not in USES]
    if unknown:
        raise ConsentError(f"Unknown use: {', '.join(unknown)}. The uses are: {', '.join(USES)}.")
    record = Grant(
        producer=producer,
        sources=names((source_name(s) for s in sources), "source"),
        consumers=names(consumers, "consumer"),
        uses=uses,
        at=at or now(),
        policy_version=POLICY_VERSION,
        record_id=uuid.uuid4().hex,
    )
    append(data, {"type": "grant", **asdict(record)})
    return record


def revoke(data: Path, producer: str, sources, at: str | None = None) -> tuple[str, ...]:
    """Withdraw sources from a producer's consent (CON-3). Collection stops on the next run."""
    producer = producer.strip()
    current = consents(data).get(producer)
    if current is None:
        raise ConsentError(f"{producer} has no consent on record, so there's nothing to revoke.")
    withdrawn = tuple(dict.fromkeys(source_name(s) for s in sources))
    if not withdrawn:
        raise ConsentError("Name at least one source to revoke.")
    missing = [s for s in withdrawn if s not in current.sources]
    if missing:
        raise ConsentError(f"{producer} isn't sharing {', '.join(missing)} now.")
    line = {
        "type": "revoke",
        "record_id": uuid.uuid4().hex,
        "producer": producer,
        "at": at or now(),
        "sources": list(withdrawn),
    }
    append(data, line)
    return withdrawn


def read_ledger(data: Path) -> list[dict]:
    """Every line of the ledger, oldest first. No ledger means no consent yet."""
    path = ledger_path(data)
    if not path.is_file():
        return []
    lines = []
    for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not text.strip():
            continue
        try:
            line = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConsentError(f"Line {number} of {path} isn't JSON: {e}") from e
        if not isinstance(line, dict) or line.get("type") not in TYPES:
            raise ConsentError(f"Line {number} of {path} isn't a grant or a revoke.")
        lines.append(line)
    return lines


def consents(data: Path) -> dict[str, Consent]:
    """Each producer's consent as it stands now, by producer name."""
    current: dict[str, Consent] = {}
    for line in read_ledger(data):
        producer = line["producer"]
        if line["type"] == "grant":
            fields = {name: line[name] for name in Grant.__dataclass_fields__}
            for name in ("sources", "consumers", "uses"):
                fields[name] = tuple(fields[name])
            current[producer] = Consent(grant=Grant(**fields), revoked=())
        elif producer in current:
            consent = current[producer]
            revoked = tuple(dict.fromkeys((*consent.revoked, *line["sources"])))
            current[producer] = Consent(grant=consent.grant, revoked=revoked)
    return current


def allowed(data: Path, source: str) -> bool:
    """Whether any producer's current consent covers this source. No record means no."""
    source = source_name(source)
    return any(source in c.sources for c in consents(data).values())


def summary(data: Path) -> str:
    """Who is sharing what now, in plain words, for `diffdata consent show`."""
    current = consents(data)
    if not current:
        return f"No consent on record in {ledger_path(data)}."
    blocks = []
    for producer, consent in sorted(current.items()):
        g = consent.grant
        lines = [
            f"{producer} (agreed {g.at}, wording {g.policy_version})",
            f"  sharing:   {', '.join(consent.sources) or 'nothing'}",
            f"  consumers: {', '.join(g.consumers)}",
            f"  uses:      {', '.join(g.uses)}",
        ]
        if consent.revoked:
            lines.append(f"  revoked:   {', '.join(consent.revoked)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
