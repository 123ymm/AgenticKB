# M1 Contract Corpus

This is the first-layer contract corpus for schema v0.5 validation.

Use only this folder as the Mining input root:

```text
data/m1_contract_corpus/corpus
```

Do not pass the parent `data/m1_contract_corpus` folder to Mining, because it
also contains human-readable notes and evaluation questions.

## Source Basis

The corpus is derived from existing files in `cloud_core_coldstart_md/`:

- `02_commands/05_add_apn.md`
- `02_commands/03_add_dnn.md`
- `02_commands/07_add_n4peer.md`
- `03_procedures/03_配置SMF到UPF的N4互通.md`
- `04_troubleshooting/03_N4_Association建立失败排障指南.md`
- `05_constraints_alarms/02_ALM-PFCP-PEER-DOWN_用户面对端不可达.md`
- `01_features/gwfd_010224_n4.md`
- `01_features/gwfd_010310_dnn.md`
- `05_constraints_alarms/06_N4互通前置检查清单.md`

The files are shortened and reshaped to trigger specific M1 contract behavior.
There is no `manifest.jsonl`, mapping CSV, or required front matter.

## Intended Coverage

| Area | Files | Expected behavior |
|---|---|---|
| Markdown parsing | `commands/smf/*.md`, `procedures/*.md` | Generate raw documents and raw segments. |
| TXT parsing | `concepts/*.txt`, `troubleshooting/*.txt` | Generate raw documents and raw segments with paragraph-level segmentation. |
| Unparsed registration | `unparsed/*.html`, `unparsed/*.pdf`, `unparsed/*.docx` | Register raw documents only; do not generate raw segments in M1. |
| Exact duplicate | `add_apn_v1.md`, `add_apn_exact_duplicate.md` | Some source segments should map to the same canonical with `exact_duplicate`. |
| Normalized duplicate | `add_apn_normalized_duplicate.md` | Whitespace and punctuation variants should map with `normalized_duplicate`. |
| Scope variant | `add_apn_v2_scope_variant.md` | Version-specific APN differences should map with `scope_variant`. |
| Conflict candidate | `add_apn_conflict.md` | Same-scope contradictory APN parameter requirement should map with `conflict_candidate`. |
| Structure metadata | tables, lists, code blocks, blockquotes, raw HTML | Exercise `block_type`, `structure_json`, and `source_offsets_json`. |
| Entity references | APN, DNN, SMF, UPF, N4, PFCP, ADD APN, ADD N4PEER | Exercise `entity_refs_json` propagation. |

## Acceptance Notes

Minimum assertions for this corpus:

- All recognized files are inserted into `raw_documents`.
- Only Markdown and TXT files create `raw_segments`.
- HTML, PDF, and DOCX files have `processing_profile_json.parse_status` equivalent to unparsed or deferred.
- Every canonical segment has exactly one `primary` source.
- Singleton source segments are not lost during canonicalization.
- Consecutive publish runs produce one active version at a time.
- A failed build keeps the previous active version readable.

