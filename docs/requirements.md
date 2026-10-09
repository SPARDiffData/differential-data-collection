# Requirements v0.1

*Draft for team edits, October 8. v0 was built from the Project Plan (SPAR scope), the Tech Notes architecture, the Theory of Change One-Pager, our Alignment Hive review and Lachlan's Google Docs PoC. v0.1 adds Paul's review: Slack becomes the way we discover sources, Slack collection moves up, and the never-modify-originals rule is added. IDs stay stable once used in Issues.*

**Priority:** **M** = needed for the midterm (Nov 2) · **F** = needed for the final (Dec 14) · **L** = later / full scope, noted so we design for it. Priorities will move as we dogfood. When something blocks us, we look for an MVP workaround before pulling a bigger requirement forward.

## 1. Scope and phases

We build the **Produce, Collect and Store** layers, dogfooding on our own SPAR team's data first and then other SPAR projects. Our own channel already holds every kind of data we care about, so we can move fast and make mistakes without real producers at risk. **Recall** is a raw download for approved users. **Consumption** (training or evaluating models) is out of scope.

| Phase | When | Outcome |
|---|---|---|
| v0.0.x | this week | Requirements, a plan for the first parts, and a bare repo the whole team can work in. High-level architecture known. |
| v0.1.x | ~2 weeks | Starting from our Slack channel, find and collect our messages, canvases, docs and transcripts. Usable by the team on Windows and macOS. Big temporary tradeoffs are fine. |
| v0.2.x | ~1 month | More formats and scenarios, sturdier code, and the first real privacy and security work. Ready to explain to other SPAR projects. |
| v0.3.x | Dec 14 | A working system that gathers, collects and stores a variety of inputs in a way that's useful to the producer. |

**Midterm target (strawman):** our Slack channel works end to end: discover, collect, scrub, store.

## 2. Principles

* **HARD RULE: never modify or delete a producer's original files or data, locally or in any cloud service.** We read, and we only ever change our own copies. Use read-only access scopes wherever a service offers them.
* **Don't reinvent the wheel.** Our core product is finding, collecting, organizing and storing. For format-specific work (Docs history, Slack exports, transcript formats) use existing libraries and tools first.
* **Move fast and iterate.** We are our own first users, so feedback is immediate. Prefer a rough version that works today over a polished part next month.
* **Log every reference, pull once.** The same doc can be linked from a chat, a canvas and a calendar invite. We record every place it's referenced and fetch it once.

## 3. Terms

* **Producer:** the person or team whose working data we collect.
* **Consumer:** an approved researcher or org that downloads stored data.
* **Source:** where data lives (a Slack channel, a canvas, a Google Doc or Sheet, a transcript folder).
* **Reference:** a place where a source is mentioned or linked (a message, a canvas, a bookmark).
* **Artifact:** one collected item (a channel export, a doc with its history, a transcript).
* **Raw:** exactly as fetched. **Scrubbed:** after redaction. **Released:** scrubbed, reviewed and in central storage.

## 4. Flow

1. **Connect:** the producer chooses what to share (for the pilot: our channels, listed in a config file).
2. **Discover:** read Slack messages and threads, log them as artifacts, and extract every link and file they reference (Google Docs and Sheets, canvases, recordings, transcripts, notes). Also check fixed places, like a transcript folder.
3. **Collect:** fetch each referenced source we can access. If we can't, keep the reference and log why.
4. **Scrub:** redact secrets and personal information, and flag anything that still looks sensitive.
5. **Organize:** put everything into a clean hierarchy and link related items (raw and processed versions, a recording and its transcript).
6. **Review:** the producer can inspect and exclude anything before release.
7. **Store:** push to central storage on a schedule, adding and never overwriting.

## 5. Requirements

### 5.1 Connect and consent (lane: Paul)

| ID | Requirement | Pri |
|---|---|---|
| CON-1 | Basic opt-in: a config file lists the channels and folders to collect. For the pilot, team agreement is the consent. | M |
| CON-2 | Consent is stored as a record: producer, sources, allowed consumers, allowed uses, date, policy version. | F |
| CON-3 | A producer can revoke a source at any time. Collection stops on the next run. | M |
| CON-4 | A producer can list exclusions (projects, people, keywords). Scrub uses them. | F |
| CON-5 | A channel or meeting with other people is collected only if every participant has agreed. Anyone can ask to be excluded. | F |
| CON-6 | Approved consumers are chosen per producer. A new consumer needs the producer's approval. | F |

### 5.2 Discover (lane: Paul)

| ID | Requirement | Pri |
|---|---|---|
| DIS-1 | Export a Slack channel's messages and thread replies as markdown, incrementally. For the pilot, each person's own Claude session reads Slack through its Slack connector and hands the export to the Python code. | M |
| DIS-6 | Direct Slack API access (a Slack app or user token), so collection can run without a Claude session. | F |
| DIS-2 | Extract every link and file from messages, threads, canvases and channel bookmarks into a reference log: what, where it was referenced, by whom, when. | M |
| DIS-3 | Deduplicate references so each source is collected once. | M |
| DIS-4 | Check fixed places on a schedule (a local or network transcript folder). | F |
| DIS-5 | Other discovery routes (a shared calendar, Zoom cloud recordings). | L |

### 5.3 Collect (lanes: Paul for the core, Lachlan for source detail)

| ID | Requirement | Pri |
|---|---|---|
| COL-1 | Every source type has an adapter behind one common interface (fetch, fetch-since). Adding a source means adding an adapter. | M |
| COL-2 | **Slack canvases:** current version. | M |
| COL-3 | **Slack canvases:** comments and version history. | F |
| COL-4 | **Google Docs:** current version, fetched by our tool running locally with the producer's own Google login (Oct 8 call). Sharing a doc with a project account stays available as a test harness (Lachlan's PoC). | M |
| COL-5 | **Google Docs:** revision history, comments and suggestions (Lachlan's PoC). | M |
| COL-6 | **Google Sheets** with revision history; **meeting transcripts and recordings**; **markdown notes** from meetings and interviews. | F |
| COL-7 | An inaccessible source keeps its reference, with the reason (no access, deleted, unsupported type). | M |
| COL-8 | Collection is incremental and safe to re-run. How often we re-check a known source is configurable. | M |
| COL-9 | Where native history isn't available, keep a snapshot at each pull so the evolution still shows. Reconcile snapshots with native history where both exist. | F |
| COL-10 | Every artifact carries basic tags: producer, org, project, source, type, created and modified dates, collected-at, tool version. | M |
| COL-11 | AI agent logs (Claude Code sessions). Try Alignment Hive's tooling first. | L |

### 5.4 Organize and history (lane: Lachlan)

| ID | Requirement | Pri |
|---|---|---|
| ORG-1 | A clean local hierarchy by source type, with raw and processed copies side by side. | M |
| ORG-2 | Normalize text to a common format: markdown for content, plus a JSON event log for edits, comments and suggestions. | M |
| ORG-3 | Link related artifacts by ID (raw and processed versions, a recording and its transcript, a doc and the thread that discussed it). | F |
| HIS-1 | Batch tiny edits into meaningful changes, and group changes into time windows. | F |
| HIS-2 | Assemble a narrative of what changed, when and why. | L |

### 5.5 Scrub (lane: Ananjay)

| ID | Requirement | Pri |
|---|---|---|
| SCR-1 | Redact secrets (API keys, tokens, passwords) with a known rule set, such as gitleaks rules, plus a high-entropy check. | M |
| SCR-2 | Redact or pseudonymize personal information (names, emails, phone numbers). The same person gets the same placeholder across artifacts. A basic version is needed by the midterm; its scope is open. | M |
| SCR-3 | Flag anything uncertain for producer review rather than dropping it. | F |
| SCR-4 | Apply the producer's exclusions (CON-4). | F |
| SCR-5 | Scrubbing is deterministic and logged. | F |
| SCR-6 | Scrub runs at collection, and again as an audit in storage. | F |

### 5.6 Review (lane: Paul, in the consent package)

| ID | Requirement | Pri |
|---|---|---|
| REV-1 | The producer can read the scrubbed version of everything before release, and exclude any artifact. | F |
| REV-3 | A way for producers to give us feedback once other SPAR projects use the tool. | L |
| REV-2 | Unflagged data releases after a review window (24 h like Alignment Hive, or 7 days). Flagged items wait for approval. | F |

### 5.7 Store and recall (lane: not assigned yet)

| ID | Requirement | Pri |
|---|---|---|
| STO-1 | Pilot store: the SPARDiffData Google Drive, with read/write for the team. | M |
| STO-2 | Folder structure: producer, then project, then source type, then date. | M |
| STO-3 | Pushes run on a schedule and are additive: never overwrite. How to handle conflicts is open. | M |
| STO-9 | Expect several people to collect the same source. An upload whose content already exists in storage is skipped (content hash). | M |
| STO-10 | The same source with different content from different collectors is stored as separate versions, linked by source ID, and reconciled later. | F |
| STO-11 | Storage sits behind one interface, so we can move from Google Drive to another service (likely AWS) without touching the other stages. | M |
| STO-4 | Encrypted in transit and at rest. Fine to rely on the provider's defaults. | F |
| STO-5 | Access per consumer, limited to producers who approved them (CON-6). | F |
| STO-6 | Every access and change is logged. | F |
| STO-7 | Deletion on request removes the data and notifies consumers who already accessed it. | F |
| STO-8 | US-only storage (open). | F |
| REC-1 | Approved users download scrubbed data. No query interface or UI. | F |

### 5.8 Engineering (lane: Paul, repo)

| ID | Requirement | Pri |
|---|---|---|
| ENG-1 | Python CLI with a config file, running on Windows and macOS. No separate UI for now: people share sources by posting them in Slack, and a Claude Code command in the repo runs the Slack step. | M |
| ENG-2 | No real data or credentials in the repo. Tests use synthetic fixtures only. Secrets live in `.env`, and push protection stays on. | M |
| ENG-3 | One package per stage (discover, collect, organize, scrub, store), so each lane can work without blocking the others. | M |
| ENG-4 | Semantic versions (0.MINOR.PATCH, matching the phases above), tagged on GitHub with release notes. | M |
| ENG-5 | Work happens on short-lived branches and merges through pull requests. Review policy is open. | M |
| ENG-6 | Tests and a linter run on every pull request. | F |
| ENG-7 | A local log of every action, kept for a set number of days. | F |

## 6. Design questions to settle

* **Staging or streaming?** Alignment Hive scrubs in memory at upload and never writes a second copy. Our flow keeps local copies, which gives an auditable trail but more raw data to protect.
* **Slack access:** Claude sessions as the connector for now. Which route do we take next: a Slack app (likely needs SPAR admin approval), a user token, or something else?
* **Collector access:** leaning to each producer's own login through the local tool (Oct 8 call). What scopes do we request, and how do we keep them read-only?
* **Re-checking known sources:** how often, and how to reconcile repeated pulls with native version history (which may only go back so far)?
* **Storage conflicts:** what happens when two people push the same artifact?
* **Consumer terms:** answered for now (Oct 8 call): we don't exclude frontier-lab training. Alignment Hive bars it, and a consumer like Caspar may want to train on the data.
* **Value proposition:** lead with access to local collection, or with improving specific AI safety initiatives?

## 7. Proposed first Issues

1. Repo skeleton, `.env.example`, config file, and the first release tag (ENG-1 to ENG-5).
2. Slack channel export to markdown, messages and threads (DIS-1).
3. Reference log and link extraction (DIS-2, DIS-3, COL-7).
4. Canvas and Google Doc current versions (COL-2, COL-4).
5. Push to the shared Drive (STO-1 to STO-3).

**In parallel:** shared tags and the common format (COL-10, ORG-2); Google Docs history from Lachlan's PoC (COL-5); scrubbing v0 for secrets (SCR-1).
