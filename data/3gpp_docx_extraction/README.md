# 3GPP DOCX Extraction Workspace

This partition is dedicated to extracting large 3GPP `.docx` specifications
into data that can later be used to build realistic corpora, questions, and
answers.

Source documents are currently discovered from:

```text
cloud_core_coldstart_md/2350*-k10.docx
```

All extraction code and generated outputs stay under this partition.

## Layout

```text
data/3gpp_docx_extraction/
  scripts/
    extract_3gpp_docx.py
    search_sections.py
  outputs/
    markdown/      # full extracted Markdown per source docx
    sections/      # section-level JSONL records
    reports/       # manifest and summary
```

## Run

From the repository root:

```bash
python data/3gpp_docx_extraction/scripts/extract_3gpp_docx.py
```

The script uses only Python standard library modules. It reads `.docx` as a
zip archive, parses WordprocessingML, detects 3GPP heading styles, and emits:

- full Markdown per document
- section JSONL per document
- `manifest.json`
- `summary.md`

Search extracted sections:

```bash
python data/3gpp_docx_extraction/scripts/search_sections.py "network slicing" --limit 10
```

## Notes

- This is an extraction workspace, not a final Mining input corpus.
- Generated Markdown can be very large because 3GPP specifications are large.
- Later corpus construction should select specific clauses and examples from
  `outputs/sections/*.jsonl` rather than importing every extracted clause
  blindly.

