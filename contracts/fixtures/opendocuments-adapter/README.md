# OpenDocuments adapter fixtures (proposed)

These files are A0 mapping evidence. They are not C1, not a production schema,
and not permission to call a customer or cloud API.

Verified upstream SHA: `f3aba15f0161f1730e746de47a2ffd1e53cbea44`.
Work Stack base SHA: `0fb998ebf4b5fb4fd17cfddf21837c22f2c94aae`.
Revised from the A0 candidate `ebe0d0c47dc3c81f659d24161ee3d562de9a4cbd`
(CHANGES REQUESTED; findings A0-1 … A0-6). Field names changed in that
revision — nothing may depend on the previous shape.
Boundary-fixed from `a6ef57fc9e3b66652bd96e7cd22b5712ae3785b9` after the
independent final re-review reopened A0-1 and A0-4. That pass added the
`unreconcilable_declared_source_type` locator reason and changed
`hostile-credential-url` to use the connector names the frozen upstream
actually writes.

Run from the repository root:

```text
python -m unittest discover -s contracts/fixtures/opendocuments-adapter -p "test_*.py" -v
```

Stdlib `unittest` plus `jsonschema==4.26.0`, which is already declared in
`requirements.txt`. The import is unconditional on purpose: the schema is closed
evidence, so a missing validator must fail the suite rather than skip it. No
product install, no live HTTP, no original documents, no skip/xfail.

What the suite proves:

- every catalog output equals its frozen golden, **and** validates against
  `proposed-normalized.schema.json` (Draft 2020-12 with format checking);
- the schema is conditional on `ok` — a success body may not carry an error and
  a refusal may not carry a result;
- `sourceType` never admits a `sourcePath`: a declared/parsed disagreement, a
  malformed `notion://` page id, a credential-bearing URL, and an unvalidatable
  scheme are all redacted or reduced, with no raw value retained;
- URI schemes are matched case-insensitively, so every spelling of
  `http`/`https` reaches the same credential and query handling and no
  recognised scheme falls through to a display label;
- every `sourceType` the frozen upstream can emit is reconciled against the
  locator classes it may carry (including the one s3 plugin that emits both
  `s3://` and `gcs://`), and any other nonempty declaration redacts the locator
  instead of retaining it on a guess;
- identity strings are matched against the whole string and the chunk position
  is bounded before construction, so an accepted source cannot produce a
  schema-invalid document; a bounded corpus of accepted boundary and adversarial
  projections is validated against the closed schema, not only the catalog;
- numeric normalization never raises: a JSON integer too large for a float is
  clamped rather than converted;
- a candidate without a well-formed, document-matching `chunkId` is omitted, and
  no candidate is ever addressed by a heading path;
- indexed identity is labelled `opendocuments_index` and source-qualified
  identity stays `unknown`;
- request and metadata bounds are enforced by the mapper, not only by the
  schema, including overlong and control-character inputs;
- merged web/non-workspace results are omitted with a distinct reason.

`mapping.py` is fixture-local. Do not import it from product runtime.
See `docs/OPENDOCUMENTS-CONNECTOR-MAPPING.md` for observed fields, proposed
mappings, coordinator decisions D1-D7, and M1 fake-HTTP coverage.
