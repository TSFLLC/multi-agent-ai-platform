# Migration rollback policy

Alembic `downgrade` is **not** the production rollback mechanism for this
platform. Rollback is a **restore of the pre-migration backup** of the database
volume. A migration may therefore be irreversible on purpose.

## Deployed migration history is never rewritten

A revision that has been applied to a hosted database keeps its exact revision id
and its exact body forever. Anything new it should have done ships as a **new
forward revision** on top of it. (Renaming a deployed revision makes Alembic fail
with `Can't locate revision identified by '…'` at deploy time, and the hosted
entrypoint then never starts the Web/Worker pair.)

## Learning Item lineage: `ail5d_` is a historical name, not a phase

| Revision | Status | What it does |
|---|---|---|
| `ail5d_learning_item_lineage` | **Deployed to staging. Never edit or rename.** Irreversible. | Adds `learning_items.lineage_id`, backfills every row with a deterministic uuid5 lineage (Version 1), adds the unique `(lineage_id, version)` index and the lineage index. `lineage_id` stays nullable and there is no `version >= 1` rule (SQLite cannot add them without rebuilding a table that other tables reference). |
| `academy_lineage_enforcement` | Forward revision (this is what new deployments add). Reversible. | Proves the data is valid, ensures the indexes exist, then enforces `lineage_id IS NOT NULL` and `version >= 1` in the database: SQLite triggers (`trg_learning_items_lineage_insert` / `_update`), or a real NOT NULL + CHECK on other engines. Downgrade removes only what it added. |

The `ail5d_` prefix is a naming accident. **It does not mean the Structured
Learning Experience (lesson steps, step progress, step-scoped Professor, step UI,
curriculum v3) is that revision.** It is Learning Item revision infrastructure for
the Level 1 Academy.

## Structured Learning (AIL.5D.1): `academy_step_progress`

| Revision | Status | What it does |
|---|---|---|
| `academy_step_progress` | Forward revision on top of `academy_lineage_enforcement`. **Reversible.** | Creates ONE table, `academy_step_progress` (mutable, user-scoped learner progress on the authored steps a Learning Item carries in `spec_json.steps`). Touches no existing table, row, index or trigger. |

`downgrade` drops only that table (and its index) and **refuses, changing nothing,
while any learner progress row exists**: that is learner data, and the platform's
rollback is a restore of the pre-migration backup, not `downgrade`. This revision
is deliberately **not** in `EXPECTED_IRREVERSIBLE`. It never renames, rewrites or
depends on the body of `ail5d_learning_item_lineage`.

Both paths reach the same contract: an already-migrated database (staging) runs
only `academy_lineage_enforcement`; a fresh or pre-AIL5 database runs the whole
chain including `ail5d_learning_item_lineage` first.

## Irreversible revisions

| Revision | Why it refuses to downgrade |
|---|---|
| `ail5d_learning_item_lineage` | Adds `learning_items.lineage_id` and the revision lineage that Level 1 Academy content and Review Attempts now reference. Removing the column needs a table rebuild of a table that other tables reference; the lineage is historical evidence. |

An irreversible revision:

1. has a `downgrade()` whose only action is to raise `RuntimeError` **before
   touching anything**;
2. is listed in `EXPECTED_IRREVERSIBLE` in `tests/test_migration_rollback_policy.py`
   (adding one requires editing that list, i.e. a review decision);
3. if it is added after this policy, also declares `IRREVERSIBLE = True` at module
   level. (`ail5d_learning_item_lineage` predates the policy and is deployed, so it
   is grandfathered without the marker rather than edited to add one.)

`downgrade()` implementations that refuse only *while data exists* (the
`ail5c_evidence_enum_ext` and `ail5c_grader_agent_role` revisions) are reversible
and are not affected by this policy.

## Before any deploy that includes an irreversible revision

1. Take a backup of the database volume (the same procedure as
   `railway-staging.md`) and verify it opens.
2. Rehearse the upgrade on a **copy** of that backup, never on the live volume.
3. Keep the backup until the release is accepted.
4. To roll back: stop the app, restore the backup, redeploy the previous image.

Never run `alembic downgrade` against a real database, and never
`downgrade base` on `data/multi_agent_platform.db`. Never run `alembic stamp` or
edit `alembic_version` by hand to make a deploy pass.

## Tests that exercise the chain

Historical full-chain downgrade tests (`test_migrations.py` and others) walk the
chain down to base. They fail at the irreversible revision by design; they were
already failing before it existed (see the AIL.5C completion report) and are
intentionally not edited to manufacture a green run. New migration tests must
stop at, or before, the irreversible revision when they need to go down.
