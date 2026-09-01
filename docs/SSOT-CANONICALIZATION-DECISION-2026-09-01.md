# SSOT canonical JSON v1 decision

Work Stack v4 starts with a constrained standard-library codec rather than claiming RFC 8785
compatibility. The current SSOT contains only JSON objects, arrays, strings, booleans, nulls, and
safe integers. A JCS dependency would add packaging and license work without providing value for
the unsupported floating-point domain.

`workstack.canonical-json.v1` has these frozen rules:

- UTF-8 with strict encoding, no BOM, trailing newline, or insignificant whitespace;
- object keys sorted by Python Unicode code-point order;
- Unicode code points are preserved exactly and are not normalized;
- only null, exact booleans, strings, lists, dictionaries with string keys, and integers in the
  inclusive range `[-9007199254740991, 9007199254740991]` are accepted;
- floats, non-string keys, invalid surrogate code points, cycles, tuples, and other Python values
  are rejected before serialization;
- digests use SHA-256 over the exact canonical bytes and retain Work Stack's `sha256:` prefix.

This is deliberately narrower than general JSON and is not RFC 8785. Before another language
verifies SSOT manifests, Work Stack must publish cross-language vectors and either implement this
frozen format exactly or introduce a separately versioned RFC 8785 format. A format identifier in
the v4 manifest prevents a future codec change from silently altering record digests.
