# One real part of a real archive

`part-00010.parquet` was downloaded out of MinIO after the run recorded in
`../archive-execution.json`. It is the smallest of the run's 11 parts,
chosen to keep the repository small; the others are identical in form.

- object key: `patient_encounters/2023-01-01/b7f8e22ba2c2/part-00010.parquet`
- rows: 16,088
- bytes: 717,064
- sha256: `7ac8b26b62aa54a2ef3e964b4f2daff2eb2d5fa67d49c3b74ed43aa20414afec`

Verify it:

```bash
shasum -a 256 part-00010.parquet
# 7ac8b26b62aa54a2ef3e964b4f2daff2eb2d5fa67d49c3b74ed43aa20414afec
```

`manifest.json` is the run's manifest exactly as it sits in the bucket. Its top-level
`sha256` is a digest over the ordered per-part digests, so the archive is verifiable
part by part rather than only as a whole. `verified_readback: true` was written only
after every part was downloaded back and re-hashed -- before any source row was deleted.

Read it:

```python
import pandas as pd
print(pd.read_parquet('part-00010.parquet').head())
```
