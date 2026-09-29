# Migration rollback policy

Alembic `downgrade` is **not** the production rollback mechanism for this
platform. Rollback is a **restore of the pre-migration backup** of the database
volume. A migration may therefore be irreversible on purpose.

## Irreversible revisions

| Revision | Why it refuses to downgrade |
|---|---|
| `academy_learning_item_lineage` | Adds `learning_items.lineage_id` and the revision lineage that Level 1 Academy content and Review Attempts now reference. Removing the column needs a table rebuild of a table that other tables reference; the lineage is historical evidence. |

An irreversible revision:

1. declares `IRREVERSIBLE = True` at module level;
2. has a `downgrade()` whose only action is to raise `RuntimeError` **before
   touching anything**;
3. is listed in `EXPECTED_IRREVERSIBLE` in `tests/test_migration_rollback_policy.py`
   (adding one requires editing that list, i.e. a review decision).

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
`downgrade base` on `data/multi_agent_platform.db`.

## Tests that exercise the chain

Historical full-chain downgrade tests (`test_migrations.py` and others) walk the
chain down to base. They fail at the irreversible revision by design; they were
already failing before it existed (see the AIL.5C completion report) and are
intentionally not edited to manufacture a green run. New migration tests must
stop at, or before, the irreversible revision when they need to go down.
