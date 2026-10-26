---
source: slack
sources: 14
places: 17
tool: diffdata VERSION
---

# References

Every source the saved Slack data points to, once each, with every place it was referenced. diffdata builds this from data already saved, and fetches nothing here. "Edited out" means the latest saved read of that message or canvas no longer has the link.

- Slack canvases: 3 (1 collected, 2 new)
- Google Docs: 1 (1 new)
- Google Sheets: 1 (1 new)
- Google Slides: 1 (1 new)
- Google Drive files: 1 (1 new)
- Slack attachments: 3 (3 not collected)
- Other links: 4 (4 not collected)

## Slack canvases

### Example Plan Canvas

Collected.

- Key: `slack-file:F00000010`
- Link: https://example.slack.com/docs/T00000000/F00000010
- Referenced from:
  - 2026-10-07 15:00 UTC · #example-channel · Sam Sample · link · [message](https://example.slack.com/archives/C00000001/p1791385200000600)

### Team notes

New: not listed in config.toml.

- Key: `slack-file:F00000013`
- File type: canvas
- Referenced from:
  - 2026-10-07 15:00 UTC · #example-channel · Sam Sample · attachment · [message](https://example.slack.com/archives/C00000001/p1791385200000600)

### slack-file:F00000014

New: not listed in config.toml.

- Key: `slack-file:F00000014`
- Referenced from:
  - 2026-10-07 16:00 UTC · #example-channel · Slackbot · tab notice · [message](https://example.slack.com/archives/C00000001/p1791388800000700)

## Google Docs

### Draft plan

New: not listed in config.toml.

- Key: `google:FAKE-DOC-0010`
- Link: https://docs.google.com/document/d/FAKE-DOC-0010/edit#heading=h.fake01
- Referenced from:
  - 2026-10-06 14:00 UTC · #example-channel · Alex Example · link · [message](https://example.slack.com/archives/C00000001/p1791295200000100)
  - 2026-10-06 14:30 UTC · #example-channel · Sam Sample · link · [message](https://example.slack.com/archives/C00000001/p1791297000000110?thread_ts=1791295200.000100&cid=C00000001)
  - 2026-10-08 12:03 UTC · canvas "Example Plan Canvas" · link

## Google Sheets

### Budget sheet

New: not listed in config.toml.

- Key: `google:FAKE-SHEET-0010`
- Link: https://docs.google.com/spreadsheets/d/FAKE-SHEET-0010/edit#gid=0
- Referenced from:
  - 2026-10-06 15:00 UTC · #example-channel · Sam Sample · link · [message](https://example.slack.com/archives/C00000001/p1791298800000300)

## Google Slides

### Review slides

New: not listed in config.toml.

- Key: `google:FAKE-SLIDES-0010`
- Link: https://docs.google.com/presentation/d/FAKE-SLIDES-0010/edit
- Referenced from:
  - 2026-10-06 15:00 UTC · #example-channel · Sam Sample · link · [message](https://example.slack.com/archives/C00000001/p1791298800000300)

## Google Drive files

### https://drive.google.com/file/d/FAKE-FILE-0010/view?usp=sharing

New: not listed in config.toml.

- Key: `google:FAKE-FILE-0010`
- Link: https://drive.google.com/file/d/FAKE-FILE-0010/view?usp=sharing
- Referenced from:
  - 2026-10-06 16:00 UTC · #example-channel · Riley Placeholder · link · [message](https://example.slack.com/archives/C00000001/p1791302400000400)

## Slack attachments

### https://example.slack.com/files/U00000002/F00000011/review.pdf

Not collected: attachments are never downloaded.

- Key: `slack-file:F00000011`
- Link: https://example.slack.com/files/U00000002/F00000011/review.pdf
- Referenced from:
  - 2026-10-07 15:00 UTC · #example-channel · Sam Sample · link · [message](https://example.slack.com/archives/C00000001/p1791385200000600)

### meeting-recording.mp4

Not collected: attachments are never downloaded.

- Key: `slack-file:F00000012`
- File type: video/mp4
- Referenced from:
  - 2026-10-07 15:00 UTC · #example-channel · Sam Sample · attachment · [message](https://example.slack.com/archives/C00000001/p1791385200000600)

### chart.png

Not collected: attachments are never downloaded.

- Key: `slack-file:F00000015`
- Link: https://example.slack.com/files/U00000001/F00000015/chart.png
- Referenced from:
  - 2026-10-08 12:03 UTC · canvas "Example Plan Canvas" · link

## Other links

### the shared folder

Not collected: ordinary links are logged only.

- Key: `url:https://drive.google.com/drive/folders/FAKE-FOLDER-0010`
- Link: https://drive.google.com/drive/folders/FAKE-FOLDER-0010
- Referenced from:
  - 2026-10-06 16:00 UTC · #example-channel · Riley Placeholder · link · [message](https://example.slack.com/archives/C00000001/p1791302400000400)

### https://code.example.com/example/repo

Not collected: ordinary links are logged only.

- Key: `url:https://code.example.com/example/repo`
- Link: https://code.example.com/example/repo
- Referenced from:
  - 2026-10-07 14:00 UTC · #example-channel · Alex Example · link · [message](https://example.slack.com/archives/C00000001/p1791381600000500)

### A paper

Not collected: ordinary links are logged only.

- Key: `url:https://example.org/paper?id=7`
- Link: https://example.org/paper?id=7#results
- Referenced from:
  - 2026-10-07 14:00 UTC · #example-channel · Alex Example · link · [message](https://example.slack.com/archives/C00000001/p1791381600000500)
  - 2026-10-08 12:03 UTC · canvas "Example Plan Canvas" · link

### the first post

Not collected: ordinary links are logged only.

- Key: `url:https://example.slack.com/archives/C00000001/p1791295200000100`
- Link: https://example.slack.com/archives/C00000001/p1791295200000100
- Referenced from:
  - 2026-10-07 14:00 UTC · #example-channel · Alex Example · link · [message](https://example.slack.com/archives/C00000001/p1791381600000500)
