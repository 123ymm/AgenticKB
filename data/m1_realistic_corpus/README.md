# M1 Realistic Corpus

This corpus is shaped by:

- `cloud_core_coldstart_md/sample_corpus_description.md`
- `cloud_core_coldstart_md/sample_corpus_list.csv`
- `cloud_core_coldstart_md/llm_prompt_template.md`

It is not a vendor manual. The content is written in a UDG-like product
documentation style, but every knowledge point is grounded in public network
sources listed in `source_registry.yaml`.

Use only this folder as the Mining input root:

```text
data/m1_realistic_corpus/corpus
```

Do not pass the parent `data/m1_realistic_corpus` folder to Mining. The parent
folder contains the source registry and user questions.

## File Mix

| Document type | Corpus file | Source basis |
|---|---|---|
| Technical concept | `technical_concepts/5g_network_slicing.md` | 3GPP 5G system overview |
| Terminology | `terms/5gc_nf_terms.md` | 3GPP 5G system overview, free5GC features |
| CLI command reference | `commands/open5gs_ue_wan_route_reference.md` | Open5GS quickstart |
| Deployment guide | `deployment/free5gc_smf_snssai_config.md` | free5GC configuration guide |
| Solution overview | `solutions/local_breakout_solution.md` | 3GPP overview, free5GC UPF/configuration docs |
| Troubleshooting | `troubleshooting/n4_pfcp_association_troubleshooting.txt` | free5GC SMF PFCP and UPF design docs |
| Unparsed HTML | `unparsed/free5gc_upf_design_snapshot.html` | Derived from the UPF concept file in this corpus and public free5GC notes |
| Unparsed PDF | `unparsed/3gpp_pfcp_spec_note.pdf` | Derived from `source_registry.yaml`; M1 should register only |
| Unparsed DOCX | `unparsed/open5gs_subscriber_webui_note.docx` | Derived from Open5GS quickstart notes; M1 should register only |

## Expected M1 Behavior

- Markdown and TXT files should generate `raw_segments`.
- HTML, PDF, and DOCX files should be inserted into `raw_documents` only.
- Source URLs should remain visible through raw document text or metadata when
  parsers preserve them.
- Questions in `questions.yaml` should exercise concept lookup, command usage,
  deployment guidance, troubleshooting, and source audit behavior.

