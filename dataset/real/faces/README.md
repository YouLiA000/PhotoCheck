# Authorised face samples

This directory defines the local handling rules for K-07 face samples.

- Raw source photographs and prepared JPEGs stay local and are ignored by Git.
- Only sanitized statistics and this README may be committed.
- Every source photo requires explicit authorisation; record it outside Git.
- Orientation-normalisation may use the current detector to choose among
  `0 / 90 / 180 / 270` degrees, but truth labels remain one visible face per
  source sample in this small authorised set.
- The K-07 target is 20 face samples. Fewer samples must be reported as a gap,
  not filled with public datasets or duplicated pictures.

Prepare the local set:

```powershell
python tools\prepare_face_samples.py --source "D:\Desktop\新建文件夹"
```

The generated `images/`, `manifest.local.csv` and `summary.local.json` are local
only. A public report contains counts and limitations, not image hashes or
source identifiers.
