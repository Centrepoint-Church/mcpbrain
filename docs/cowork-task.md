# Claude Cowork scheduled task: `mcpbrain-enrichment`

This is the exact text to paste into Cowork's *Create new task* form. It's
user-agnostic — Cowork runs as you, so `~/.mcpbrain/` resolves to the right
directory on every machine.

Steps to install:

1. Open the Claude desktop app and go to **Cowork**.
2. Click **Scheduled**, then **Create new task**.
3. Copy the **Name**, **Description**, and **Task** below into the form.
4. Set the frequency to **hourly** and save.

Cowork tasks only run while the machine is awake and the Claude desktop app is
open.

---

## Name

```
mcpbrain-enrichment
```

## Description

```
Reads a batch of email threads from ~/.mcpbrain/enrich_queue/pending.json,
extracts structured knowledge (entities, actions, relations, org tags), and
writes the result to ~/.mcpbrain/enrich_inbox/<batch_id>.json. No database
access, no Gmail — two files in and out. Run when pending.json appears; the
mcpbrain daemon handles everything else.
```

## Task

> The body below is the contents of [`mcpbrain/enrich_prompt.md`](../mcpbrain/enrich_prompt.md)
> prepended with one line that tells Cowork where to find the spool. If you
> change the prompt, edit `enrich_prompt.md` (the canonical source) and copy
> the new body back into this task.

```
The base directory for all file operations is ~/.mcpbrain/ (your home dir's
.mcpbrain folder). All paths below are relative to that base.

You are extracting structured knowledge from a batch of email threads for an
operations manager who works across several organisations. You read one input
file and write one output file. You touch nothing else: no database, no Gmail,
no marking, no sending. Two files, in and out.

The two files

Input: enrich_queue/pending.json. It carries a batch_id, a context
block, a list of threads, and a merge_review list (often empty).
Output: enrich_inbox/<batch_id>.json, where <batch_id> is the
batch_id from the input, verbatim. Write valid JSON only.

The task

Produce exactly one extraction per thread in threads. Read each message's
text body to do the extraction. Then assemble the output file:

{"batch_id": "batch-...",
 "extractions": [ <one extraction envelope per thread> ],
 "merge_answers": [ <one answer per merge_review pair, or []> ],
 "synthesis": [ <one answer per synthesis request, only if pending.json carries them> ]}

merge_answers and synthesis are present only when pending.json carries a
merge_review or synthesis block respectively; otherwise omit them or use [].

If a thread carries part and of keys (a long thread split across parts),
still emit one extraction for that part; the daemon regroups parts by
thread_id before applying. Keep the same thread_id on every part.

The extraction envelope

Each extraction uses this schema verbatim. Match the field names exactly.

{
  "thread_id": "t-abc123",
  "org": "<one of the valid_orgs tags>",
  "content_type": "request|update|decision|fyi|notification",
  "summary": "One plain sentence.",
  "contextual_summary": "Optional longer situational summary, or omit it.",
  "entities": [{"name": "Person Name", "type": "person|org|project",
                "org": "<org tag>", "role": "Job title"}],
  "topics": ["facilities", "worship"],
  "actions": [{"description": "...", "owner_name": "Person Name",
               "owner_fallback": "sender", "due_date": "YYYY-MM-DD",
               "project_id": "your-project-id", "area_id": "your-area-id",
               "waiting_on": "Other Person"}],
  "reply_needed": true,
  "reply_reason": "Direct question: 'can you confirm Hall B?'",
  "resolved_action_ids": [42],
  "updated_actions": [{"id": 42, "new_text": "..."}],
  "relations": [{"source_name": "Person Name", "type": "works_at",
                 "target_name": "Org Name"}],
  "messages": [{"message_id": "m-1",
                "sender": "Person Name <addr@example.com>",
                "date": "YYYY-MM-DD", "labels": "INBOX", "subject": "..."}]
}

Field notes:

thread_id: copy the thread's thread_id exactly.
org: one of the tags in the context block's valid_orgs list (the configured
org names plus external and unknown). Use org_domain_map to map sender
domains to an org. Use unknown only when nothing supports a choice.
content_type: one of request, update, decision, fyi, notification.
summary: one plain sentence. contextual_summary is optional; leave it as an
empty string when there is nothing situational to add.
entities, topics, actions, relations: lists. Empty lists are fine.
waiting_on (on an action): optional. Set it to the name of the person the
action is awaiting a reply or input from. Use the person's bare name, matching
an entity you listed. Omit it for actions that are not blocked on someone's
reply.
messages: provenance only. For each message in the thread emit message_id,
sender, date, labels, subject. Do NOT include the message text body in the
output. You read the body; you do not echo it.

Using the standing context

The context block is given so you don't re-derive what is already known.

known_people: each entry's org and role are confirmed. Trust them. Do not
re-derive a person's org or role, and do not contradict these entries. Trust
them even when the sender's email domain is absent from org_domain_map.
valid_orgs: the org tags this install classifies against — the configured
org names plus external and unknown. The thread-level org and any org tag
you assign must come from this list.
org_domain_map: maps email domains to orgs. Use it to set org and to decide
whether a sender is internal or external.
projects and areas: the valid project_id and area_id sets. When you attach a
project_id or area_id to an action, it must be an id drawn from these lists.
If no listed id fits, leave the field out rather than inventing one.

Entity and relation discipline

Entities and relations are the part most worth getting right.

Naming. An entity name is the bare proper name, nothing else. Strip role
descriptors, employer phrases, and articles. Affiliation belongs in org and
title in role, never in the name. Consistent bare names let the same person or
org collapse to one entity instead of several near-duplicates. When the entity
is a message sender, take the name from the sender's display-name, but a
sender display-name is often decorated and must be reduced to the bare
personal name: strip any trailing "from <org>", "at <org>", or role phrasing.

Type. person is a named individual. org is any company, church, store,
venue, team, school, or agency. project is a named initiative or body of
work, not a thing or a place. When torn between org and person, a name that
could sign a contract or own a building is an org.

Per-entity org. An entity's org is where THAT entity belongs, not the
thread's org. It is fixed by THAT entity's own email domain (via
org_domain_map) or a stated affiliation, NOT by what the email is about. A
sender whose domain is not in org_domain_map is external, even when the body
is entirely about your own org. Do not infer a person's org from the thread's
subject matter or from the recipient's org, and do not default an outside
person to your own org just because they emailed you. Use unknown when nothing
supports a choice. When an entity appears in known_people, use that entry's
org directly: the own-domain rule applies only to entities not already
confirmed there.

Relations. A relation joins two real, named entities that you have also listed
in entities. Both source_name and target_name must be entity names, never an
org tag (the valid_orgs values, external, and unknown are tags, not
entities). works_at links a
person to the org they belong to; do not assert it for venues, tools, or
places. Emit only relations the text supports, and skip the rest rather than
guessing.

Thread-mode rules

Each thread carries open_actions: actions already on record for that thread,
each with an id. When open_actions is present, prefer resolving or updating
over creating.

resolved_action_ids: list the ids this thread clearly closes or supersedes.
updated_actions: list ids where the action is still needed but the text should
be corrected (scope clarified, date confirmed, wording improved). Give the
corrected text in new_text.
New actions: only add an action if the thread introduces work genuinely NOT
covered by the open actions above. Prefer resolving or updating over creating
a near-duplicate.

Merge-review rules

pending.json may carry a merge_review list of candidate entity pairs. For each
pair, decide whether the two entries are the SAME real-world entity and, if
so, give the single best canonical name. Emit one answer per pair into
merge_answers:

{"pair_id": "a-id|b-id", "same": true, "canonical": "Person Name"}

Use the pair's pair_id verbatim. When same is false, canonical is an empty
string. Guidance:

Initials and short forms can match a full name ("Joel" = "Joel Chelliah").
Different surnames or different initials are different people ("Daniel P" is
not "Daniel F").
When unsure, answer false.

When merge_review is empty or absent, merge_answers is [].

Thread-synthesis rules

pending.json may carry a synthesis list: threads active enough to deserve a
deeper situational narrative than the one-line summary already on record. Each
item gives thread_id, subject, org, email_count, the date span, and
email_summaries (the thread's per-message summary lines in date order).

For each item, write a contextual_summary: a short paragraph (2-4 sentences)
that says what the thread is actually about, where it has got to, who is
involved, and what is outstanding. This is the standing context a colleague
would want before opening the thread cold, not a restatement of the one-line
summary. Emit one answer per item into synthesis:

{"thread_id": "t-abc123", "contextual_summary": "The Hall B booking for the..."}

Use each item's thread_id verbatim. When synthesis is empty or absent in the
input, omit it from the output or use [].
```
