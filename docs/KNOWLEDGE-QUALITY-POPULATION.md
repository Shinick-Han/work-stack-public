# Which source the structural quality gate governs

Status (2026-09-08): the knowledge retrieval contracts in `workstack/`, the
knowledge capture import modules, the OpenDocuments adapter subtree and the dist
source gate are registered production source, and every one of those roots now
exists in this checkout. Discovery reports no missing production root and the
layer resolver reports no forbidden import. Two roots were declared ahead of the
lanes that created them; both lanes have since landed, and the section below
records that history together with the inbound caller edges the landing made
real. The three capture import modules integrated after the retrieval lane froze
are registered in [The capture import layers](#the-capture-import-layers).

## Why registration is the whole story

`scripts/quality_gate.py` measures exactly two things about a file, and both are
opt-in:

- **Discovery.** `_discover` walks the roots declared in `source_sets`. A file no
  root reaches is not in `source_populations`, so it has no measured file length,
  no measured function length, no measured cyclomatic complexity and no node in
  the import graph. It is not "passing" the gate — it is invisible to it.
- **Classification.** `_classify_layers` assigns each discovered file the single
  `python_layers` / `frontend_layers` entry whose globs match it. A discovered
  file no layer claims is reported as `unclassified production source` and blocks
  the gate. A file that *is* classified has its outgoing imports checked against
  that layer's `may_import`.

The consequence worth remembering: a new top-level directory of production Python
escapes every structural scan silently, whereas a new file inside an already
governed root fails loudly until someone gives it a layer. Silence is the
dangerous case, so a new production tree gets its root declared before the code
arrives.

## The declared populations

| Source set | Roots | Extensions |
| --- | --- | --- |
| `python_core` | `workstack` | `.py` |
| `python_entrypoint` | `run_work_stack.py` | `.py` |
| `python_desktop` | `desktop/python-webview-shell` | `.py` |
| `python_opendocuments` | `integrations/opendocuments` | `.py` |
| `quality_tooling` | named `scripts/*` files, incl. `scripts/dist_source_gate.py` | `.py`, `.cjs` |
| `frontend_production` | `frontend/src` | `.ts`, `.tsx` |

`python_opendocuments` names the **adapter subtree**, not all of `integrations/`.
The neighbouring trees — the browser extension under
`integrations/microsoft-web-capture/` and the skill Markdown under
`integrations/agent-skill/` — are deliberately left outside; a missing adapter
root is an ordering problem and is not a licence to sweep unrelated integration
code into the production population. Its `exclude_globs` is empty, so nothing
*inside* the adapter subtree is hidden from the scan.

Only `.py` is admitted, so an adapter README stays out, as does anything under
`docs/` or `contracts/` — a fixture mapping is not production source merely
because it is written in Python.

## Roots that were declared ahead of their lane

`integrations/opendocuments` and `scripts/dist_source_gate.py` were declared
before either existed. Declaring them early was deliberate: omitting a root lets
the whole scan report green while real production source goes unmeasured, which
is the failure mode this page exists to prevent, whereas a declared-but-absent
root makes `_discover` say so by name.

Both lanes have now landed, so the actual scan of this checkout reports **no**
missing production root, and the adapter population is every `.py` file on disk
under the subtree. That started as five modules — `nas_paths.py`, `od_client.py`,
`retrieval_mapper.py`, `retrieval_mapper_fields.py` and `source_access.py` — and
the client and source-access split has since added `od_client_config.py`,
`od_client_transport.py`, `source_access_nas.py` and `source_access_types.py`.
Every one of them joined through the subtree glob, layered, with **no**
configuration change — which is what declaring the root early and claiming the
whole subtree bought. The test compares the population against the directory
listing rather than a recorded count, so a tenth helper neither slips in
unmeasured nor is quietly excluded.

The refusal itself is still tested. `MissingRequiredRootTests` deletes each
declared root from a synthetic checkout and asserts `_discover` still reports

```
missing production root: integrations/opendocuments
missing production root: scripts/dist_source_gate.py
```

so a root that silently stops being reachable remains a loud failure rather than
a stale expectation nobody exercises.

Sweeping the whole `scripts/` directory instead of naming the file was rejected
for the same reason as widening `integrations/`: thirteen further scripts live
there that no layer claims, and admitting them would report thirteen unclassified
sources at once.

### The dist gate helpers: explicit roots after integration

The dist source gate has been split into exactly two helpers:
`scripts/dist_source_gate_manifest.py` and `scripts/dist_source_gate_receipt.py`.
Both are now explicit `quality_tooling` discovery roots as well as members of
the narrow layer glob. An actual-tree test checks both files are discovered and
layered. The synthetic pre-landing tests remove only these roots and their own
temporary stub files so their missing-root/invisible-file counterexamples remain
meaningful after integration. The two halves were prepared separately:

- The **layer** glob `scripts/dist_source_gate_*.py` is declared **now**, on
  `py_quality_tooling`. A glob carries no existence requirement, so claiming the
  prefix in advance costs nothing and means a helper cannot land unclassified.
  The prefix is narrow: `scripts/dist_release_notes.py` matches nothing and stays
  refused, exactly as the twelve other unregistered scripts do.
- The **discovery root** is added when each helper lands, not before. A root that
  does not exist is reported by name — `missing production root:
  scripts/dist_source_gate_manifest.py` — so pre-declaring these two would turn a
  green scan red for a file nobody has written yet. That is the opposite of the
  ordering rule above, which pre-declares a root only when the alternative is a
  silently *unmeasured* tree; here the base file `scripts/dist_source_gate.py`
  keeps the tree measured and mandatory in the meantime.

`DistGateHelperAdmissionTests` drives all four cases synthetically: both helpers
admitted and layered once their roots land, the unrelated script admitted but
refused a layer, a helper root declared before its file reported as missing, and
a helper file that lands without its root staying invisible to the scan. The base
root stays load-bearing: deleting `scripts/dist_source_gate.py` still reports it
missing, and the new prefix does not double-claim it.

What this prefix does **not** buy is dependency enforcement between the three
files. The lane composes them through an explicit
`importlib.util.spec_from_file_location` under its own
`workstack_dist_source_gate_*` module names, and `_python_graph` reads imports
from the AST, so the intended `receipt <- manifest <- facade` direction produces
no edge the layer rule could check. The registration is therefore honest about
its scope: it guarantees each helper is discovered, measured against the
structural limits and classified — and asserts the import graph really is empty
for them — while the load direction stays the lane's own invariant. Recording it
as a rule here would be a rule the resolver cannot enforce.

## The knowledge layers

| Layer | Files | May import |
| --- | --- | --- |
| `py_knowledge_request` | `workstack/knowledge_request.py` | `py_foundation` |
| `py_capture_retrieval` | `workstack/capture_retrieval.py` | `py_foundation`, `py_knowledge_request` |
| `py_knowledge_ledger_document` | `workstack/knowledge_ledger_document.py` | `py_foundation`, `py_knowledge_request` |
| `py_knowledge_owner_requests` | `workstack/knowledge_owner_requests.py` | `py_foundation`, `py_knowledge_request`, `py_knowledge_ledger_document` |
| `py_knowledge_request_issuer` | `workstack/knowledge_request_issuer.py` | `py_foundation`, `py_knowledge_request`, `py_knowledge_ledger_document`, `py_knowledge_owner_requests` |
| `py_knowledge_requests_http` | `workstack/knowledge_requests_http.py` | `py_transport`, `py_legacy_store`, `py_knowledge_request`, `py_knowledge_ledger_document`, `py_knowledge_request_issuer` |
| `py_store_knowledge_migration` | `workstack/store_knowledge_migration.py` | `py_foundation`, `py_store_migration`, `py_knowledge_ledger_document` |
| `py_opendocuments_adapter` | `integrations/opendocuments/**` | `py_knowledge_request`, `py_capture_retrieval` |

Each `may_import` is the set the file's real imports need, and nothing wider.
`workstack/knowledge_request.py` imports `workstack/capture.py`, which is
`py_foundation`; `workstack/store_knowledge_migration.py` reaches
`store_report_migration` (`py_store_migration`) plus `store_rosters` and
`store_errors` (`py_foundation`); the issuer reaches the three knowledge
contracts and `capture`.

`py_knowledge_requests_http` is a layer of its own rather than another glob on
`py_transport`. It needs `server_errors.RequestError` and `store.Store`, and
folding it into `py_transport` would have meant granting the knowledge contracts
to all twelve transport modules. The one coarse grant that remains is
`py_transport` itself, taken for a single error type; extracting a
`py_transport_errors` layer would make it exact, and is the right follow-up if
that layer is ever touched again.

### What the landed callers of these layers may reach

Carving the knowledge modules into their own layers made six edges that already
existed in `workstack/` cross a layer boundary for the first time. Each is a
current module-load import, so each needed its caller's `may_import` corrected;
none was granted through `architecture_exceptions`, and no caller was widened
beyond the single neighbour its own responsibility needs.

| Caller (layer) | Reaches (layer) | Why that matches the caller's responsibility |
| --- | --- | --- |
| `workstack/maintenance.py` (`py_storage`) | `store_knowledge_migration.py` (`py_store_knowledge_migration`) | Storage maintenance plans the knowledge ledger upgrade exactly as it already plans the report upgrade through the `py_store_migration` grant it holds. |
| `workstack/server.py` (`py_transport`) | `knowledge_requests_http.py` (`py_knowledge_requests_http`) | The server composes `KnowledgeRequestsHttpMixin`. That surface is a transport sibling, carved out only so *its* downstream reach to the issuer and contracts stays narrow. |
| `workstack/server_post_routes.py` (`py_transport`) | `knowledge_requests_http.py` (`py_knowledge_requests_http`) | The POST router reads `KNOWLEDGE_BODY_LIMIT` and `KNOWLEDGE_PATH_PREFIX` from the route that owns them rather than restating those constants. |
| `workstack/store_document_validation.py` (`py_store_validation`) | `knowledge_ledger_document.py` (`py_knowledge_ledger_document`) | Document validation delegates the knowledge document's shape rule — `KNOWLEDGE_DEFAULT`, `validate_knowledge_document`, `KnowledgeLedgerError` — to the pure contract module that owns it. |
| `workstack/store_layout.py` (`py_store_layout`) | `knowledge_ledger_document.py` (`py_knowledge_ledger_document`) | Layout places the ledger file using the contract's own `KNOWLEDGE_DOCUMENT_NAME` and `KNOWLEDGE_DEFAULT`, which only that module may define. |
| `workstack/store_schema_upgrade.py` (`py_store_schema_upgrade`) | `store_knowledge_migration.py` (`py_store_knowledge_migration`) | The schema upgrade calls the knowledge migration planner beside the report migration planner it is already permitted. |

The grant is one named layer in each case, so the boundary each caller did not
already cross stays shut: `py_store_validation` still may not reach the issuer,
`py_store_layout` still may not reach the migration planner, `py_storage` still
may not reach the HTTP surface, `py_store_schema_upgrade` still may not reach the
ledger contract directly, and `py_transport` still may not bypass
`py_knowledge_requests_http` to reach the issuer or the ledger contract.
`ActualInboundEdgeTests` withdraws each grant from a copy of the rules and
asserts the corresponding violation returns — and that withdrawing all six
reproduces those six edges and no others, so nothing was admitted alongside them.

### What the adapter may and may not reach

`py_opendocuments_adapter` is granted the two **pure retrieval validators** and
nothing else. Those modules validate and normalise request and capture payloads;
they hold no store, no service, no transport and no I/O, so an adapter that
reuses them is reusing a validation primitive rather than reaching into the core.
The mapper lane's `retrieval_mapper.py` and `retrieval_mapper_fields.py` are
covered by the subtree glob and need no further registration.

Everything else in `workstack/` stays refused. An adapter module that imports
`workstack.store` produces

```
forbidden layer import: integrations/opendocuments/retrieval_mapper.py (py_opendocuments_adapter)
  -> workstack/store.py (py_legacy_store)
```

and `workstack.service` is refused the same way. This is enforced through the
ordinary `may_import` mechanism: no entry was added to
`architecture_exceptions`, and no broad integration-to-core allowance exists.

If a future mapper needs a `workstack/` module that is *not* one of the two
retrieval validators — `workstack/capture.py` directly, for instance — the honest
options are to extract the primitive it needs into its own pure layer, or to
widen this list one named layer at a time. Granting `py_foundation` wholesale
would hand the adapter the report, roster and store-error modules too, and is not
an acceptable substitute.

## The capture import layers

Three modules integrated after the retrieval lane froze and landed unclassified:
`knowledge_capture_packets.py` (the pure packet contract),
`knowledge_capture_import.py` (the orchestrator that turns reviewed packets into
ledger stages) and `knowledge_captures_http.py` (the HTTP import boundary). They
are registered as three layers, one per responsibility, because their reach
differs: the contract touches no store and no service, the orchestrator mints
identifiers, and only the HTTP module knows about transport.

| Layer | Files | May import |
| --- | --- | --- |
| `py_knowledge_capture_packets` | `workstack/knowledge_capture_packets.py` | `py_foundation`, `py_knowledge_request`, `py_capture_retrieval` |
| `py_knowledge_capture_import` | `workstack/knowledge_capture_import.py` | `py_knowledge_request`, `py_knowledge_ledger_document`, `py_knowledge_owner_requests`, `py_knowledge_capture_packets`, `py_service_domain` |
| `py_knowledge_captures_http` | `workstack/knowledge_captures_http.py` | `py_transport`, `py_capture_retrieval`, `py_knowledge_request`, `py_knowledge_ledger_document`, `py_knowledge_request_issuer`, `py_knowledge_requests_http`, `py_knowledge_capture_packets`, `py_knowledge_capture_import` |

Each list is exactly the set of layers the module's real imports resolve to;
`ActualImportGraphTests` compares the granted reach against `_python_graph`'s
targets for the file and fails if either side gains an entry.

`py_knowledge_captures_http` takes the same coarse `py_transport` grant that
`py_knowledge_requests_http` already takes, and for the same single reason:
`server_errors.RequestError`. Extracting a `py_transport_errors` layer would
make both exact and remains the recorded follow-up; this lane did not widen the
debt, it reused it.

### Keeping the orchestrator's identifier dependency honest

`knowledge_capture_import.py` really imports `service_domain._next_id`, and
`service_domain.py` was inside the coarse `py_application` layer. Three ways out
were available and two were rejected:

- Granting `py_application` to the orchestrator would hand it the twenty-odd
  service modules — task commands, search, workspace, checkpoints — for one id
  helper. Rejected: that is a blanket grant, not a dependency.
- Moving `service_domain.py` into `py_foundation` would relabel an application
  module as a foundation one to make the edge legal. Rejected: the file imports
  `store` and the domain error taxonomy, so calling it foundational would be a
  fiction the graph contradicts.
- Carving the two files the dependency actually needs into their own narrow
  layers, named for what they hold. Taken.

| Layer | Files | May import | Role |
| --- | --- | --- | --- |
| `py_service_errors` | `workstack/service_errors.py` | `py_snapshot` | The shared domain error taxonomy. |
| `py_service_domain` | `workstack/service_domain.py` | `py_service_errors`, `py_legacy_store` | Enumerations, limits and the id/record/revision primitives. |

`py_application` keeps every other service module and gains both narrow layers as
neighbours, so no existing application import changed meaning. The orchestrator
is granted `py_service_domain` **alone**: it still may not reach `py_application`,
`py_legacy_store`, `py_storage` or `py_transport`, and each of those four
refusals is exercised against the real resolver over a synthetic tree.

The carve-out points one way. `py_service_domain` is not granted the capture
layers, so the primitives cannot learn about their caller, and the packet
contract is not granted `py_service_domain` either — a pure contract mints
nothing.

### What the landed callers of the capture layers may reach

Four existing modules already import these three, so registering only the
outgoing reach would have left four forbidden imports. Each caller gained one
named neighbour.

| Caller (layer) | Reaches (layer) | Why that matches the caller's responsibility |
| --- | --- | --- |
| `workstack/service_capture_rules.py` (`py_application`) | `knowledge_capture_packets.py` (`py_knowledge_capture_packets`) | The capture rules delegate the imported-packet shape rule to the pure contract that owns it instead of restating the packet schema. |
| `workstack/store_document_validation.py` (`py_store_validation`) | `knowledge_capture_packets.py` (`py_knowledge_capture_packets`) | Document validation asks the packet contract for `imported_capture_defect`, exactly as it already asks the ledger contract for its document shape. |
| `workstack/server.py` (`py_transport`) | `knowledge_captures_http.py` (`py_knowledge_captures_http`) | The server composes `KnowledgeCapturesHttpMixin` beside the knowledge request mixin it already composes. |
| `workstack/server_post_routes.py` (`py_transport`) | `knowledge_captures_http.py` (`py_knowledge_captures_http`) | POST routing reads `IMPORT_PATH` and `IMPORT_BODY_LIMIT` from the route that owns them rather than restating either constant. |

The boundaries these callers did not already cross stay shut: `py_application`
may not reach the orchestrator, `py_store_validation` may not reach it either,
and `py_transport` may not bypass `py_knowledge_captures_http` to reach the
orchestrator or the packet contract directly. The OpenDocuments adapter is
granted neither the packet contract nor the identifier primitives; both refusals
are tested. `ActualImportGraphTests` withdraws each new grant from a copy of the
rules and asserts exactly its own edges return, and that withdrawing all of them
reproduces those edges and no others.

## Registration is not a debt amnesty

Admitting a file measures it against the declared limits — 800 file lines, 100
function lines, CCN 15 — for the first time. A newly admitted file over a limit
raises a `new … exceeds` error rather than quietly joining
`quality/structural-baseline.json`, and `admissible_file_length_debt` refuses to
seal file-length debt for a file that was not already over the limit at the
sealed HEAD. Registering a tree therefore surfaces its structural debt; paying it
is a separate change.

The debt this page's registrations have made visible, and deliberately have not
paid, hidden or waived:

- **Unclassified production source.** `workstack/cli_output.py` and
  `desktop/python-webview-shell/desktop_update_process.py` are discovered and
  claimed by no layer. They predate every registration here. The capture import
  lane asserts that the unclassified list is *exactly* these two, so neither a
  new escapee nor a silent sweep of these into a layer can pass unnoticed.
- **Coarse transport reach.** Two knowledge HTTP layers hold `py_transport` for
  one error type, as described above.
- **Structural limits.** `integrations/opendocuments/source_access.py`,
  `nas_paths.py`, `od_client.py` and `scripts/dist_source_gate.py` carry the
  file-length and complexity debt recorded when their trees were admitted.

None of these was addressed by lowering a threshold, widening a baseline,
relaxing a coverage floor or adding an `architecture_exceptions` entry; the
capture import lane added no exception at all, and asserts that none was added
for its own modules.
