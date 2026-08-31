# snapshot-v1 content-safety policy / 1

## 1. Scope

This policy applies only to the `title` and `detail` strings in a structurally valid `workstack.planning-task-snapshot.v1` object.

It is a deterministic, high-confidence credential tripwire. It is not a comprehensive secret detector, malware detector, prompt-injection detector, data-class classifier, or replacement for explicit user disclosure review.

The snapshot remains display/review/storage content in Conduit v1 and must not become agent execution input.

## 2. Processing model

For each field independently:

1. Apply the docking contract's Unicode 17.0.0 NFC rule, scalar-count, UTF-16-count, and control-character rules first.
2. Evaluate the exact original string. Do not normalize, casefold, trim, redact, or rewrite the stored value.
3. Matching below is ASCII case-insensitive only where explicitly stated.
4. Evaluate lines after splitting on LF. LF remains part of the original value; line splitting is a matching view only.
5. Evaluate explicit encoded views defined in section 3. No implicit decoding is allowed.
6. Stop at the first rule in ascending rule-ID order that refuses.
7. Return only the public refusal code, field name, and rule ID. Never return the matched substring, decoded content, line, offset, or original field value.

The decision is either `ALLOW` or `REFUSE`. There is no warning-only, automatic redaction, repair, or sanitizing path.

## 3. Explicit encoded views

Only these wrappers trigger decoding:

- `conduit-base64:<payload>`
- `conduit-percent:<payload>`

The wrapper may occur as a complete LF-delimited line after optional leading and trailing ASCII space or tab. Wrapper names are ASCII case-sensitive.

### 3.1 Base64

- Payload uses the standard RFC 4648 alphabet `A-Z a-z 0-9 + /` with canonical `=` padding.
- Length must be a nonzero multiple of four.
- Unused pad bits in the final encoded symbol must be zero; a payload whose final symbol carries nonzero unused bits is not canonical and is refused.
- Decoding is strict; ignored characters and whitespace inside the payload are forbidden.
- Decoded length must not exceed 4,096 bytes.
- Decoded bytes must be valid UTF-8 without BOM and must satisfy the field's control-character rule.
- Exactly one decoding layer is evaluated. A decoded second wrapper is ordinary text and is not decoded again.

### 3.2 Percent encoding

- Payload consists only of RFC 3986 unreserved characters, ASCII plus (`+`, U+002B), and `%HH` triplets with uppercase hexadecimal digits.
- ASCII plus is decoded as the literal plus character U+002B and is never decoded as space.
- Decoded length must not exceed 4,096 bytes.
- Decoded bytes must be valid UTF-8 without BOM and must satisfy the field's control-character rule.
- Exactly one decoding layer is evaluated.

An explicit wrapper with invalid syntax, size, UTF-8, BOM, or control content is refused as rule `S000` with public code `SNAPSHOT_SAFETY_ENCODING_INVALID`.

## 4. Placeholders

For every rule that invokes placeholder recognition, first remove zero or more ASCII space (U+0020) and horizontal tab (U+0009) characters from both ends of the complete candidate value; remove no other character. The following resulting complete values are placeholders and do not trigger refusal. Comparison is ASCII case-insensitive where alphabetic:

- `<redacted>`
- `[redacted]`
- `redacted`
- `***`
- `xxxxx`
- `example`
- `placeholder`
- `not-a-secret`
- `$IDENTIFIER`
- `${IDENTIFIER}`
- `%IDENTIFIER%`

`IDENTIFIER` means `[A-Za-z_][A-Za-z0-9_]{0,63}`. Placeholder recognition succeeds only when the entire value remaining after the specified ASCII trim exactly matches one listed fixed value or one complete `IDENTIFIER` form. Any other remaining character makes the value non-placeholder.

## 5. Refusal rules

### S001 — private-key envelope

Refuse when any original or decoded view contains an LF-delimited line whose value, after removing leading and trailing ASCII space (U+0020) and horizontal tab (U+0009) characters, starts with `-----BEGIN ` and ends with ` PRIVATE KEY-----`.

Public code: `SNAPSHOT_CREDENTIAL_SUSPECTED`.

### S002 — authorization header with concrete value

Refuse when an original or decoded LF-delimited line, after leading ASCII space/tab removal, has:

1. ASCII-case-insensitive field name `Authorization` or `Proxy-Authorization`;
2. optional ASCII space/tab, `:`, optional ASCII space/tab;
3. ASCII-case-insensitive scheme `Basic` or `Bearer`;
4. one or more ASCII spaces/tabs; and
5. a candidate value equal to the maximal nonempty run, starting at that position, of characters in the inclusive ASCII range U+0021 through U+007E; refuse only when that entire candidate has at least 12 characters and is non-placeholder.

Public code: `SNAPSHOT_CREDENTIAL_SUSPECTED`.

### S003 — credential-key assignment with concrete value

Refuse when an original or decoded LF-delimited line contains a credential key followed by optional ASCII space/tab, `:` or `=`, optional ASCII space/tab, and a candidate value that is non-placeholder and contains at least 8 Unicode scalar values. When the first character after the optional whitespace is `"` or `'` and the same character occurs again later in the line, the candidate value is the span between that first quote pair with the two quote characters removed. Otherwise the candidate value is the maximal run, starting at that position, of characters other than ASCII space (U+0020) and horizontal tab (U+0009).

Credential keys are ASCII case-insensitive and may use `_` or `-` at the shown separator positions:

- `password`
- `passwd`
- `pwd`
- `api_key`
- `access_token`
- `refresh_token`
- `id_token`
- `oauth_token`
- `client_secret`
- `private_key`

The key must begin at line start or be preceded by ASCII space (U+0020), horizontal tab (U+0009), `"`, `'`, `{`, `[`, or `,`. It must end immediately before the assignment whitespace/separator.

Public code: `SNAPSHOT_CREDENTIAL_SUSPECTED`.

### S004 — known standalone token shape

Refuse when an original or decoded view contains an ASCII token bounded on both sides by start/end or a non-`[A-Za-z0-9_]` character and matching one of:

- `ghp_` followed by at least 20 `[A-Za-z0-9]` characters;
- `github_pat_` followed by at least 20 `[A-Za-z0-9_]` characters;
- `sk-` followed by at least 20 `[A-Za-z0-9_-]` characters.

Public code: `SNAPSHOT_CREDENTIAL_SUSPECTED`.

### S005 — credential-bearing HTTP userinfo

For each original or decoded LF-delimited line, scan left to right for ASCII case-insensitive `http://` or `https://` whose initial `h` is at line start or is preceded by a character outside `[A-Za-z0-9+.-]`. Starting immediately after `//`, take the maximal sequence ending before the first `/`, `?`, `#`, ASCII space (U+0020), horizontal tab (U+0009), or line end as the authority candidate. The candidate must satisfy the RFC 3986 authority grammar and must contain RFC 3986 userinfo followed by its literal `@` delimiter. Split that userinfo at its first literal `:` before percent decoding; the prefix is username and must be nonempty, and the complete remainder is password. Convert every unescaped ASCII userinfo character to its ASCII octet and every valid `%HH` triplet (uppercase or lowercase hexadecimal) to its represented octet exactly once; `+` remains the ASCII plus octet. The password octets must decode as strict UTF-8. Invalid URI syntax, invalid percent syntax, or invalid UTF-8 does not match S005. Refuse when the decoded password is non-placeholder and contains at least 8 Unicode scalar values.

Public code: `SNAPSHOT_CREDENTIAL_SUSPECTED`.

### S006 — sensitive machine-local credential path

For each original or decoded view, form a matching view by replacing every backslash (`\`) with slash (`/`) and ASCII-lowercasing `A` through `Z`; perform no other path, Unicode, percent, dot-segment, or filesystem normalization. Refuse when this matching view contains one listed suffix and the character immediately after that occurrence is either end of view or one of ASCII space (U+0020), horizontal tab (U+0009), LF (U+000A), double quote (`"`), single quote (`'`), right parenthesis (`)`), right square bracket (`]`), right brace (`}`), comma (`,`), or semicolon (`;`).

- `/.ssh/id_rsa`
- `/.ssh/id_ed25519`
- `/.aws/credentials`
- `/gcloud/application_default_credentials.json`

A source-repository path such as `src/auth/credentials.ts` is not covered.

Public code: `SNAPSHOT_SENSITIVE_PATH_SUSPECTED`.

## 6. Explicit non-refusals

In the absence of another matching rule, the policy allows:

- ordinary email addresses;
- words such as `token`, `password`, `credential`, or `authorization` used without a concrete matching value;
- ordinary commands and command names;
- environment-variable names and placeholder references;
- source-code paths, including filenames containing `token`, `auth`, or `credentials`;
- prose explaining how not to expose a secret;
- opaque IDs without a known standalone-token prefix;
- LF and horizontal tab in `detail` as allowed by the base contract.

## 7. Fixture execution

For each case in `snapshot-v1-safety-cases.json`:

1. concatenate `input.fragments` in array order with no delimiter;
2. assert that no fragment is empty;
3. run the policy against the named field;
4. compare the decision and, for refusal, the public code and rule ID exactly;
5. assert that diagnostics contain none of the fragments and none of the assembled input;
6. do not write the assembled positive input to disk or logs;
7. record, beside the fixture results, the Unicode Character Database version used by the harness and require its NFC decisions to equal Unicode 17.0.0 normalization results; a host runtime version other than 17.0.0 is not by itself a failure, but any differing NFC decision is a conformance failure.

The fixture file itself contains no complete positive credential-shaped canary.
