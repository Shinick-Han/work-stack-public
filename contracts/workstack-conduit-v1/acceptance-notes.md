# Language-neutral acceptance notes

## 1. Authority and evaluation order

The bundled contract is authoritative. The JSON Schema is only an early structural
aid. A conforming consumer follows Contract §9.1 order: byte length, optional supplied
digest syntax, exact-byte digest, UTF-8/BOM, terminal LF, duplicate keys, exact key set,
field rules, deterministic `origin_ref`, canonical reserialization equality, then the
frozen safety policy.

A failure must not create a Core task, import ticket, room, seat, run, provider process,
or session. Diagnostics must not echo rejected content.

## 2. Valid canonical fixtures

Read each `fixtures/valid/*.snapshot.json` file as binary. Its SHA-256 includes the
single final LF. Parsing and reserializing under Contract §7.1 must reproduce every byte
exactly. Do not normalize or repair source values. The `unicode` fixture contains
non-ASCII scalars directly in UTF-8 and JSON escapes for the field's LF and HTAB.

The expected-digest index is not authority by itself: recompute each digest, compare it
with the index, parse, validate, reserialize, and compare exact bytes.

## 3. Invalid construction recipes

Start from the exact named valid fixture. Apply operations in listed order and only in
memory. Do not persist generated invalid or credential-shaped inputs.

Byte operations are exact:

- `prepend_hex` and `append_hex`: decode lowercase hex to bytes and concatenate;
- `remove_suffix_hex`: require the named byte suffix exactly, then remove it;
- `replace_suffix_hex`: require one exact suffix and replace it;
- `replace_utf8_once`: UTF-8 encode both strings, require exactly one needle, replace
  it once;
- `replace_utf8_once_with_hex`: require exactly one UTF-8 needle and replace it with
  decoded hex bytes;
- `insert_after_prefix_utf8`: require one exact UTF-8 prefix at byte offset zero and
  insert `count` copies of the single byte named by `byte_hex` immediately after it.

For `reserialize_with_key_order`, parse the valid base, emit keys in the listed order
using all other Contract §7.1 rules, and append one LF.

For `object_mutation`, parse the base, apply top-level operations in order, and run
validation on the resulting in-memory value. `set`, `add`, and `delete` have their
ordinary exact meanings. `set_constructed` supports either
`utf16_code_units` (construct those exact 16-bit units) or `repeat` (repeat the named
Unicode scalar exactly `count` times). A lone surrogate must be refused before any
serializer attempts UTF-8 output.

For `digest_override`, leave bytes unchanged and supply the exact named digest string.

The expected `stage` and `reason` form the kit's conformance vocabulary. A product
may map them to a separately reviewed public error type, but its test adapter must
preserve the exact kit classification. Contract-defined `public_code` values must
match exactly where present.

## 4. Duplicate keys and numeric tokens

Do not use a parser that silently keeps the first or last duplicate. Detect duplicates
before or during parsing. Revision numeric spelling is checked on received bytes, not
only after conversion to a number; exponent, fraction, sign, and leading-zero forms are
nonconforming even when a library would coerce them to the same mathematical value.

## 5. Unicode and length rules

Reject unpaired surrogates first. NFC means Unicode Standard 17.0.0, independent of the
host's bundled Unicode version. Record the host Unicode data version and require its
decision to equal Unicode 17.0.0. The version-discriminator fixture must refuse as
`NOT_NFC`.

Count Unicode scalar values and UTF-16 code units separately. Apply the exact field
limits and control sets in Contract §6.3. Never normalize, truncate, or rewrite a value.

## 6. Safety policy

The nested `safety` directory must independently reconstruct to
`sha256:48dfd17ec86942e44692a26e7d43b88387c2c97f208c4d1081878a9318440148`.
Execute all 38 safety cases and all 17 text-boundary cases. Assemble fragments only in
memory, require every fragment to be nonempty, and never persist or log assembled
positive inputs. Compare decision, public code, rule, and reason exactly where present.

This tripwire is intentionally narrow. Passing it is not proof that content is
secret-free and must not be advertised as comprehensive detection.

## 7. JSON Schema limitations

The schema cannot express Unicode 17.0.0 NFC, UTF-16 code-unit limits, canonical byte
spelling, duplicate-key rejection, cross-field `origin_ref` derivation, exact digest,
or the safety policy. JSON Schema `maxLength` is only the scalar-length structural aid.
The semantic validator remains mandatory.

## 8. Acceptance result

A product accepts this kit only when it:

1. verifies every payload hash and reconstructs the top-level root;
2. reconstructs the nested safety root;
3. recomputes both valid-fixture digests;
4. proves both valid files are byte-canonical and semantically valid;
5. executes every invalid recipe with the exact expected classification;
6. executes all 55 nested safety/boundary fixtures;
7. records runtime and Unicode-data versions; and
8. reports the exact contract SHA-256 and top-level kit root.

Acceptance is read-only evidence. It does not authorize implementation, dependency
installation, commits, pushes, provider execution, or product-state mutation.
